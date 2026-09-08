"""The /ws endpoint: Starlette transport wrapped around `Session`.

The first frame must be a `join`. Everything after it is dispatched by kind:
binary frames go straight to the CRDT relay without any JSON parsing, text
frames are validated against the discriminated union first (spec section 5).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.deps import services_of
from app.rooms.types import CloseCode
from app.util.ids import new_uuid
from app.ws import events, messages
from app.ws.connection import Session
from app.ws.heartbeat import Heartbeat

log = logging.getLogger(__name__)

router = APIRouter()

# How many malformed frames a socket may send before it is treated as abusive.
# A single ValidationError must never terminate a connection; repeated ones may.
MAX_PROTOCOL_ERRORS = 20


class StarletteConnection:
    """Adapts a Starlette WebSocket to the `PeerConnection` protocol."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self._ws.send_json(payload)

    async def send_bytes(self, payload: bytes) -> None:
        await self._ws.send_bytes(payload)

    async def close(self, code: int, reason: str) -> None:
        if self._ws.client_state is not WebSocketState.DISCONNECTED:
            await self._ws.close(code=code, reason=reason)

    @property
    def is_open(self) -> bool:
        return (
            self._ws.client_state is WebSocketState.CONNECTED
            and self._ws.application_state is WebSocketState.CONNECTED
        )


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    services = services_of(ws)
    conn = StarletteConnection(ws)
    session = Session(connection=conn, services=services)
    heartbeat: Heartbeat | None = None
    errors = 0

    try:
        while True:
            raw = await ws.receive()

            if raw["type"] == "websocket.disconnect":
                break

            if (data := raw.get("bytes")) is not None:
                if session.user is None:
                    continue
                await session.handle_binary(data)
                continue

            text = raw.get("text")
            if text is None:
                continue

            try:
                msg = messages.parse_client_message(
                    text, max_bytes=services.settings.MAX_WS_MESSAGE_BYTES
                )
            except messages.ParseError as exc:
                errors += 1
                await conn.send_json(events.error(exc.code, exc.message))
                if errors > MAX_PROTOCOL_ERRORS:
                    await conn.close(CloseCode.PROTOCOL_ABUSE, "too many invalid messages")
                    break
                continue

            if session.user is None:
                if not isinstance(msg, messages.JoinMessage):
                    await conn.send_json(
                        events.error("not_joined", "Join a room before sending anything else.")
                    )
                    continue
                if not await session.handle_join(msg):
                    break
                user = session.user
                assert user is not None
                heartbeat = Heartbeat(
                    connection=conn,
                    clock=services.clock,
                    interval_ms=services.settings.WS_HEARTBEAT_INTERVAL_MS,
                    timeout_ms=services.settings.WS_HEARTBEAT_TIMEOUT_MS,
                    last_pong_at=lambda: user.last_pong_at,
                )
                heartbeat.start()
                continue

            if isinstance(msg, messages.JoinMessage):
                # Already joined; a duplicate join is ignored rather than
                # rebinding, which would strand the first identity.
                continue

            if not await session.handle_message(msg):
                break

    except WebSocketDisconnect:
        # Not an error: this is the ordinary path for a closed tab or a dropped
        # network, and it is what starts the reconnect grace.
        pass
    except Exception:
        reference = new_uuid()
        log.exception("websocket handler failed (reference %s)", reference)
        try:
            await conn.send_json(
                events.error(
                    "server_error",
                    "Something went wrong on the server.",
                    correlation_id=reference,
                )
            )
        except Exception:
            pass
    finally:
        if heartbeat is not None:
            await heartbeat.stop()
        await session.leave(explicit=False)
        try:
            await conn.close(CloseCode.ROOM_CLOSING, "closed")
        except Exception:
            pass
