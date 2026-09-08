"""Heartbeat (spec section 2.1).

Closing the tab must result in leaving, and detection is by heartbeat timeout
rather than `beforeunload`; a `sendBeacon` hint may shorten the delay but
correctness never depends on it (spec section 27).

WS_HEARTBEAT_INTERVAL_MS must stay comfortably below Nginx's
`proxy_read_timeout`, or the proxy will cut idle sockets before the first ping
arrives. Do not raise one without the other.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from app.rooms.types import CloseCode, PeerConnection
from app.ws import events

log = logging.getLogger(__name__)


class Heartbeat:
    """Pings a single connection and terminates it if the pong stops coming."""

    def __init__(
        self,
        *,
        connection: PeerConnection,
        clock: Any,
        interval_ms: int,
        timeout_ms: int,
        last_pong_at: Callable[[], int],
    ) -> None:
        self._conn = connection
        self._clock = clock
        self._interval_ms = interval_ms
        self._timeout_ms = timeout_ms
        self._last_pong_at = last_pong_at
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
            while self._conn.is_open:
                await asyncio.sleep(self._interval_ms / 1000)
                if not self._conn.is_open:
                    return
                silent_for = self._clock.now_ms() - self._last_pong_at()
                if silent_for > self._timeout_ms:
                    log.info("terminating a socket silent for %dms", silent_for)
                    await self._conn.close(CloseCode.HEARTBEAT_TIMEOUT, "heartbeat timeout")
                    return
                try:
                    await self._conn.send_json(events.ping(self._clock.now_ms()))
                except Exception:
                    return
        except asyncio.CancelledError:
            pass
