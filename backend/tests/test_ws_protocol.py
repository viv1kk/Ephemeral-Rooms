"""Message validation and the real WebSocket transport (spec sections 5, 21.2).

The Session-level tests elsewhere bypass the transport deliberately. These
drive an actual Starlette WebSocket so that the framing, the discriminated
union, and the error paths are exercised end to end.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.collab import sync
from app.deps import Services
from app.main import create_app
from app.rooms.types import CloseCode
from app.ws.messages import ParseError, parse_client_message
from app.ws.routes import MAX_PROTOCOL_ERRORS


# ----------------------------------------------------------------------
# Schema validation (pure, no transport)
# ----------------------------------------------------------------------


def test_a_valid_join_parses_into_a_typed_model() -> None:
    msg = parse_client_message('{"type":"join","roomCode":"4827"}', max_bytes=1024)
    assert msg.type == "join"
    assert msg.roomCode == "4827"
    assert msg.sessionToken is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"type":"nonsense"}',                      # unknown type
        '{"roomCode":"4827"}',                      # missing discriminator
        '{"type":"join"}',                          # missing required field
        '{"type":"join","roomCode":"48"}',          # wrong shape
        '{"type":"join","roomCode":"abcd"}',        # wrong character class
        '{"type":"join","roomCode":"4827","x":1}',  # unknown field
        '{"type":"upload_progress","uploadId":"a","percent":150}',  # out of range
        "not json at all",
    ],
)
def test_malformed_messages_are_rejected_with_a_user_safe_error(raw: str) -> None:
    with pytest.raises(ParseError) as caught:
        parse_client_message(raw, max_bytes=1024)
    # Plain language, no stack trace, no internal detail (spec section 32).
    assert "Traceback" not in caught.value.message
    assert caught.value.code in ("invalid_message", "invalid_json")


def test_an_oversized_frame_is_rejected_before_parsing() -> None:
    huge = json.dumps({"type": "join", "roomCode": "4827", "sessionToken": "x" * 5000})
    with pytest.raises(ParseError) as caught:
        parse_client_message(huge, max_bytes=1024)
    assert caught.value.code == "message_too_large"


# ----------------------------------------------------------------------
# Binary frame decoding
# ----------------------------------------------------------------------


def test_binary_frames_round_trip_through_the_channel_tag() -> None:
    document_id = "3f169fa2-0a54-442f-913f-ad5f672f8546"
    frame = sync.encode_frame(document_id, b"\x00\x01payload")
    assert frame[0] == sync.CHANNEL_CRDT
    assert sync.decode_frame(frame) == (document_id, b"\x00\x01payload")


@pytest.mark.parametrize("frame", [b"", b"\x01", b"\x99" + b"\x00" * 20, b"\x01\x00" * 3])
def test_an_undecodable_binary_frame_raises_rather_than_corrupting_state(frame: bytes) -> None:
    with pytest.raises((sync.FrameError, ValueError)):
        sync.decode_frame(frame)


# ----------------------------------------------------------------------
# Real transport
# ----------------------------------------------------------------------


@pytest.fixture
def client(services: Services) -> TestClient:
    return TestClient(create_app(services.settings, services=services))


def test_a_websocket_join_returns_identity_and_a_room_snapshot(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "join", "roomCode": "4827"})
        ack = ws.receive_json()

    assert ack["type"] == "joined"
    assert ack["roomCode"] == "4827"
    # clientId must arrive in the acknowledgement, before any sync traffic, so
    # the browser can construct its Y.Doc with it (spec section 7.1).
    assert ack["clientId"] == 1
    assert len(ack["sessionToken"]) == 32  # 128 bits, hex-encoded
    assert ack["room"]["documents"]


def test_a_validation_error_does_not_terminate_the_socket(client: TestClient) -> None:
    """A ValidationError must never propagate to the connection handler and
    must never close the socket (spec section 5)."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "join", "roomCode": "4827"})
        ws.receive_json()

        ws.send_json({"type": "not_a_real_type", "whatever": 1})
        error = ws.receive_json()
        assert error["type"] == "error"

        # The socket is still usable afterwards.
        ws.send_json({"type": "create_document", "name": "still-works.py"})
        created = ws.receive_json()
        assert created["type"] == "document_created"
        assert created["document"]["name"] == "still-works.py"


def test_repeated_abuse_eventually_closes_the_socket(client: TestClient) -> None:
    """One bad frame is tolerated indefinitely; a flood is not."""
    errors_tolerated = 0
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "join", "roomCode": "4827"})
            ws.receive_json()
            for _ in range(MAX_PROTOCOL_ERRORS + 5):
                ws.send_text("garbage")
                ws.receive_json()
                errors_tolerated += 1

    assert caught.value.code == CloseCode.PROTOCOL_ABUSE
    assert errors_tolerated >= MAX_PROTOCOL_ERRORS


