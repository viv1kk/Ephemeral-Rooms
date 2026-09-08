"""Event loop discipline (spec sections 28.1 and 40, Infrastructure).

The whole server is one thread running one event loop, so any blocking call
stalls every room simultaneously. Node hides much of this behind libuv; Python
does not. The acceptance criterion is specific: a large rmtree during cleanup
must not stall other rooms.

These tests use a FileStore whose deletion genuinely blocks a thread, so a
cleanup implemented with an inline `shutil.rmtree` would fail them.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.cleanup.room import RoomCleaner
from app.deps import Services, build_services
from app.storage.fs import LocalFileStore
from tests.fakes import FakeClock

pytestmark = pytest.mark.asyncio

BLOCKING_DELETE_SECONDS = 0.6


class SlowDeleteFileStore(LocalFileStore):
    """A file store whose room deletion takes a long, genuinely blocking time.

    `time.sleep` here stands in for an rmtree of a directory holding tens of
    gigabytes. It blocks whichever thread runs it, so if cleanup calls it
    inline the event loop stops dead."""

    def __init__(self, data_root, seconds: float = BLOCKING_DELETE_SECONDS) -> None:
        super().__init__(data_root)
        self._seconds = seconds
        self.deleted: list[str] = []

    async def remove_room(self, room_instance_id: str) -> None:
        await asyncio.to_thread(self._blocking_delete, room_instance_id)

    def _blocking_delete(self, room_instance_id: str) -> None:
        time.sleep(self._seconds)
        self.deleted.append(room_instance_id)

    async def room_exists(self, room_instance_id: str) -> bool:
        return room_instance_id not in self.deleted


async def test_a_large_room_deletion_does_not_stall_the_event_loop(
    settings, clock: FakeClock, disk
) -> None:
    """Cleanup of one room must not freeze every other room in the process."""
    store = SlowDeleteFileStore(settings.DATA_ROOT)
    services: Services = build_services(settings, clock=clock, disk=disk, file_store=store)
    cleaner = RoomCleaner(file_store=store, ledger=services.ledger)

    doomed = await services.manager.create()
    survivor = await services.manager.create()

    ticks = 0

    async def keep_serving() -> None:
        """Stands in for every other room's traffic."""
        nonlocal ticks
        deadline = time.monotonic() + BLOCKING_DELETE_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            ticks += 1

    started = time.monotonic()
    await asyncio.gather(cleaner.clean(doomed), keep_serving())
    elapsed = time.monotonic() - started

    assert doomed.room_instance_id in store.deleted
    # The loop kept running throughout the deletion. An inline rmtree would
    # have produced a handful of ticks at most, all of them after it finished.
    assert ticks > 20, f"the event loop was stalled during cleanup ({ticks} ticks)"
    # And the two overlapped rather than running back to back.
    assert elapsed < BLOCKING_DELETE_SECONDS * 1.8

    # The unrelated room is untouched and still usable.
    assert services.manager.get(survivor.room_code) is survivor
    await services.manager.shutdown()


async def test_disk_space_probing_does_not_block_the_loop(services: Services, disk) -> None:
    """`statvfs` is a blocking syscall and must be run off the loop."""
    probes = 0

    async def slow_probe() -> None:
        nonlocal probes
        probes += 1
        await asyncio.sleep(0.05)

    disk.probe_hook = slow_probe

    ticks = 0

    async def keep_serving() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.005)
            ticks += 1

    await asyncio.gather(services.ledger.available_for_new_upload(), keep_serving())

    assert probes == 1
    assert ticks == 20, "the loop stopped serving while free space was being read"


async def test_cleanup_is_idempotent_and_safe_to_repeat(
    services: Services, file_store, make_peer
) -> None:
    """Spec section 33: cleanup may be run repeatedly without harm."""
    from app.cleanup.room import RoomCleaner

    room = await services.manager.create()
    await make_peer(room.room_code)
    instance_id = room.room_instance_id
    cleaner = RoomCleaner(file_store=file_store, ledger=services.ledger)

    await cleaner.clean(room)
    await cleaner.clean(room)
    await cleaner.clean(room)

    assert not await file_store.room_exists(instance_id)
    assert room.users == {}
    assert len(room.documents) == 0


async def test_timers_are_cancelled_rather_than_left_to_fire(
    services: Services, clock: FakeClock, settings, make_peer
) -> None:
    """Every transition that invalidates a timer must cancel it explicitly, and
    a cancelled timer must not surface as an unhandled task exception
    (spec section 28.1)."""
    room = await services.manager.create()
    a = await make_peer(room.room_code)

    a.conn.drop()
    await a.session.leave(explicit=False)
    # A user-removal timer and a room-closing timer are now pending.
    assert clock.pending >= 2

    # Reconnecting must cancel both.
    await make_peer(room.room_code, token=a.token)
    assert clock.pending == 0

    # Advancing well past both deadlines fires nothing.
    await clock.advance(settings.ROOM_EMPTY_GRACE_MS * 3)
    assert room.state.value == "ACTIVE"
