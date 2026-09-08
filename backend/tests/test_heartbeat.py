"""Heartbeat behaviour (spec sections 2.1, 22, 27).

Closing a tab must result in leaving, and detection is by heartbeat timeout
rather than an unload handler. A socket that keeps answering must stay open
indefinitely, which is the local half of the "alive past 60 seconds idle"
infrastructure criterion; the other half is Nginx's proxy_read_timeout, checked
against a live instance.

Intervals here are milliseconds, not grace periods. Nothing sleeps through one
of section 2.1's timers.
"""

from __future__ import annotations

import asyncio

import pytest

from app.rooms.types import CloseCode
from app.ws.heartbeat import Heartbeat
from tests.fakes import FakeClock, RecordingConnection

pytestmark = pytest.mark.asyncio

INTERVAL_MS = 20
TIMEOUT_MS = 60


async def _run_heartbeat(conn: RecordingConnection, clock: FakeClock, last_pong) -> Heartbeat:
    beat = Heartbeat(
        connection=conn,
        clock=clock,
        interval_ms=INTERVAL_MS,
        timeout_ms=TIMEOUT_MS,
        last_pong_at=last_pong,
    )
    beat.start()
    return beat


async def test_a_responsive_socket_is_pinged_and_never_closed() -> None:
    conn = RecordingConnection()
    clock = FakeClock()
    # A client that answers every ping keeps its pong timestamp current.
    beat = await _run_heartbeat(conn, clock, lambda: clock.now_ms())

    for _ in range(4):
        await asyncio.sleep(INTERVAL_MS / 1000)
    await beat.stop()

    assert len(conn.events_of("ping")) >= 2, "the server must actually ping"
    assert conn.closed_with is None
    assert conn.is_open


async def test_a_silent_socket_is_terminated_with_a_defined_close_code() -> None:
    """A closed tab produces no pong and no explicit leave; the timeout is what
    detects it."""
    conn = RecordingConnection()
    clock = FakeClock()
    silent_since = clock.now_ms()
    beat = await _run_heartbeat(conn, clock, lambda: silent_since)

    # Virtual time moves past the timeout while the loop keeps ticking.
    clock.jump(TIMEOUT_MS * 2)
    for _ in range(4):
        await asyncio.sleep(INTERVAL_MS / 1000)
        if conn.closed_with is not None:
            break
    await beat.stop()

    assert conn.closed_with is not None, "a silent socket must be terminated"
    assert conn.closed_with[0] == CloseCode.HEARTBEAT_TIMEOUT
    assert not conn.is_open


async def test_stopping_the_heartbeat_does_not_raise() -> None:
    """A cancelled timer must be handled, never surfaced as an unhandled task
    exception (spec section 28.1)."""
    conn = RecordingConnection()
    clock = FakeClock()
    beat = await _run_heartbeat(conn, clock, lambda: clock.now_ms())

    await beat.stop()
    await beat.stop()  # idempotent

    assert conn.closed_with is None


async def test_a_pong_refreshes_the_users_liveness(services, make_peer) -> None:
    from app.ws.messages import PongMessage

    room = await services.manager.create()
    a = await make_peer(room.room_code)
    room.users[a.user_id].last_pong_at = 0

    await a.session.handle_message(PongMessage(type="pong"))

    assert room.users[a.user_id].last_pong_at == services.clock.now_ms()
