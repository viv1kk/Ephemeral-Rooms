"""Outbound event constructors.

Kept in one place so the wire vocabulary is readable end to end and the
frontend has a single file to mirror. User-facing strings here are plain
language: no stack traces, no filesystem paths, no internal identifiers beyond
the room code and the names the user can already see (spec section 32).
"""

from __future__ import annotations

from typing import Any

from app.rooms.types import DocumentRecord, FileRecord, User
from app.util.names import color_for


def joined(
    *,
    user: User,
    room_code: str,
    room_snapshot: dict[str, Any],
    resumed: bool,
    room_was_created: bool,
    storage_available: int,
    limits: dict[str, int],
) -> dict[str, Any]:
    """The join acknowledgement.

    This must reach the client BEFORE any sync traffic, because the browser
    needs `clientId` to construct its Y.Doc with the right tie-break identity
    (spec section 7.1)."""
    return {
        "type": "joined",
        "userId": user.user_id,
        "clientId": user.client_id,
        "sessionToken": user.session_token,
        "displayName": user.display_name,
        "color": color_for(user.user_id),
        "roomCode": room_code,
        "resumed": resumed,
        # Drives the one-time "this room was empty, so a new one was created"
        # notice, which is how a mistyped code becomes visible (section 26).
        "roomWasCreated": room_was_created,
        "room": room_snapshot,
        "storageAvailable": storage_available,
        "limits": limits,
    }


def user_joined(user: User) -> dict[str, Any]:
    return {"type": "user_joined", "user": user.public(), "color": color_for(user.user_id)}


def user_left(user: User) -> dict[str, Any]:
    return {"type": "user_left", "userId": user.user_id, "displayName": user.display_name}


def user_updated(user: User, op_seq: int) -> dict[str, Any]:
    return {"type": "user_updated", "user": user.public(), "opSeq": op_seq}


def presence(users: list[User]) -> dict[str, Any]:
    return {
        "type": "presence",
        "users": [{**u.public(), "color": color_for(u.user_id)} for u in users],
    }


def document_created(record: DocumentRecord, op_seq: int) -> dict[str, Any]:
    return {"type": "document_created", "document": record.public(), "opSeq": op_seq}


def document_renamed(record: DocumentRecord, op_seq: int) -> dict[str, Any]:
    return {
        "type": "document_renamed",
        "documentId": record.document_id,
        "name": record.name,
        "opSeq": op_seq,
    }


def document_deleted(record: DocumentRecord, by_name: str, op_seq: int) -> dict[str, Any]:
    return {
        "type": "document_deleted",
        "documentId": record.document_id,
        "name": record.name,
        "deletedBy": by_name,
        "opSeq": op_seq,
    }


def file_added(record: FileRecord) -> dict[str, Any]:
    return {"type": "file_added", "file": record.public()}


def file_deleted(file_id: str, name: str, by_name: str) -> dict[str, Any]:
    return {"type": "file_deleted", "fileId": file_id, "name": name, "deletedBy": by_name}


def upload_progress(*, upload_id: str, uploader_name: str, percent: int, filename: str) -> dict[str, Any]:
    return {
        "type": "upload_progress",
        "uploadId": upload_id,
        "uploaderName": uploader_name,
        "filename": filename,
        "percent": percent,
    }


def storage(available: int) -> dict[str, Any]:
    return {"type": "storage", "available": available}


def ping(now_ms: int) -> dict[str, Any]:
    return {"type": "ping", "t": now_ms}


def room_closing(reason: str) -> dict[str, Any]:
    return {"type": "room_closing", "reason": reason}


def error(code: str, message: str, *, correlation_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "error", "code": code, "message": message}
    if correlation_id is not None:
        # An opaque reference the user can quote; the detail stays in the log.
        payload["reference"] = correlation_id
    return payload
