"""Injectable seams for the things that make tests dishonest if hard-coded.

Spec section 37.1: grace periods, disk pressure, and file storage must all be
substitutable, otherwise the corresponding tests are either absent or faked.
Nothing in the application calls `time`, `os.statvfs`, or `open` directly; it
goes through one of these Protocols, wired up in `app/deps.py`.
"""

from __future__ import annotations

from typing import AsyncIterator, Awaitable, Callable, Protocol


class Clock(Protocol):
    """Wall-clock time and timer scheduling."""

    def now_ms(self) -> int:
        """Milliseconds since the epoch, from the server's clock only."""

    def schedule(self, delay_ms: int, callback: Callable[[], Awaitable[None]]) -> "TimerHandle":
        """Run `callback` after `delay_ms`. The handle must be retained by the
        caller so the underlying task is not garbage collected mid-flight
        (spec section 28.1)."""


class TimerHandle(Protocol):
    def cancel(self) -> None:
        """Cancel a pending timer. Safe to call after it has already fired."""

    @property
    def cancelled(self) -> bool: ...


class DiskSpaceProvider(Protocol):
    async def bytes_available(self) -> int:
        """Free bytes on the volume holding DATA_ROOT, as reported by the OS.

        Implementations must not block the event loop (spec section 28.1)."""


class FileStore(Protocol):
    """Byte storage for room files, keyed exclusively by server-generated UUIDs.

    No method takes a user-supplied name; paths are never built from user input
    (spec section 21.2)."""

    async def create_room_dirs(self, room_instance_id: str) -> None: ...

    async def append_part(self, room_instance_id: str, upload_id: str, data: bytes) -> int:
        """Append to `tmp/<upload_id>.part`, returning the new size in bytes."""

    async def part_size(self, room_instance_id: str, upload_id: str) -> int:
        """Current committed size, or 0 if the part file does not exist."""

    async def discard_part(self, room_instance_id: str, upload_id: str) -> None:
        """Delete a partial upload. Idempotent."""

    async def commit_part(self, room_instance_id: str, upload_id: str, file_id: str) -> None:
        """fsync and atomically rename `tmp/<upload_id>.part` to `files/<file_id>`."""

    async def open_file(self, room_instance_id: str, file_id: str) -> AsyncIterator[bytes]:
        """Stream a stored file. The stream must survive the file being unlinked
        mid-download (spec section 8.1)."""

    async def file_exists(self, room_instance_id: str, file_id: str) -> bool: ...

    async def delete_file(self, room_instance_id: str, file_id: str) -> None: ...

    async def remove_room(self, room_instance_id: str) -> None:
        """Recursively delete a room directory, off the event loop."""

    async def room_exists(self, room_instance_id: str) -> bool:
        """Used by cleanup to verify the deletion actually happened (spec section 33 step 7)."""

    async def sweep_data_root(self) -> None:
        """Delete everything under `<DATA_ROOT>/rooms`. No room survives a
        restart, so its contents are by definition garbage (spec section 15)."""

    async def list_stale_parts(self, room_instance_id: str) -> list[str]: ...