def test_messages_before_join_are_refused(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "create_document", "name": "too-early.py"})
        error = ws.receive_json()

    assert error["type"] == "error"
    assert error["code"] == "not_joined"


def test_two_sockets_in_one_room_see_each_other(client: TestClient) -> None:
    with client.websocket_connect("/ws") as first:
        first.send_json({"type": "join", "roomCode": "4827"})
        first.receive_json()

        with client.websocket_connect("/ws") as second:
            second.send_json({"type": "join", "roomCode": "4827"})
            second_ack = second.receive_json()

            arrival = first.receive_json()
            assert arrival["type"] == "user_joined"
            assert arrival["user"]["userId"] == second_ack["userId"]
            # Join order assigns the clientID that breaks CRDT ties.
            assert second_ack["clientId"] == 2


def test_room_creation_over_http_then_join_over_websocket(client: TestClient) -> None:
    created = client.post("/api/rooms")
    assert created.status_code == 200
    code = created.json()["roomCode"]

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "join", "roomCode": code})
        ack = ws.receive_json()

    assert ack["roomCode"] == code
    # Created by the button, not by a URL visit, so no "new room" notice.
    assert ack["roomWasCreated"] is False


def test_the_test_settings_ignore_a_local_env_file(tmp_path, monkeypatch) -> None:
    """A developer's local backend/.env must never change test outcomes.

    This builds its OWN .env with a non-default value and calls the same helper
    the settings fixture uses, so it fails the moment `_env_file=None` is
    dropped - including on a machine whose real .env happens to match the
    defaults, where asserting on the fixture alone would pass by accident."""
    from app.config import Settings
    from tests.conftest import make_test_settings

    (tmp_path / ".env").write_text("MAX_ROOMS=7", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # First, prove the file is discoverable, so the real assertion below means
    # something rather than passing because nothing was ever read.
    assert Settings(DATA_ROOT=tmp_path).MAX_ROOMS == 7

    # The settings the suite actually runs against must ignore it.
    assert make_test_settings(tmp_path / "data").MAX_ROOMS == 200


# ----------------------------------------------------------------------
# Security headers
# ----------------------------------------------------------------------


def test_security_headers_are_present_on_every_response(client: TestClient) -> None:
    """Scored by Mozilla Observatory, and each one earns its place."""
    response = client.post("/api/rooms")
    headers = {k.lower(): v for k, v in response.headers.items()}

    csp = headers["content-security-policy"]
    # The two that matter most: nothing may frame this, and no inline script
    # may run. Both were failing before these headers existed.
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-eval'" not in csp
    # style-src is the one documented relaxation; script-src must never gain it.
    script_directive = next(p for p in csp.split("; ") if p.startswith("script-src"))
    assert "'unsafe-inline'" not in script_directive

    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["cross-origin-resource-policy"] == "same-origin"


def test_hsts_is_sent_only_over_https(client: TestClient) -> None:
    """A browser ignores HSTS on a plain connection, and sending it there would
    make local development look secure when it is not. Uvicorn sits on loopback
    behind the proxy, so the proxy's X-Forwarded-Proto is what says whether the
    browser is actually on HTTPS."""
    plain = client.post("/api/rooms")
    assert "strict-transport-security" not in {k.lower() for k in plain.headers}

    forwarded = client.post("/api/rooms", headers={"X-Forwarded-Proto": "https"})
    hsts = forwarded.headers["strict-transport-security"]
    # Six months is Observatory's threshold; a year is its preload threshold.
    assert int(hsts.split("max-age=")[1].split(";")[0]) >= 31_536_000
    assert "includeSubDomains" in hsts and "preload" in hsts

    # A proxy chain appends, so the client-facing scheme is the first entry.
    chained = client.post("/api/rooms", headers={"X-Forwarded-Proto": "https, http"})
    assert "strict-transport-security" in {k.lower() for k in chained.headers}


def test_an_error_response_still_carries_the_security_headers(
    client: TestClient,
) -> None:
    """The middleware must not clobber what a route already set, and must not
    buffer a streaming response."""
    created = client.post("/api/rooms")
    code = created.json()["roomCode"]

    missing = client.get(f"/api/rooms/{code}/files/not-a-uuid")
    assert missing.status_code == 404
    headers = {k.lower(): v for k, v in missing.headers.items()}
    assert "content-security-policy" in headers
    assert headers["x-content-type-options"] == "nosniff"
