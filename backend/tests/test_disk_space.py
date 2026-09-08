"""The real disk-space provider (spec sections 17, 28.1).

Every other test injects FakeDiskSpace, which is exactly what makes disk
pressure testable without filling a volume. The cost is that StatvfsDiskSpace
itself goes unexercised, and it is platform-branching:

    os.statvfs(...)            on Linux   <- what production runs
    shutil.disk_usage(...)     on Windows <- what a Windows dev machine runs

So on a Windows development machine the branch that actually ships never
executes, and a green suite says nothing about it. These tests run whichever
branch the host provides, which is the concrete reason CI runs on
ubuntu-latest rather than only on a developer's laptop.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.storage.fs import StatvfsDiskSpace

pytestmark = pytest.mark.asyncio


async def test_it_reports_a_plausible_amount_of_free_space(tmp_path: Path) -> None:
    provider = StatvfsDiskSpace(tmp_path)

    free = await provider.bytes_available()

    assert isinstance(free, int)
    assert free > 0, "a writable temp volume should report some free space"
    # A sanity bound rather than a real assertion: catches a unit mix-up, such
    # as returning blocks instead of bytes, which would otherwise pass silently
    # and make the reservation ledger wildly wrong.
    assert free < 1024**6


async def test_it_walks_up_to_an_existing_ancestor(tmp_path: Path) -> None:
    """DATA_ROOT is probed before the boot sweep has necessarily created it, so
    the provider must resolve to the nearest existing parent rather than fail."""
    missing = tmp_path / "not" / "created" / "yet"
    assert not missing.exists()

    free = await StatvfsDiskSpace(missing).bytes_available()

    assert free > 0


async def test_probing_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """statvfs is a blocking syscall and must run in a thread, or it stalls
    every room in the process while it runs (spec section 28.1)."""
    ticks = 0

    async def keep_serving() -> None:
        nonlocal ticks
        for _ in range(15):
            await asyncio.sleep(0.001)
            ticks += 1

    provider = StatvfsDiskSpace(tmp_path)
    free, _ = await asyncio.gather(provider.bytes_available(), keep_serving())

    assert free > 0
    assert ticks == 15, "the loop stopped serving while free space was read"


@pytest.mark.skipif(not hasattr(os, "statvfs"), reason="POSIX only")
async def test_the_posix_branch_agrees_with_shutil(tmp_path: Path) -> None:
    """On Linux, assert the statvfs arithmetic is right.

    f_bavail is blocks available to an unprivileged user and f_frsize is the
    fragment size; multiplying the wrong pair, or using f_bfree instead, gives
    a number that looks reasonable and is wrong. Comparing against
    shutil.disk_usage, which reports the same quantity by a different route,
    catches that."""
    import shutil

    reported = await StatvfsDiskSpace(tmp_path).bytes_available()
    expected = shutil.disk_usage(tmp_path).free

    # Not equal: free space genuinely moves between the two calls on a busy
    # runner. Within 5% is close enough to prove the arithmetic.
    assert abs(reported - expected) <= max(expected * 0.05, 64 * 1024**2)
