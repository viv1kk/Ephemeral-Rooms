"""RoomManager: code allocation, lookup, and the CLOSING handoff.

The manager owns `dict[room_code, RoomInstance]`. Everything in it lives in
this one process's memory, which is why the server must run as exactly one
Uvicorn worker (spec section 20.1).

The section 34 race is resolved here, in `_begin_closing`: the code is removed
from the map *before* any deletion is scheduled. A user arriving at that code
one microsecond later therefore misses, and gets a fresh instance with a fresh
roomInstanceId keyed to a different directory. The old instance then finishes
deleting its own directory, which the new one never touches.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Awaitable, Callable

from app.rooms.instance import RoomInstance
from app.rooms.types import RoomState
from app.storage.protocols import Clock, FileStore

log = logging.getLogger(__name__)

CODE_LOW = 1000
CODE_HIGH = 9999


class NoCodesAvailableError(Exception):
    """MAX_ROOMS reached. Reported as a clear error, never an allocation loop."""


class RoomManager:
    def __init__(
        self,
        *,
        clock: Clock,
        file_store: FileStore,
        settings: Any,
        cleanup: Callable[[RoomInstance], Awaitable[None]],
    ) -> None:
        self._rooms: dict[str, RoomInstance] = {}
        self._clock = clock
        self._file_store = file_store
        self._settings = settings
        self._cleanup = cleanup
        # Cleanup runs detached; the references are kept so the tasks are not
        # garbage collected mid-deletion (spec section 28.1).
        self._closing_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Lookup and allocation
    # ------------------------------------------------------------------

    @property
    def rooms(self) -> dict[str, RoomInstance]:
        return self._rooms

    def get(self, room_code: str) -> RoomInstance | None:
        return self._rooms.get(room_code)

    def allocate_code(self) -> str:
        """A uniformly random unused 4-digit code.

        If MAX_ROOMS is reached this raises rather than searching an exhausted
        space (spec section 1)."""
        if len(self._rooms) >= self._settings.MAX_ROOMS:
            raise NoCodesAvailableError()
        span = CODE_HIGH - CODE_LOW + 1
        # Rejection sampling keeps the distribution uniform. MAX_ROOMS is
        # capped well below the code space (see config), so the expected number
        # of retries stays small; the bounded fallback below covers the tail.
        for _ in range(200):
            code = str(CODE_LOW + secrets.randbelow(span))
            if code not in self._rooms:
                return code
        for candidate in range(CODE_LOW, CODE_HIGH + 1):
            code = str(candidate)
            if code not in self._rooms:
                return code
        raise NoCodesAvailableError()

    async def create(self, room_code: str | None = None) -> RoomInstance:
        code = room_code or self.allocate_code()
        if len(self._rooms) >= self._settings.MAX_ROOMS and code not in self._rooms:
            raise NoCodesAvailableError()
        room = RoomInstance(
            room_code=code,
            clock=self._clock,
            settings=self._settings,
            on_closing=self._begin_closing,
        )
        self._rooms[code] = room
        await self._file_store.create_room_dirs(room.room_instance_id)
        log.info("room %s created (instance %s)", code, room.room_instance_id)
        return room

    async def get_or_create(self, room_code: str) -> tuple[RoomInstance, bool]:
        """Resolve a code to a joinable instance, creating one if needed.

        Returns (room, created). A room in EMPTY_GRACE is returned as-is with
        its state intact; that is the refresh-safety path. A room past the
        CLOSING boundary is already absent from the map, so this cannot return
        one."""
        existing = self._rooms.get(room_code)
        if existing is not None and existing.is_joinable:
            return existing, False
        room = await self.create(room_code)
        room.created_by_url_visit = True
        return room, True

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------

    async def _begin_closing(self, room: RoomInstance) -> None:
        # Step 1 of spec section 33, and the whole of the section 34 fix: drop
        # the code first, so the instance is unreachable before a single byte
        # is deleted.
        if self._rooms.get(room.room_code) is room:
            del self._rooms[room.room_code]
        task = asyncio.create_task(self._run_cleanup(room))
        self._closing_tasks.add(task)
        task.add_done_callback(self._closing_tasks.discard)

    async def _run_cleanup(self, room: RoomInstance) -> None:
        try:
            await self._cleanup(room)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cleanup failed for room instance %s", room.room_instance_id)

    async def close_room_now(self, room: RoomInstance) -> None:
        """Force a room straight to CLOSING, used by shutdown and by tests."""
        if room.state in (RoomState.CLOSING, RoomState.CLOSED):
            return
        room.cancel_all_timers()
        room.state = RoomState.CLOSING
        await self._begin_closing(room)

    async def shutdown(self) -> None:
        """Orderly shutdown. No attempt is made to preserve room state; a
        restart losing every room is the accepted design (spec section 19)."""
        for room in list(self._rooms.values()):
            await self.close_room_now(room)
        if self._closing_tasks:
            await asyncio.gather(*list(self._closing_tasks), return_exceptions=True)
