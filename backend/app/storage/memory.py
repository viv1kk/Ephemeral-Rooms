"""Memory headroom: the disk ledger's counterpart for text.

Files land on the filesystem, so `ReservationLedger` can ask `statvfs` whether
there is room for one. Documents never do - every `pycrdt.Doc` lives in this
process - so with no fixed `MAX_DOC_BYTES` there was nothing at all bounding
how much text a room may hold. This module is what that question consults
instead.

Reading "available memory" correctly is the whole difficulty, because the
number that matters is the one the *container* is held to, not the one the host
reports:

  * cgroup v2 (`memory.max` / `memory.current`) is what Docker uses today, and
    it is the only source that reflects `--memory`. Page cache counts towards
    `memory.current` but is reclaimable rather than lost, so `memory.stat`'s
    `inactive_file` is added back; without that, a container that has merely
    read a lot of file data looks permanently full.
  * cgroup v1 (`memory.limit_in_bytes`) for older hosts, same reasoning.
  * `/proc/meminfo`'s `MemAvailable` when no limit is set - the kernel's own
    estimate of what is allocatable without swapping, which is exactly the
    question, and is why this is not `MemFree`.
  * `GlobalMemoryStatusEx` on Windows, so development there is not a special
    case. Not a deployment target; see backend/Dockerfile.

An unreadable or absent source is never fatal. It reports "unknown", the guard
treats unknown as "not under pressure", and the application behaves as it did
before this module existed: unbounded. Refusing to serve because a metrics file
could not be parsed would be the wrong trade by a wide margin.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import sys
import time
from pathlib import Path
from typing import Callable

from app.storage.protocols import MemoryProvider

log = logging.getLogger(__name__)

# Distinct from 0, which means "measured, and there is none left".
UNKNOWN = -1

_CGROUP_V2 = Path("/sys/fs/cgroup")
_CGROUP_V1 = Path("/sys/fs/cgroup/memory")
_MEMINFO = Path("/proc/meminfo")
# cgroup v1 spells "unlimited" as a sentinel near 2**63 rather than a word.
_V1_UNLIMITED = 1 << 62


class SystemMemory:
    """Production `MemoryProvider`. Every probe runs in a thread; they are all
    file reads or blocking syscalls (spec section 28.1)."""

    async def bytes_available(self) -> int:
        return await asyncio.to_thread(self._probe)

    def _probe(self) -> int:
        for source in (self._cgroup_v2, self._cgroup_v1, self._meminfo, self._windows):
            with contextlib.suppress(Exception):
                value = source()
                if value >= 0:
                    return value
        return UNKNOWN

    # -- Linux, inside a container with a memory limit ---------------------

    def _cgroup_v2(self) -> int:
        # "max" - no limit set - parses as UNKNOWN, which correctly falls
        # through to /proc/meminfo rather than reporting a bogus figure.
        limit = _read_int(_CGROUP_V2 / "memory.max")
        current = _read_int(_CGROUP_V2 / "memory.current")
        if limit == UNKNOWN or current == UNKNOWN:
            return UNKNOWN
        reclaimable = _stat_field(_CGROUP_V2 / "memory.stat", "inactive_file")
        return max(0, limit - current + reclaimable)

    def _cgroup_v1(self) -> int:
        limit = _read_int(_CGROUP_V1 / "memory.limit_in_bytes")
        usage = _read_int(_CGROUP_V1 / "memory.usage_in_bytes")
        if limit == UNKNOWN or usage == UNKNOWN or limit >= _V1_UNLIMITED:
            return UNKNOWN
        reclaimable = _stat_field(_CGROUP_V1 / "memory.stat", "total_inactive_file")
        return max(0, limit - usage + reclaimable)

    # -- Linux, no limit ---------------------------------------------------

    def _meminfo(self) -> int:
        # Values are in kB, and MemAvailable is the kernel's own estimate of
        # what can be allocated without swapping - the question being asked,
        # which is why this is not MemFree.
        kb = _stat_field(_MEMINFO, "MemAvailable")
        return kb * 1024 if kb > 0 else UNKNOWN

    # -- Windows -----------------------------------------------------------

    def _windows(self) -> int:
        if sys.platform != "win32":
            return UNKNOWN

        class _Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _Status()
        status.dwLength = ctypes.sizeof(_Status)
        # `ctypes.windll` exists only on Windows. mypy understands the
        # `sys.platform` guard above, so this line is checked on a Windows
        # machine and treated as unreachable on the Linux CI runner - which is
        # why it needs no `type: ignore`, and why adding one fails there.
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return UNKNOWN
        return int(status.ullAvailPhys)


class MemoryGuard:
    """`available - headroom`, cached, plus the boolean the hot path asks for.

    The mirror of `ReservationLedger.available_for_new_upload` and
    `headroom_breached`, minus the reservations: text arrives as applied
    updates rather than as a declared size, so there is nothing to promise in
    advance. What is left is the same shape - a reserved slice the application
    will not spend, and a question about whether it has been reached.
    """

    def __init__(
        self,
        *,
        memory: MemoryProvider,
        headroom_bytes: int,
        poll_interval_ms: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._memory = memory
        self._headroom = headroom_bytes
        self._ttl = poll_interval_ms / 1000
        self._monotonic = monotonic
        self._cached: int = UNKNOWN
        self._cached_at: float | None = None
        # Probes can overlap: the CRDT path calls this without holding anything.
        self._lock = asyncio.Lock()

    @property
    def headroom_bytes(self) -> int:
        return self._headroom

    async def bytes_available(self) -> int:
        """Raw reading, or UNKNOWN. Cached for MEMORY_POLL_INTERVAL_MS, because
        this is called once per applied CRDT update."""
        async with self._lock:
            now = self._monotonic()
            if self._cached_at is not None and now - self._cached_at < self._ttl:
                return self._cached
            self._cached = await self._memory.bytes_available()
            self._cached_at = now
            return self._cached

    async def available_for_new_text(self) -> int:
        """What rooms may still grow into, headroom already subtracted.

        UNKNOWN passes straight through rather than becoming 0: "we could not
        measure" and "there is none left" must not look the same to a caller,
        and the status bar renders the two differently."""
        reading = await self.bytes_available()
        if reading == UNKNOWN:
            return UNKNOWN
        return max(0, reading - self._headroom)

    async def under_pressure(self) -> bool:
        """True only when a real reading says the headroom has been eaten into.
        Unknown is not pressure; see the module docstring."""
        if self._headroom <= 0:
            return False
        reading = await self.bytes_available()
        return reading != UNKNOWN and reading < self._headroom


def _read_int(path: Path) -> int:
    """One integer from a one-line file. UNKNOWN for "max", for a missing file,
    and for anything else that does not parse."""
    try:
        raw = path.read_text(encoding="ascii").strip()
    except OSError:
        return UNKNOWN
    try:
        return int(raw)
    except ValueError:
        return UNKNOWN


def _stat_field(path: Path, key: str) -> int:
    """One `key value` line out of a cgroup `memory.stat` or `/proc/meminfo`.

    Returns 0 when the file or the field is absent, so a caller adding a
    reclaimable-memory figure can do so unconditionally."""
    try:
        with path.open(encoding="ascii") as fh:
            for line in fh:
                name, _, rest = line.partition(" ")
                # /proc/meminfo writes "MemAvailable:   123 kB", cgroup writes
                # "inactive_file 123"; strip the colon so one reader does both.
                if name.rstrip(":") != key:
                    continue
                with contextlib.suppress(ValueError, IndexError):
                    return int(rest.split()[0])
    except OSError:
        return 0
    return 0
