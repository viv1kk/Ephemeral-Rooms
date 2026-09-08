"""Disk reservation ledger (spec section 17).

A bare pre-flight check is racy: two 40 GB uploads both pass against 50 GB of
free space and then both fail partway through, which is exactly the "leave
existing room files untouched" failure the spec forbids. The ledger fixes that
by subtracting bytes that are promised but not yet written.

    availableForNewUpload = free_bytes - sum(active reservations) - headroom

The subtlety that makes the lock mandatory: `bytes_available()` runs in a
thread executor, so the check-then-reserve sequence contains an `await`, and
the event loop can interleave another upload's check between our check and our
reservation. "Python is single-threaded" does not make this safe. The lock is
held across read-free-space, subtract, compare, and record - and released
before any actual byte is written, because it guards the ledger, not the
transfer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.storage.protocols import DiskSpaceProvider

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReservationDenied:
    """Why an upload was refused, in terms the user can act on."""

    reason: str
    available: int


class ReservationLedger:
    def __init__(self, *, disk: DiskSpaceProvider, headroom_bytes: int) -> None:
        self._disk = disk
        self._headroom = headroom_bytes
        self._reservations: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def reserved_bytes(self) -> int:
        return sum(self._reservations.values())

    def has(self, upload_id: str) -> bool:
        return upload_id in self._reservations

    async def available_for_new_upload(self) -> int:
        """The figure shown in the UI. Advisory for the browser; the server
        enforces independently at reserve time."""
        async with self._lock:
            return await self._available_locked()

    async def _available_locked(self) -> int:
        free = await self._disk.bytes_available()
        return max(0, free - self.reserved_bytes - self._headroom)

    async def reserve(self, upload_id: str, size: int) -> ReservationDenied | None:
        """Atomically check and record. Returns None on success.

        Rejection happens before a single byte is written."""
        async with self._lock:
            available = await self._available_locked()
            if size > available:
                return ReservationDenied(
                    reason="Not enough space on the server for this file.",
                    available=available,
                )
            self._reservations[upload_id] = size
            return None

    async def release(self, upload_id: str) -> None:
        """Release on complete, abort, reap, or room cleanup. Idempotent."""
        async with self._lock:
            self._reservations.pop(upload_id, None)

    async def shrink(self, upload_id: str, remaining: int) -> None:
        """Reduce a reservation to the bytes still outstanding, so a long
        upload does not keep the full declared size reserved after most of it
        has landed."""
        async with self._lock:
            if upload_id in self._reservations:
                self._reservations[upload_id] = max(0, remaining)

    async def headroom_breached(self) -> bool:
        """Mid-upload re-check, called every DISK_RECHECK_INTERVAL_BYTES to
        catch space consumed outside the application."""
        free = await self._disk.bytes_available()
        return free < self._headroom
