"""Stale-upload reaper: layer 2 of the three-layer orphan prevention.

Layer 1 is explicit abort/error/cleanup handling, layer 3 is the boot sweep.
This one catches the case those two cannot: a client that simply stops talking
without aborting, leaving a `.part` file and a live reservation behind
(spec section 15).
"""

from __future__ import annotations

import asyncio
import logging

from app.files.reservations import ReservationLedger
from app.rooms.manager import RoomManager
from app.storage.protocols import Clock, FileStore
from app.ws import events

log = logging.getLogger(__name__)


class UploadReaper:
    def __init__(
        self,
        *,
        manager: RoomManager,
        ledger: ReservationLedger,
        file_store: FileStore,
        clock: Clock,
        stale_ms: int,
        interval_ms: int,
    ) -> None:
        self._manager = manager
        self._ledger = ledger
        self._store = file_store
        self._clock = clock
        self._stale_ms = stale_ms
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
                    await self.sweep_once()
                except Exception:
                    log.exception("upload reaper sweep failed")
        except asyncio.CancelledError:
            pass

    async def sweep_once(self) -> int:
        """Delete every upload with no activity for UPLOAD_STALE_MS. Returns
        the number reaped, which is what the tests assert on."""
        now = self._clock.now_ms()
        reaped = 0
        for room in list(self._manager.rooms.values()):
            for upload in list(room.uploads.values()):
                if upload.writing:
                    continue
                if now - upload.last_activity_at < self._stale_ms:
                    continue
                room.uploads.pop(upload.upload_id, None)
                await self._ledger.release(upload.upload_id)
                await self._store.discard_part(room.room_instance_id, upload.upload_id)
                reaped += 1
                # The uploader has gone silent, so nothing else will ever clear
                # this transfer from other participants' screens.
                await room.broadcast_json(
                    events.upload_ended(upload.upload_id, "abandoned")
                )
                log.info(
                    "reaped stale upload %s in room %s", upload.upload_id, room.room_code
                )
        return reaped
