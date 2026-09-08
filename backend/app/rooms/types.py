"""Shared room data types and the connection seam.

`PeerConnection` exists so that room logic never imports Starlette. Tests drive
rooms through a fake connection that records frames; production wraps a real
WebSocket (spec section 37.1).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol


class RoomState(str, enum.Enum):
    """Spec section 2.1. The ACTIVE -> EMPTY_GRACE -> CLOSING boundary is the
    single point that decides whether a returning user resurrects the room or
    gets a brand-new one."""

    ACTIVE = "ACTIVE"
    EMPTY_GRACE = "EMPTY_GRACE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class ConnectionState(str, enum.Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"


class CloseCode:
    """Defined close codes, so a client can tell why it was dropped."""

    ROOM_CLOSING = 4000
    ROOM_FULL = 4001
    HEARTBEAT_TIMEOUT = 4002
    SERVER_SHUTDOWN = 4003
    PROTOCOL_ABUSE = 4004
    REPLACED_BY_RESUME = 4005


class PeerConnection(Protocol):
    """One live transport to one browser."""

    async def send_json(self, payload: dict[str, Any]) -> None: ...

    async def send_bytes(self, payload: bytes) -> None: ...

    async def close(self, code: int, reason: str) -> None: ...

    @property
    def is_open(self) -> bool: ...


@dataclass
class User:
    user_id: str
    display_name: str
    session_token: str
    # The Yjs clientID handed to the browser. Equal to the room's join sequence
    # number, so join order breaks CRDT insertion ties (spec section 7.1).
    client_id: int
    joined_at: int
    connection: PeerConnection | None = None
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    # Retained so a reconnect can cancel the pending removal.
    disconnect_timer: Any = None
    last_pong_at: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "userId": self.user_id,
            "displayName": self.display_name,
            "clientId": self.client_id,
            "joinedAt": self.joined_at,
            "connected": self.connection_state is ConnectionState.CONNECTED,
        }


@dataclass
class DocumentRecord:
    document_id: str
    name: str
    created_at: int
    create_seq: int  # Disambiguates same-named documents by creation order.

    def public(self) -> dict[str, Any]:
        return {
            "documentId": self.document_id,
            "name": self.name,
            "createdAt": self.created_at,
            "createSeq": self.create_seq,
        }


@dataclass
class FileRecord:
    file_id: str
    original_name: str
    size: int
    uploader_id: str
    # Snapshotted at upload time, not a live reference (spec section 13).
    uploader_name: str
    uploaded_at: int

    def public(self) -> dict[str, Any]:
        return {
            "fileId": self.file_id,
            "name": self.original_name,
            "size": self.size,
            "uploaderId": self.uploader_id,
            "uploaderName": self.uploader_name,
            "uploadedAt": self.uploaded_at,
        }


@dataclass
class UploadRecord:
    upload_id: str
    file_id: str
    room_code: str
    room_instance_id: str
    filename: str
    declared_size: int
    uploader_id: str
    uploader_name: str
    reservation_bytes: int
    committed_offset: int = 0
    last_activity_at: int = 0
    aborted: bool = False
    bytes_since_recheck: int = 0
    # Set when a chunk write is in progress, so the reaper does not delete a
    # part file out from under an active transfer.
    writing: bool = False


@dataclass
class RoomSnapshot:
    """What a joining client needs to render the room in one shot."""

    users: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
