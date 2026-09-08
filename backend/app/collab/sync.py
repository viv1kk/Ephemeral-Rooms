"""Yjs sync and awareness relay.

Binary frames are dispatched by a leading channel tag before any JSON parsing
is attempted (spec section 5). Because a room holds many documents on one
socket, the tag is followed by the 16 raw bytes of the target document's UUID:

    [tag:1][documentId:16][y-protocol message ...]

The y-protocol message is exactly what `yjs`/`y-protocols` produces in the
browser and what `pycrdt`'s protocol helpers consume here, unmodified.
"""

from __future__ import annotations

from typing import Any

import uuid

from pycrdt import (
    Awareness,
    Doc,
    YMessageType,
    create_awareness_message,
    create_sync_message,
    create_update_message,
    handle_sync_message,
    read_message,
)

CHANNEL_CRDT = 0x01
_UUID_BYTES = 16
_HEADER_BYTES = 1 + _UUID_BYTES


class FrameError(ValueError):
    """A binary frame that could not be decoded. Dropped, never fatal."""


def encode_frame(document_id: str, payload: bytes) -> bytes:
    return bytes([CHANNEL_CRDT]) + uuid.UUID(document_id).bytes + payload


def decode_frame(frame: bytes) -> tuple[str, bytes]:
    """Split a binary frame into its document id and y-protocol payload."""
    if len(frame) < _HEADER_BYTES + 1:
        raise FrameError("frame too short")
    if frame[0] != CHANNEL_CRDT:
        raise FrameError(f"unknown channel tag {frame[0]}")
    document_id = str(uuid.UUID(bytes=bytes(frame[1:_HEADER_BYTES])))
    return document_id, bytes(frame[_HEADER_BYTES:])


def initial_sync_frame(document_id: str, doc: Doc[Any]) -> bytes:
    """SYNC_STEP1 carrying the server's state vector, so a joining or
    reconnecting client receives a diff rather than a full replay."""
    return encode_frame(document_id, create_sync_message(doc))


def apply_sync(document_id: str, doc: Doc[Any], payload: bytes) -> bytes | None:
    """Apply a SYNC message to the server's replica.

    Returns a reply frame when the message was a SYNC_STEP1 (which requires a
    SYNC_STEP2 in response), otherwise None."""
    reply = handle_sync_message(payload[1:], doc)
    return None if reply is None else encode_frame(document_id, reply)


def update_frame(document_id: str, update: bytes) -> bytes:
    return encode_frame(document_id, create_update_message(update))


def apply_awareness(awareness: Awareness, payload: bytes) -> None:
    awareness.apply_awareness_update(read_message(payload[1:]), "remote")


def awareness_frame(document_id: str, awareness: Awareness, client_ids: list[int]) -> bytes | None:
    if not client_ids:
        return None
    update = awareness.encode_awareness_update(client_ids)
    return encode_frame(document_id, create_awareness_message(update))


def message_kind(payload: bytes) -> int:
    """The y-protocol message type: SYNC or AWARENESS."""
    if not payload:
        raise FrameError("empty y-protocol payload")
    kind = payload[0]
    if kind not in (YMessageType.SYNC, YMessageType.AWARENESS):
        raise FrameError(f"unknown y message type {kind}")
    return kind


def new_awareness(doc: Doc[Any]) -> Awareness:
    """Server-side awareness state, kept so late joiners immediately see the
    cursors already present. The server sets no local state of its own, so it
    never appears as a phantom participant."""
    awareness = Awareness(doc)
    awareness.set_local_state(None)
    return awareness
