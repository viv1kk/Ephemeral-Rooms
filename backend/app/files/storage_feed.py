"""Periodic available-capacity push (spec section 17).

Two figures, because a room holds two kinds of thing and they are limited by
different resources:

  * `available` - free disk minus outstanding reservations minus headroom,
    which is what a file may still grow into.
  * `memoryAvailable` - free memory minus headroom, which is what the text in
    every open document may still grow into. -1 when it could not be measured.

Neither is a quota handed out in advance; both are simply what is left. They
are refreshed on a timer and pushed over the WebSocket so each one reflects
what everyone else is doing, not just this client.

The browser figure is advisory only. The server enforces independently at
reservation time and never trusts a client-declared size beyond using it as the
reservation amount, which is itself validated against bytes actually received.
"""

from __future__ import annotations

import asyncio
import logging

from app.files.reservations import ReservationLedger
from app.rooms.manager import RoomManager
from app.storage.memory import MemoryGuard
from app.ws import events

log = logging.getLogger(__name__)


class StorageBroadcaster:
    def __init__(
        self,
        *,
        manager: RoomManager,
        ledger: ReservationLedger,
        memory: MemoryGuard,
        interval_ms: int,
    ) -> None:
        self._manager = manager
        self._ledger = ledger
        self._memory = memory
        self._interval_ms = interval_ms
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_ms / 1000)
                try:
                    await self.broadcast_once()
                except Exception:
                    log.exception("storage broadcast failed")
        except asyncio.CancelledError:
            pass

    async def broadcast_once(self) -> int:
        """Push the current figures to every connected room. Returns the disk
        value sent, which is what the tests assert on."""
        rooms = [r for r in self._manager.rooms.values() if r.connected_users]
        if not rooms:
            return 0
        # One probe of each for all rooms; both figures are process-wide.
        available = await self._ledger.available_for_new_upload()
        memory_available = await self._memory.available_for_new_text()
        payload = events.storage(available, memory_available)
        for room in rooms:
            await room.broadcast_json(payload)
        return available
