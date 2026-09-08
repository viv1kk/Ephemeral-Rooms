"""Real filesystem implementations of the storage protocols.

Everything that touches the disk is either `aiofiles` or `asyncio.to_thread`.
A synchronous write of an 8 MiB chunk, or an rmtree of a directory holding tens
of gigabytes, would stall every WebSocket in every room (spec section 28.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import aiofiles
import aiofiles.os

READ_CHUNK_BYTES = 256 * 1024


class SystemClock:
    """Production `Clock`: real time, real asyncio timers."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def schedule(self, delay_ms: int, callback: Callable[[], Awaitable[None]]) -> "AsyncioTimer":
        return AsyncioTimer(delay_ms, callback)


class AsyncioTimer:
    """A cancellable delayed callback.

    The task reference is held on the instance, and the caller is expected to
    hold the instance, so the timer cannot be collected mid-flight. CancelledError
    is swallowed here rather than surfacing as an unhandled task exception."""

    def __init__(self, delay_ms: int, callback: Callable[[], Awaitable[None]]) -> None:
        self._cancelled = False
        self._task = asyncio.create_task(self._run(delay_ms, callback))

    async def _run(self, delay_ms: int, callback: Callable[[], Awaitable[None]]) -> None:
        try:
            await asyncio.sleep(delay_ms / 1000)
            if not self._cancelled:
                await callback()
        except asyncio.CancelledError:
            pass

    def cancel(self) -> None:
        self._cancelled = True
        if not self._task.done():
            self._task.cancel()

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class StatvfsDiskSpace:
    """Production `DiskSpaceProvider`.

    `os.statvfs` does not exist on Windows, so development there falls back to
    `shutil.disk_usage`, which reports the same quantity. Either way the call
    runs in a thread; it is a blocking syscall (spec section 28.1)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def bytes_available(self) -> int:
        return await asyncio.to_thread(self._probe)

    def _probe(self) -> int:
        target = self._path
        while not target.exists() and target.parent != target:
            target = target.parent
        if hasattr(os, "statvfs"):
            st = os.statvfs(target)
            return int(st.f_bavail * st.f_frsize)
        return int(shutil.disk_usage(target).free)


class LocalFileStore:
    """Production `FileStore`, rooted at `<DATA_ROOT>/rooms/<roomInstanceId>`.

    Keying by instance UUID rather than the reusable 4-digit code is what makes
    the section 34 race structurally impossible: a new room and the cleanup of
    an old one with the same code operate on disjoint directories."""

    def __init__(self, data_root: Path) -> None:
        self._rooms = Path(data_root) / "rooms"

    def _room_dir(self, room_instance_id: str) -> Path:
        return self._rooms / room_instance_id

    def _part_path(self, room_instance_id: str, upload_id: str) -> Path:
        return self._room_dir(room_instance_id) / "tmp" / f"{upload_id}.part"

    def _file_path(self, room_instance_id: str, file_id: str) -> Path:
        return self._room_dir(room_instance_id) / "files" / file_id

    async def create_room_dirs(self, room_instance_id: str) -> None:
        root = self._room_dir(room_instance_id)
        await asyncio.to_thread(lambda: (root / "files").mkdir(parents=True, exist_ok=True))
        await asyncio.to_thread(lambda: (root / "tmp").mkdir(parents=True, exist_ok=True))

    async def append_part(self, room_instance_id: str, upload_id: str, data: bytes) -> int:
        path = self._part_path(room_instance_id, upload_id)
        async with aiofiles.open(path, "ab") as fh:
            await fh.write(data)
            return int(await fh.tell())

    async def part_size(self, room_instance_id: str, upload_id: str) -> int:
        path = self._part_path(room_instance_id, upload_id)
        try:
            return int((await aiofiles.os.stat(path)).st_size)
        except FileNotFoundError:
            return 0

    async def discard_part(self, room_instance_id: str, upload_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            await aiofiles.os.remove(self._part_path(room_instance_id, upload_id))

    async def commit_part(self, room_instance_id: str, upload_id: str, file_id: str) -> None:
        part = self._part_path(room_instance_id, upload_id)
        dest = self._file_path(room_instance_id, file_id)
        # fsync exactly once, at completion. Per-chunk fsync makes large
        # uploads unusably slow (spec section 15).
        await asyncio.to_thread(_fsync_path, part)
        await asyncio.to_thread(os.replace, part, dest)

    async def open_file(self, room_instance_id: str, file_id: str) -> AsyncIterator[bytes]:
        path = self._file_path(room_instance_id, file_id)
        # The descriptor is opened before the first yield. If another user
        # deletes the file mid-download the unlink succeeds but the open
        # descriptor keeps the data alive until this stream ends
        # (spec section 8.1, intentional).
        handle = await aiofiles.open(path, "rb")

        async def _stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await handle.read(READ_CHUNK_BYTES)
                    if not chunk:
                        return
                    yield chunk
            finally:
                await handle.close()

        return _stream()

    async def file_exists(self, room_instance_id: str, file_id: str) -> bool:
        return await aiofiles.os.path.exists(self._file_path(room_instance_id, file_id))

    async def delete_file(self, room_instance_id: str, file_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            await aiofiles.os.remove(self._file_path(room_instance_id, file_id))

    async def remove_room(self, room_instance_id: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self._room_dir(room_instance_id), True)

    async def room_exists(self, room_instance_id: str) -> bool:
        return await aiofiles.os.path.exists(self._room_dir(room_instance_id))

    async def sweep_data_root(self) -> None:
        await asyncio.to_thread(shutil.rmtree, self._rooms, True)
        await asyncio.to_thread(lambda: self._rooms.mkdir(parents=True, exist_ok=True))

    async def list_stale_parts(self, room_instance_id: str) -> list[str]:
        tmp = self._room_dir(room_instance_id) / "tmp"
        if not await aiofiles.os.path.exists(tmp):
            return []
        names = await asyncio.to_thread(os.listdir, tmp)
        return [n[: -len(".part")] for n in names if n.endswith(".part")]


def _fsync_path(path: Path) -> None:
    # The descriptor must be writable: on Windows `os.fsync` maps to `_commit`,
    # which fails with EBADF on a read-only handle.
    with open(path, "rb+") as fh:
        os.fsync(fh.fileno())
