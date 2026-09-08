"""Application assembly.

Everything the app needs is constructed here and hung off `app.state`, so the
production wiring and a test's fake wiring differ in exactly one place. The
Clock, DiskSpaceProvider and FileStore seams are the reason the grace-period
and disk-pressure tests can be honest (spec section 37.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.cleanup.reaper import UploadReaper
from app.cleanup.room import make_cleanup
from app.config import Settings
from app.files.reservations import ReservationLedger
from app.files.storage_feed import StorageBroadcaster
from app.files.uploads import UploadService
from app.rooms.manager import RoomManager
from app.storage.fs import LocalFileStore, StatvfsDiskSpace, SystemClock
from app.storage.protocols import Clock, DiskSpaceProvider, FileStore


@dataclass
class Services:
    settings: Settings
    clock: Clock
    disk: DiskSpaceProvider
    file_store: FileStore
    ledger: ReservationLedger
    manager: RoomManager
    uploads: UploadService
    reaper: UploadReaper
    storage_feed: StorageBroadcaster


def build_services(
    settings: Settings,
    *,
    clock: Clock | None = None,
    disk: DiskSpaceProvider | None = None,
    file_store: FileStore | None = None,
) -> Services:
    clock = clock or SystemClock()
    disk = disk or StatvfsDiskSpace(settings.DATA_ROOT)
    file_store = file_store or LocalFileStore(settings.DATA_ROOT)

    ledger = ReservationLedger(disk=disk, headroom_bytes=settings.DISK_HEADROOM_BYTES)
    manager = RoomManager(
        clock=clock,
        file_store=file_store,
        settings=settings,
        cleanup=make_cleanup(file_store=file_store, ledger=ledger),
    )
    uploads = UploadService(
        manager=manager,
        ledger=ledger,
        file_store=file_store,
        clock=clock,
        settings=settings,
    )
    reaper = UploadReaper(
        manager=manager,
        ledger=ledger,
        file_store=file_store,
        clock=clock,
        stale_ms=settings.UPLOAD_STALE_MS,
        interval_ms=settings.REAPER_INTERVAL_MS,
    )
    storage_feed = StorageBroadcaster(
        manager=manager,
        ledger=ledger,
        interval_ms=settings.STORAGE_BROADCAST_INTERVAL_MS,
    )
    return Services(
        settings=settings,
        clock=clock,
        disk=disk,
        file_store=file_store,
        ledger=ledger,
        manager=manager,
        uploads=uploads,
        reaper=reaper,
        storage_feed=storage_feed,
    )


def services_of(request_or_ws: Any) -> Services:
    return request_or_ws.app.state.services  # type: ignore[no-any-return]


def client_limits(settings: Settings) -> dict[str, int]:
    """The subset of the caps the browser needs, so it can refuse obviously
    invalid input locally instead of round-tripping to be told no."""
    return {
        "maxDocs": settings.MAX_DOCS_PER_ROOM,
        "maxDocBytes": settings.MAX_DOC_BYTES,
        "maxFiles": settings.MAX_FILES_PER_ROOM,
        "maxFileBytes": settings.MAX_FILE_BYTES,
        "maxDisplayNameChars": settings.MAX_DISPLAY_NAME_CHARS,
        "maxDocNameChars": settings.MAX_DOC_NAME_CHARS,
        "maxFilenameChars": settings.MAX_FILENAME_CHARS,
        "uploadChunkBytes": settings.UPLOAD_CHUNK_BYTES,
        "maxUploadsPerUser": settings.MAX_UPLOADS_PER_USER,
        "heartbeatIntervalMs": settings.WS_HEARTBEAT_INTERVAL_MS,
    }
