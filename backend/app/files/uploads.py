"""Resumable upload protocol (spec section 15).

Init -> PUT chunks with Content-Range -> complete. One `.part` file per upload
with a single committed byte offset, rather than N discrete chunk files: there
is no reassembly step, and therefore no window in which a half-assembled file
can be observed. Resume costs an ordered transmission requirement, which is an
acceptable trade at this scale.

This module holds the state machine only. The HTTP surface, including the
raw-body streaming that keeps FastAPI from spooling chunks to a second temp
file, lives in `routes.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.files.reservations import ReservationLedger
from app.rooms.instance import RoomInstance
from app.rooms.manager import RoomManager
from app.rooms.types import FileRecord, UploadRecord
from app.storage.protocols import Clock, FileStore
from app.util.ids import is_uuid, new_uuid
from app.util.sanitize import clean_filename

log = logging.getLogger(__name__)


@dataclass
class UploadError(Exception):
    """A refusal the caller turns into an HTTP status plus a plain message."""

    status: int
    code: str
    message: str
    extra: dict[str, Any] | None = None


class UploadService:
    def __init__(
        self,
        *,
        manager: RoomManager,
        ledger: ReservationLedger,
        file_store: FileStore,
        clock: Clock,
        settings: Any,
    ) -> None:
        self._manager = manager
        self._ledger = ledger
        self._store = file_store
        self._clock = clock
        self._settings = settings
        # uploadId -> roomCode, so a PUT can find its room without the client
        # telling us which one it belongs to.
        self._index: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def _room(self, room_code: str) -> RoomInstance:
        room = self._manager.get(room_code)
        if room is None or not room.is_joinable:
            raise UploadError(404, "no_room", "That room is no longer available.")
        return room

    def resolve(self, upload_id: str) -> tuple[RoomInstance, UploadRecord]:
        if not is_uuid(upload_id):
            raise UploadError(404, "no_upload", "That upload was not found.")
        room_code = self._index.get(upload_id)
        if room_code is None:
            raise UploadError(404, "no_upload", "That upload was not found.")
        room = self._manager.get(room_code)
        upload = room.uploads.get(upload_id) if room is not None else None
        if room is None or upload is None or upload.aborted:
            raise UploadError(404, "no_upload", "That upload was not found.")
        return room, upload

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    async def init(
        self,
        *,
        room_code: str,
        filename: str,
        size: int,
        uploader_id: str,
    ) -> UploadRecord:
        room = self._room(room_code)
        user = room.users.get(uploader_id)
        if user is None:
            raise UploadError(403, "not_in_room", "You are not connected to this room.")

        if size < 0:
            raise UploadError(400, "bad_size", "That file size is not valid.")

        # Every cap below is opt-in and off by default. What decides whether a
        # file may be uploaded is the reservation further down: free disk,
        # minus what other uploads have already promised to use, minus the
        # headroom the server keeps for its own operation. A file that fits
        # that is allowed, however large it is - which is the whole point of
        # leaving these at 0 (spec section 17).
        if self._settings.MAX_FILE_BYTES and size > self._settings.MAX_FILE_BYTES:
            raise UploadError(413, "file_too_large", "That file is larger than this server allows.")
        per_room = self._settings.MAX_FILES_PER_ROOM
        if per_room and len(room.files) >= per_room:
            raise UploadError(409, "too_many_files", "This room already holds the maximum number of files.")
        if self._settings.MAX_ROOM_TOTAL_BYTES:
            total = sum(f.size for f in room.files.values()) + size
            if total > self._settings.MAX_ROOM_TOTAL_BYTES:
                raise UploadError(413, "room_full", "This room has reached its total storage limit.")

        # Not a limit on what a room may hold: this one bounds how many
        # transfers a single browser may have open at once, and the client
        # queues the rest rather than dropping them, so every chosen file still
        # arrives. Concurrency, not capacity.
        mine = sum(1 for u in room.uploads.values() if u.uploader_id == uploader_id)
        if mine >= self._settings.MAX_UPLOADS_PER_USER:
            raise UploadError(429, "too_many_uploads", "You already have too many uploads in progress.")

        upload_id = new_uuid()
        # Check and reserve atomically, before a single byte is written.
        denied = await self._ledger.reserve(upload_id, size)
        if denied is not None:
            raise UploadError(
                507, "no_space", denied.reason, {"availableBytes": denied.available}
            )

        record = UploadRecord(
            upload_id=upload_id,
            file_id=new_uuid(),
            room_code=room.room_code,
            room_instance_id=room.room_instance_id,
            filename=clean_filename(filename, max_chars=self._settings.MAX_FILENAME_CHARS),
            declared_size=size,
            uploader_id=uploader_id,
            # Snapshotted now, not a live reference (spec section 13).
            uploader_name=user.display_name,
            reservation_bytes=size,
            last_activity_at=self._clock.now_ms(),
        )
        room.uploads[upload_id] = record
        self._index[upload_id] = room.room_code
        return record

    # ------------------------------------------------------------------
    # Offset bookkeeping
    # ------------------------------------------------------------------

    def check_range_start(self, upload: UploadRecord, start: int) -> None:
        """A range must begin exactly at the committed offset.

        Anything else is a 409 carrying the current offset, so the client can
        resynchronize rather than guess (spec section 15)."""
        if start != upload.committed_offset:
            raise UploadError(
                409,
                "offset_mismatch",
                "That upload chunk did not line up; resuming from the last confirmed position.",
                {"committedOffset": upload.committed_offset},
            )

    async def write_chunk(self, room: RoomInstance, upload: UploadRecord, data: bytes) -> int:
        """Append one buffer, advancing the committed offset only after the
        write has completed. If the client vanishes mid-stream the offset is
        left at the last fully written position, never a partial one."""
        if upload.committed_offset + len(data) > upload.declared_size:
            raise UploadError(
                400, "over_declared_size", "That upload sent more data than it declared."
            )
        size = await self._store.append_part(room.room_instance_id, upload.upload_id, data)
        upload.committed_offset = size
        upload.last_activity_at = self._clock.now_ms()
        upload.bytes_since_recheck += len(data)

        # Keep the ledger honest as bytes land: what is still promised is what
        # has not yet been written.
        await self._ledger.shrink(upload.upload_id, upload.declared_size - size)

        if upload.bytes_since_recheck >= self._settings.DISK_RECHECK_INTERVAL_BYTES:
            upload.bytes_since_recheck = 0
            # Catch space consumed outside the application mid-transfer.
            if await self._ledger.headroom_breached():
                await self.abort(room, upload)
                raise UploadError(
                    507, "no_space", "The server ran out of space while receiving this file."
                )
        return size

    # ------------------------------------------------------------------
    # Terminal transitions
    # ------------------------------------------------------------------

    async def complete(self, room: RoomInstance, upload: UploadRecord) -> FileRecord:
        actual = await self._store.part_size(room.room_instance_id, upload.upload_id)
        if actual != upload.declared_size:
            raise UploadError(
                409,
                "incomplete",
                "That upload is not finished yet.",
                {"committedOffset": actual},
            )
        await self._store.commit_part(room.room_instance_id, upload.upload_id, upload.file_id)
        record = FileRecord(
            file_id=upload.file_id,
            original_name=upload.filename,
            size=actual,
            uploader_id=upload.uploader_id,
            uploader_name=upload.uploader_name,
            # The server's clock, never the client's (spec section 13).
            uploaded_at=self._clock.now_ms(),
        )
        room.files[record.file_id] = record
        room.uploads.pop(upload.upload_id, None)
        self._index.pop(upload.upload_id, None)
        await self._ledger.release(upload.upload_id)
        return record

    async def abort(self, room: RoomInstance, upload: UploadRecord) -> None:
        """Delete the partial data and release the reservation. Idempotent."""
        upload.aborted = True
        room.uploads.pop(upload.upload_id, None)
        self._index.pop(upload.upload_id, None)
        await self._ledger.release(upload.upload_id)
        await self._store.discard_part(room.room_instance_id, upload.upload_id)
