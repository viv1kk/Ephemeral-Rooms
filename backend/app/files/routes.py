"""HTTP surface: room creation, the resumable upload protocol, and downloads.

The upload endpoints deliberately avoid `UploadFile` and `File(...)`. FastAPI
spools a multipart body into a SpooledTemporaryFile, writing the whole chunk to
disk a second time before the handler sees it - doubling transient disk usage
and putting bytes on disk outside the reservation ledger's accounting, which
defeats section 17 entirely. Chunks arrive as a raw PUT body read through
`request.stream()` (spec section 15).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.deps import services_of
from app.files.uploads import UploadError
from app.rooms.manager import NoCodesAvailableError
from app.util.ids import is_uuid

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
# Bounded so a single chunk cannot be streamed into memory without limit.
STREAM_READ_BYTES = 1024 * 1024


class CreateRoomResponse(BaseModel):
    roomCode: str


class InitUploadRequest(BaseModel):
    filename: str = Field(max_length=1024)
    size: int = Field(ge=0)
    userId: str = Field(max_length=64)


def _error(exc: UploadError) -> JSONResponse:
    body: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.extra:
        body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status)


@router.post("/rooms", response_model=CreateRoomResponse)
async def create_room(request: Request) -> Any:
    """Allocate a code and an empty instance. The creator then connects over
    the WebSocket like anyone else."""
    services = services_of(request)
    try:
        room = await services.manager.create()
    except NoCodesAvailableError:
        return JSONResponse(
            {"code": "at_capacity", "message": "The server is at capacity. Please try again shortly."},
            status_code=503,
        )
    return {"roomCode": room.room_code}


@router.get("/version")
async def version(request: Request) -> dict[str, str]:
    """Which build of the backend is answering.

    The frontend shows this beside its own build id, and the two are expected
    to drift: `web` updates itself when a new image is published, while this
    service is promoted by hand because restarting it destroys every live room.
    Showing both makes that drift visible instead of leaving it to be inferred
    from behaviour.
    """
    return {"build": services_of(request).settings.BUILD_ID}


@router.get("/storage")
async def storage(request: Request) -> dict[str, int]:
    """What is left, after each resource's reserved headroom.

    `available` bounds files, `memoryAvailable` bounds document text; -1 for
    the latter means the platform exposed nothing to measure. Also the
    container health check, which is why it touches the data volume."""
    services = services_of(request)
    return {
        "available": await services.ledger.available_for_new_upload(),
        "memoryAvailable": await services.memory.available_for_new_text(),
    }


@router.post("/rooms/{room_code}/uploads")
async def init_upload(room_code: str, body: InitUploadRequest, request: Request) -> Any:
    services = services_of(request)
    try:
        upload = await services.uploads.init(
            room_code=room_code,
            filename=body.filename,
            size=body.size,
            uploader_id=body.userId,
        )
    except UploadError as exc:
        return _error(exc)
    return {
        "uploadId": upload.upload_id,
        "fileId": upload.file_id,
        "filename": upload.filename,
        "committedOffset": upload.committed_offset,
    }


@router.get("/uploads/{upload_id}")
async def upload_status(upload_id: str, request: Request) -> Any:
    """Where to resume from after a network failure."""
    services = services_of(request)
    try:
        _, upload = services.uploads.resolve(upload_id)
    except UploadError as exc:
        return _error(exc)
    return {"committedOffset": upload.committed_offset, "declaredSize": upload.declared_size}


@router.put("/uploads/{upload_id}")
async def upload_chunk(upload_id: str, request: Request) -> Any:
    services = services_of(request)
    try:
        room, upload = services.uploads.resolve(upload_id)
    except UploadError as exc:
        return _error(exc)

    header = request.headers.get("content-range")
    if header is None:
        return _error(UploadError(400, "no_range", "That upload chunk was missing its range."))
    match = _CONTENT_RANGE.match(header.strip())
    if match is None:
        return _error(UploadError(400, "bad_range", "That upload chunk had an invalid range."))
    start, end = int(match.group(1)), int(match.group(2))
    if end < start:
        return _error(UploadError(400, "bad_range", "That upload chunk had an invalid range."))

    try:
        services.uploads.check_range_start(upload, start)
    except UploadError as exc:
        return _error(exc)

    upload.writing = True
    buffer = bytearray()
    try:
        async for piece in request.stream():
            buffer.extend(piece)
            if len(buffer) >= STREAM_READ_BYTES:
                await services.uploads.write_chunk(room, upload, bytes(buffer))
                buffer.clear()
        if buffer:
            await services.uploads.write_chunk(room, upload, bytes(buffer))
    except UploadError as exc:
        return _error(exc)
    except Exception:
        # The client vanished mid-stream. committedOffset is already at the
        # last fully written position, so the client can resume from it.
        log.info("upload %s interrupted at offset %d", upload_id, upload.committed_offset)
        return JSONResponse(
            {"code": "interrupted", "committedOffset": upload.committed_offset},
            status_code=499,
        )
    finally:
        upload.writing = False

    return {"committedOffset": upload.committed_offset}


@router.post("/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, request: Request) -> Any:
    services = services_of(request)
    try:
        room, upload = services.uploads.resolve(upload_id)
        record = await services.uploads.complete(room, upload)
    except UploadError as exc:
        return _error(exc)

    from app.ws import events  # imported here to keep the module graph acyclic

    # The upload id travels with the file so watchers can retire the progress
    # row they have been tracking under that id.
    await room.broadcast_json(events.file_added(record, upload.upload_id))
    return {"file": record.public()}


@router.delete("/uploads/{upload_id}")
async def abort_upload(upload_id: str, request: Request) -> Any:
    services = services_of(request)
    try:
        room, upload = services.uploads.resolve(upload_id)
    except UploadError as exc:
        return _error(exc)
    await services.uploads.abort(room, upload)

    from app.ws import events

    # No file will ever arrive for this upload, so tell watchers explicitly or
    # their progress row stays at whatever percentage it had reached.
    await room.broadcast_json(events.upload_ended(upload_id, "cancelled"))
    return Response(status_code=204)


@router.get("/rooms/{room_code}/files/{file_id}")
async def download(room_code: str, file_id: str, request: Request) -> Any:
    """Stream a file.

    The file is resolved within the instance currently bound to `room_code`. A
    fileId belonging to another room returns 404 rather than 403, so the
    response never confirms that it exists elsewhere (spec section 14)."""
    services = services_of(request)
    room = services.manager.get(room_code)
    # Reject a malformed id before it can reach the filesystem layer.
    if room is None or not is_uuid(file_id):
        return JSONResponse({"code": "not_found", "message": "That file was not found."}, 404)
    record = room.files.get(file_id)
    if record is None or not await services.file_store.file_exists(room.room_instance_id, file_id):
        return JSONResponse({"code": "not_found", "message": "That file was not found."}, 404)

    stream = await services.file_store.open_file(room.room_instance_id, file_id)
    return StreamingResponse(
        stream,
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(record.size),
            "Content-Disposition": _content_disposition(record.original_name),
            # An uploaded HTML or SVG file must not execute on this origin.
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def _content_disposition(filename: str) -> str:
    """RFC 5987 encoding, so spaces, Unicode and punctuation survive."""
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
