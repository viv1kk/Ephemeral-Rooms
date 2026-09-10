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

The second half of this file covers BudgetedDiskSpace, which is the other
way the ledger can be told there is less room than the volume reports.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.deps import build_services
from app.files.reservations import ReservationLedger
from app.storage.fs import BudgetedDiskSpace, StatvfsDiskSpace

from tests.fakes import FakeDiskSpace

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


class _FrozenClock:
    """Monotonic time the test controls, so the usage cache can be aged
    deliberately rather than by sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


async def test_it_reports_the_budget_when_the_volume_is_larger(tmp_path: Path) -> None:
    """The point of the whole class: a 1 TB volume must not be offered to an
    application that is only allowed 10 MB of it."""
    provider = BudgetedDiskSpace(
        FakeDiskSpace(free_bytes=1024**4), data_root=tmp_path, budget_bytes=10 * 1024**2
    )

    assert await provider.bytes_available() == 10 * 1024**2


async def test_it_reports_the_volume_when_the_volume_is_smaller(tmp_path: Path) -> None:
    """A budget is a second ceiling, never a promise. If the disk is nearly
    full the disk still wins, or the ledger would admit an upload the volume
    cannot physically take."""
    provider = BudgetedDiskSpace(
        FakeDiskSpace(free_bytes=4096), data_root=tmp_path, budget_bytes=10 * 1024**3
    )

    assert await provider.bytes_available() == 4096


async def test_stored_bytes_come_off_the_budget(tmp_path: Path) -> None:
    _write(tmp_path / "rooms" / "inst" / "files" / "a", 300)
    _write(tmp_path / "rooms" / "inst" / "files" / "b", 700)

    provider = BudgetedDiskSpace(
        FakeDiskSpace(free_bytes=1024**4), data_root=tmp_path, budget_bytes=5000
    )

    assert await provider.bytes_used() == 1000
    assert await provider.bytes_available() == 4000


async def test_a_full_budget_reports_zero_not_a_negative_number(tmp_path: Path) -> None:
    """max(0, ...) is not cosmetic: `reserve` compares `size > available`, and a
    negative available would still refuse, but the figure is broadcast to every
    client and a negative byte count in the UI is a bug report."""
    _write(tmp_path / "rooms" / "inst" / "files" / "big", 9000)

    provider = BudgetedDiskSpace(
        FakeDiskSpace(free_bytes=1024**4), data_root=tmp_path, budget_bytes=5000
    )

    assert await provider.bytes_available() == 0


async def test_it_counts_partial_uploads_not_just_committed_files(tmp_path: Path) -> None:
    """Bytes in `tmp/*.part` are on the disk and must count against the budget
    while they are being written, or a transfer in flight is invisible to it."""
    _write(tmp_path / "rooms" / "inst" / "tmp" / "u1.part", 2500)

    provider = BudgetedDiskSpace(
        FakeDiskSpace(free_bytes=1024**4), data_root=tmp_path, budget_bytes=10_000
    )

    assert await provider.bytes_used() == 2500


async def test_a_missing_data_root_is_zero_bytes_used(tmp_path: Path) -> None:
    """The budget is read before the boot sweep has necessarily created the
    data root, exactly as StatvfsDiskSpace is."""
    provider = BudgetedDiskSpace(
        FakeDiskSpace(free_bytes=1024**4),
        data_root=tmp_path / "not" / "created" / "yet",
        budget_bytes=5000,
    )

    assert await provider.bytes_used() == 0
    assert await provider.bytes_available() == 5000


async def test_usage_is_cached_briefly_then_refreshed(tmp_path: Path) -> None:
    """`headroom_breached` runs every DISK_RECHECK_INTERVAL_BYTES during a
    transfer. Without the cache that is a directory walk per 64 MB."""
    clock = _FrozenClock()
    _write(tmp_path / "rooms" / "inst" / "files" / "a", 100)
    provider = BudgetedDiskSpace(
        FakeDiskSpace(free_bytes=1024**4),
        data_root=tmp_path,
        budget_bytes=10_000,
        monotonic=clock,
    )

    assert await provider.bytes_used() == 100

    _write(tmp_path / "rooms" / "inst" / "files" / "b", 400)
    assert await provider.bytes_used() == 100, "should still be serving the cached reading"

    clock.now += BudgetedDiskSpace.CACHE_TTL_SECONDS
    assert await provider.bytes_used() == 500, "the cache should have expired"


async def test_measuring_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """A walk over MAX_ROOMS x MAX_FILES_PER_ROOM entries would stall every
    room in the process if it ran on the loop (spec section 28.1)."""
    for i in range(200):
        _write(tmp_path / "rooms" / f"inst{i}" / "files" / "f", 64)
    ticks = 0

    async def keep_serving() -> None:
        nonlocal ticks
        for _ in range(15):
            await asyncio.sleep(0.001)
            ticks += 1

    provider = BudgetedDiskSpace(
        FakeDiskSpace(free_bytes=1024**4), data_root=tmp_path, budget_bytes=10**9
    )
    used, _ = await asyncio.gather(provider.bytes_used(), keep_serving())

    assert used == 200 * 64
    assert ticks == 15, "the loop stopped serving while the data root was measured"


async def test_it_refuses_to_wrap_without_a_budget(tmp_path: Path) -> None:
    """0 means unlimited, and `build_services` is expected to skip the wrapper
    entirely rather than construct one that caps at zero."""
    with pytest.raises(ValueError):
        BudgetedDiskSpace(FakeDiskSpace(), data_root=tmp_path, budget_bytes=0)


async def test_the_ledger_admits_and_refuses_against_the_budget(tmp_path: Path) -> None:
    """The end the feature exists for: the budget, not the volume, decides.

    Also pins the interaction with the headroom and with an outstanding
    reservation, which is where an off-by-one would actually hurt."""
    provider = BudgetedDiskSpace(
        FakeDiskSpace(free_bytes=1024**4), data_root=tmp_path, budget_bytes=10_000
    )
    ledger = ReservationLedger(disk=provider, headroom_bytes=1_000)

    assert await ledger.available_for_new_upload() == 9_000

    assert await ledger.reserve("u1", 6_000) is None
    assert await ledger.available_for_new_upload() == 3_000

    denied = await ledger.reserve("u2", 4_000)
    assert denied is not None
    assert denied.available == 3_000

    await ledger.release("u1")
    assert await ledger.available_for_new_upload() == 9_000


async def test_build_services_wires_the_budget_from_settings(tmp_path: Path) -> None:
    """The setting has to reach the ledger, and 0 has to leave it unwrapped."""
    unlimited = build_services(
        Settings(DATA_ROOT=tmp_path, MAX_TOTAL_STORAGE_BYTES=0),
        disk=FakeDiskSpace(free_bytes=1024**4),
    )
    assert not isinstance(unlimited.disk, BudgetedDiskSpace)

    capped = build_services(
        Settings(DATA_ROOT=tmp_path, MAX_TOTAL_STORAGE_BYTES=10 * 1024**2, DISK_HEADROOM_BYTES=0),
        disk=FakeDiskSpace(free_bytes=1024**4),
    )
    assert isinstance(capped.disk, BudgetedDiskSpace)
    assert await capped.ledger.available_for_new_upload() == 10 * 1024**2
