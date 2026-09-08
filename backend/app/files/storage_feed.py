"""Periodic available-storage push (spec section 17).

The figure shown in the room's status bar is `availableForNewUpload`: free
space minus outstanding reservations minus headroom. It is refreshed on a timer
and pushed over the WebSocket so it reflects other people's uploads, not just
this client's.

The browser figure is advisory only. The server enforces independently at
reservation time and never trusts a client-declared size beyond using it as the
reservation amount, which is itself validated against bytes actually received.
"""

from __future__ import annotations

import asyncio
import logging

from app.files.reservations import ReservationLedger
from app.rooms.manager import RoomManager
from app.ws import events

log = logging.getLogger(__name__)


class StorageBroadcaster:
    def __init__(
        self,
        *,
        manager: RoomManager,
        ledger: ReservationLedger,
        interval_ms: int,
    ) -> None:
        self._manager = manager
        self._ledger = ledger
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
        """Push the current figure to every connected room. Returns the value
        sent, which is what the tests assert on."""
        rooms = [r for r in self._manager.rooms.values() if r.connected_users]
        if not rooms:
            return 0
        # One disk probe for all rooms; the figure is process-wide.
        available = await self._ledger.available_for_new_upload()
        payload = events.storage(available)
        for room in rooms:
            await room.broadcast_json(payload)
        return available
