"""Inbound WebSocket control messages.

Every JSON frame is parsed into a typed model by a Pydantic v2 discriminated
union on `type` before it reaches a handler (spec section 5). A ValidationError
never propagates to the connection handler and never terminates the socket; the
frame is dropped and a structured error event is returned instead.

Outbound events are plain dicts built by `events.py`; only the inbound
direction needs schema enforcement, because only the inbound direction is
attacker-controlled.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


class _Base(BaseModel):
    # Reject unknown keys so a typo in a client build surfaces immediately
    # rather than being silently ignored.
    model_config = ConfigDict(extra="forbid")


class JoinMessage(_Base):
    type: Literal["join"]
    roomCode: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")
    # Presented on reconnect. Never trusted as identity by itself; the server
    # matches it against a user in this room instance (spec section 4.1).
    sessionToken: str | None = Field(default=None, max_length=64)


class LeaveMessage(_Base):
    type: Literal["leave"]


class PongMessage(_Base):
    type: Literal["pong"]


class SetNameMessage(_Base):
    type: Literal["set_name"]
    displayName: str = Field(max_length=512)


class CreateDocumentMessage(_Base):
    type: Literal["create_document"]
    name: str = Field(max_length=512)


class RenameDocumentMessage(_Base):
    type: Literal["rename_document"]
    documentId: str = Field(max_length=64)
    name: str = Field(max_length=512)


class DeleteDocumentMessage(_Base):
    type: Literal["delete_document"]
    documentId: str = Field(max_length=64)


class SubscribeDocumentMessage(_Base):
    """Ask for a SYNC_STEP1 so the client can diff against the server's state."""

    type: Literal["subscribe_document"]
    documentId: str = Field(max_length=64)


class DeleteFileMessage(_Base):
    type: Literal["delete_file"]
    fileId: str = Field(max_length=64)


class UploadProgressMessage(_Base):
    """Coarse progress for *other* participants. The uploader's own bar is
    driven locally by HTTP progress events (spec section 5)."""

    type: Literal["upload_progress"]
    uploadId: str = Field(max_length=64)
    percent: int = Field(ge=0, le=100)


ClientMessage = Annotated[
    Union[
        JoinMessage,
        LeaveMessage,
        PongMessage,
        SetNameMessage,
        CreateDocumentMessage,
        RenameDocumentMessage,
        DeleteDocumentMessage,
        SubscribeDocumentMessage,
        DeleteFileMessage,
        UploadProgressMessage,
    ],
    Field(discriminator="type"),
]

_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


class ParseError(Exception):
    """A frame that could not be turned into a valid message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_client_message(raw: str, *, max_bytes: int) -> ClientMessage:
    """Parse one inbound text frame.

    Raises ParseError with a user-safe code and message. The caller reports it
    as a structured error event and keeps the socket open.

    `max_bytes` bounds CONTROL messages only - a join, a rename, a name change.
    Document text is not one of these; it travels as binary CRDT frames, which
    have no application size limit at all (see Session.handle_binary). Do not
    reuse this cap there on the assumption that it is a general frame limit."""
    if len(raw.encode("utf-8")) > max_bytes:
        raise ParseError("message_too_large", "That message was too large and was ignored.")
    try:
        return _adapter.validate_json(raw)
    except ValidationError as exc:
        details = exc.errors()
        location = details[0]["loc"] if details else ()
        field = ".".join(str(p) for p in location if p != "function-after")
        detail = f" ({field})" if field else ""
        raise ParseError("invalid_message", f"That request was not understood{detail}.") from exc
    except ValueError as exc:
        raise ParseError("invalid_json", "That message was not valid JSON.") from exc
