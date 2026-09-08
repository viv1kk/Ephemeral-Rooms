"""Room cleanup (spec section 33).

Runs on entering CLOSING, after the manager has already removed the code from
its map. The steps are ordered so that the room becomes unreachable before it
becomes incomplete: nothing can observe a half-deleted room, because nothing
can find it at all.

Cleanup is idempotent and safe to run repeatedly. It retries with backoff and
never leaves room contents in place on the grounds that deletion errored.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.files.reservations import ReservationLedger
from app.rooms.instance import RoomInstance
from app.rooms.types import CloseCode
from app.storage.protocols import FileStore
from app.ws import events

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 0.2


class RoomCleaner:
    def __init__(self, *, file_store: FileStore, ledger: ReservationLedger) -> None:
        self._store = file_store
        self._ledger = ledger

    async def clean(self, room: RoomInstance) -> None:
        instance_id = room.room_instance_id

        # 2. Reject all new operations. The state is already CLOSING; timers
        #    are cancelled so nothing can schedule further work.
        room.cancel_all_timers()

        # 3. Abort in-flight uploads and release their reservations.
        for upload in list(room.uploads.values()):
            upload.aborted = True
            await self._ledger.release(upload.upload_id)
            await self._store.discard_part(instance_id, upload.upload_id)
        room.uploads.clear()

        # 4. Close any socket still attached, with a defined close code.
        await self._close_connections(room)

        # 5. Drop CRDT references so the Python objects and the underlying Rust
        #    allocations are released.
        room.documents.clear()
        room.awareness.clear()

        # 6-7. Delete the directory off the event loop, then verify it is gone.
        await self._delete_verified(room)

        # 8. Clear presence and the session-token table.
        room.users.clear()
        room.files.clear()

        # 9.
        room.mark_closed()
        log.info("room %s (instance %s) closed", room.room_code, instance_id)

    async def _close_connections(self, room: RoomInstance) -> None:
        notice = events.room_closing("This room was closed because everyone left.")
        for user in list(room.users.values()):
            conn = user.connection
            if conn is None or not conn.is_open:
                continue
            try:
                await conn.send_json(notice)
                await conn.close(CloseCode.ROOM_CLOSING, "room closed")
            except Exception:
                log.debug("failed to close a connection during cleanup", exc_info=True)
            user.connection = None

    async def _delete_verified(self, room: RoomInstance) -> None:
        instance_id = room.room_instance_id
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                # rmtree of a directory holding tens of gigabytes can take
                # seconds; doing it inline would freeze every other room
                # (spec section 28.1). The FileStore runs it in a thread.
                await self._store.remove_room(instance_id)
                if not await self._store.room_exists(instance_id):
                    return
                last_error = RuntimeError("directory still present after removal")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(BACKOFF_BASE_S * (2**attempt))
        log.error(
            "cleanup could not remove room instance %s after %d attempts: %s",
            instance_id,
            MAX_ATTEMPTS,
            last_error,
        )


def make_cleanup(*, file_store: FileStore, ledger: ReservationLedger) -> Any:
    cleaner = RoomCleaner(file_store=file_store, ledger=ledger)

    async def _cleanup(room: RoomInstance) -> None:
        await cleaner.clean(room)

    return _cleanup
