"""Test doubles for the three injectable seams (spec section 37.1).

The FakeClock is the important one. A suite that sleeps through
ROOM_EMPTY_GRACE_MS takes minutes and gets quietly deleted by whoever maintains
it next, so tests advance this clock instead. `asyncio.sleep` never appears in
a test as a way to wait out a grace period.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from typing import Any, Awaitable, Callable


class FakeTimer:
    def __init__(self, clock: "FakeClock", seq: int) -> None:
        self._clock = clock
        self._seq = seq
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        self._clock._cancelled.add(self._seq)

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class FakeClock:
    """Virtual time. Timers fire only when `advance` walks past their deadline."""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self._now = start_ms
        self._heap: list[tuple[int, int, Callable[[], Awaitable[None]]]] = []
        self._counter = itertools.count()
        self._cancelled: set[int] = set()

    def now_ms(self) -> int:
        return self._now

    def schedule(self, delay_ms: int, callback: Callable[[], Awaitable[None]]) -> FakeTimer:
        seq = next(self._counter)
        heapq.heappush(self._heap, (self._now + delay_ms, seq, callback))
        return FakeTimer(self, seq)

    async def advance(self, delta_ms: int) -> None:
        """Move virtual time forward, running every timer that comes due.

        Callbacks may schedule further timers; those fire too if they fall
        inside the window, which is what makes a chain like
        disconnect-grace -> empty-grace -> CLOSING testable in one call."""
        target = self._now + delta_ms
        while self._heap and self._heap[0][0] <= target:
            when, seq, callback = heapq.heappop(self._heap)
            self._now = max(self._now, when)
            if seq in self._cancelled:
                continue
            await callback()
            # Let anything the callback spawned (cleanup tasks, broadcasts)
            # reach a suspension point before the next timer fires.
            await asyncio.sleep(0)
        self._now = target
        await asyncio.sleep(0)

    def jump(self, delta_ms: int) -> None:
        """Move the wall clock forward WITHOUT running due timers.

        Used to age a value that is compared against `now_ms()` (a last-pong
        timestamp, say) in a test that is driving its own loop."""
        self._now += delta_ms

    @property
    def pending(self) -> int:
        return sum(1 for _, seq, _ in self._heap if seq not in self._cancelled)


class FakeDiskSpace:
    """Settable free space, so 'insufficient disk' is testable without filling
    a real volume."""

    def __init__(self, free_bytes: int = 100 * 1024**3) -> None:
        self.free_bytes = free_bytes
        self.calls = 0
        # Awaited inside the ledger's lock; setting it proves the lock actually
        # serializes check-then-reserve across the await.
        self.probe_hook: Callable[[], Awaitable[None]] | None = None

    async def bytes_available(self) -> int:
        self.calls += 1
        if self.probe_hook is not None:
            await self.probe_hook()
        return self.free_bytes


class FakeMemory:
    """Settable free memory, the counterpart of FakeDiskSpace.

    Memory pressure is not something a test can produce honestly - filling the
    runner's RAM to see what the guard does would be a test that occasionally
    kills the test runner - so the reading is injected, exactly as free disk
    space is (spec section 37.1).

    Defaults high enough that a test which does not care about memory never
    trips the guard. `-1` is the "could not be measured" reading; see
    app/storage/memory.py.
    """

    def __init__(self, available_bytes: int = 8 * 1024**3) -> None:
        self.available_bytes = available_bytes
        self.calls = 0

    async def bytes_available(self) -> int:
        self.calls += 1
        return self.available_bytes


class RecordingConnection:
    """A `PeerConnection` that records frames instead of sending them.

    Two of these are two genuinely independent sessions, which is what the
    concurrency tests need."""

    def __init__(self, name: str = "peer") -> None:
        self.name = name
        self.json_frames: list[dict[str, Any]] = []
        self.binary_frames: list[bytes] = []
        self.closed_with: tuple[int, str] | None = None
        self._open = True

    async def send_json(self, payload: dict[str, Any]) -> None:
        if not self._open:
            raise RuntimeError("connection closed")
        self.json_frames.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        if not self._open:
            raise RuntimeError("connection closed")
        self.binary_frames.append(payload)

    async def close(self, code: int, reason: str) -> None:
        self.closed_with = (code, reason)
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def drop(self) -> None:
        """Simulate a network failure: the socket dies with no explicit leave."""
        self._open = False

    # -- assertions helpers ------------------------------------------------

    def events_of(self, event_type: str) -> list[dict[str, Any]]:
        return [f for f in self.json_frames if f.get("type") == event_type]

    def last(self, event_type: str) -> dict[str, Any] | None:
        found = self.events_of(event_type)
        return found[-1] if found else None
