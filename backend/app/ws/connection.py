"""The per-connection session: one browser, one socket, one room.

Transport-agnostic on purpose. It talks to a `PeerConnection`, so the same
class is driven by a real Starlette WebSocket in production and by a recording
fake in tests, which is what lets two-user concurrency tests use two genuinely
independent sessions rather than one client sending two messages
(spec section 37.1).
"""

from __future__ import annotations

import logging
from typing import Any

from pycrdt import YMessageType

from app.collab import sync
from app.deps import Services, client_limits
from app.rooms.instance import RoomFullError, RoomInstance, RoomNotJoinableError
from app.rooms.manager import NoCodesAvailableError
from app.rooms.types import CloseCode, PeerConnection, User
from app.util.ids import is_uuid, new_uuid
from app.util.sanitize import clean_display_name, clean_document_name
from app.ws import events, messages

log = logging.getLogger(__name__)


class Session:
    def __init__(self, *, connection: PeerConnection, services: Services) -> None:
        self.conn = connection
        self.svc = services
        self.settings = services.settings
        self.room: RoomInstance | None = None
        self.user: User | None = None
        # Documents this socket has subscribed to, so update relays are not
        # sent to a client that has never synced the document.
        self.subscribed: set[str] = set()
        self._left = False

    # ------------------------------------------------------------------
    # Join
    # ------------------------------------------------------------------

    async def handle_join(self, msg: messages.JoinMessage) -> bool:
        try:
            room, created = await self.svc.manager.get_or_create(msg.roomCode)
        except NoCodesAvailableError:
            await self.conn.send_json(
                events.error("at_capacity", "The server is at capacity. Please try again shortly.")
            )
            return False

        try:
            user, resumed = room.join(connection=self.conn, session_token=msg.sessionToken)
        except RoomFullError:
            await self.conn.send_json(
                events.error("room_full", "This room is full. Please try another.")
            )
            await self.conn.close(CloseCode.ROOM_FULL, "room full")
            return False
        except RoomNotJoinableError:
            # The instance crossed the CLOSING boundary between lookup and
            # join. Retrying gets a brand-new instance, which is the defined
            # behaviour (spec section 34).
            room, created = await self.svc.manager.get_or_create(msg.roomCode)
            user, resumed = room.join(connection=self.conn, session_token=msg.sessionToken)

        self.room = room
        self.user = user

        if not room.documents:
            # A room always opens with something to type in.
            room.documents.create(new_uuid(), "untitled.txt", self.svc.clock.now_ms())

        available = await self.svc.ledger.available_for_new_upload()

        # The acknowledgement must land before any sync traffic: the browser
        # needs clientId to construct its Y.Doc (spec section 7.1).
        await self.conn.send_json(
            events.joined(
                user=user,
                room_code=room.room_code,
                room_snapshot=room.snapshot(),
                resumed=resumed,
                room_was_created=created and room.created_by_url_visit,
                storage_available=available,
                limits=client_limits(self.settings),
            )
        )
        room.created_by_url_visit = False

        if not resumed:
            await room.broadcast_json(events.user_joined(user), exclude=user.user_id)
        else:
            await room.broadcast_json(events.presence(list(room.users.values())))
        return True

    # ------------------------------------------------------------------
    # Control messages
    # ------------------------------------------------------------------

    async def handle_message(self, msg: Any) -> bool:
        """Dispatch one validated control message. Returns False to end the
        session (an explicit leave)."""
        room, user = self.room, self.user
        if room is None or user is None:
            return True

        match msg:
            case messages.PongMessage():
                user.last_pong_at = self.svc.clock.now_ms()

            case messages.LeaveMessage():
                await self.leave(explicit=True)
                return False

            case messages.SetNameMessage():
                name = clean_display_name(
                    msg.displayName,
                    max_chars=self.settings.MAX_DISPLAY_NAME_CHARS,
                    fallback=user.display_name,
                )
                user.display_name = name
                await room.broadcast_json(events.user_updated(user, room.next_op_seq()))

            case messages.SubscribeDocumentMessage():
                await self._subscribe(room, msg.documentId)

            case messages.CreateDocumentMessage():
                await self._create_document(room, msg.name)

            case messages.RenameDocumentMessage():
                await self._rename_document(room, msg.documentId, msg.name)

            case messages.DeleteDocumentMessage():
                await self._delete_document(room, user, msg.documentId)

            case messages.DeleteFileMessage():
                await self._delete_file(room, user, msg.fileId)

            case messages.UploadProgressMessage():
                upload = room.uploads.get(msg.uploadId)
                if upload is not None and upload.uploader_id == user.user_id:
                    await room.broadcast_json(
                        events.upload_progress(
                            upload_id=upload.upload_id,
                            uploader_name=upload.uploader_name,
                            filename=upload.filename,
                            percent=msg.percent,
                        ),
                        exclude=user.user_id,
                    )
        return True

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    async def _subscribe(self, room: RoomInstance, document_id: str) -> None:
        if not is_uuid(document_id) or document_id not in room.documents:
            await self.conn.send_json(
                events.error("no_document", "That document is no longer available.")
            )
            return
        doc = room.documents.doc(document_id)
        if doc is None:
            return
        self.subscribed.add(document_id)
        # A state-vector diff, not a replay of the room's whole history.
        await self.conn.send_bytes(sync.initial_sync_frame(document_id, doc))
        awareness = room.awareness.get(document_id)
        if awareness is not None:
            frame = sync.awareness_frame(
                document_id, awareness, list(awareness.states.keys())
            )
            if frame is not None:
                await self.conn.send_bytes(frame)

    async def _create_document(self, room: RoomInstance, raw_name: str) -> None:
        if len(room.documents) >= self.settings.MAX_DOCS_PER_ROOM:
            await self.conn.send_json(
                events.error(
                    "too_many_documents",
                    "This room already holds the maximum number of documents.",
                )
            )
            return
        name = clean_document_name(raw_name, max_chars=self.settings.MAX_DOC_NAME_CHARS)
        record = room.documents.create(new_uuid(), name, self.svc.clock.now_ms())
        await room.broadcast_json(events.document_created(record, room.next_op_seq()))

    async def _rename_document(self, room: RoomInstance, document_id: str, raw_name: str) -> None:
        if not is_uuid(document_id):
            return
        name = clean_document_name(raw_name, max_chars=self.settings.MAX_DOC_NAME_CHARS)
        # Last write wins by opSeq. The counter is stamped in the order the
        # server processes messages, so ties are impossible (spec section 7.2).
        record = room.documents.rename(document_id, name)
        if record is None:
            return
        await room.broadcast_json(events.document_renamed(record, room.next_op_seq()))

    async def _delete_document(self, room: RoomInstance, user: User, document_id: str) -> None:
        if not is_uuid(document_id):
            return
        record = room.documents.delete(document_id)
        if record is None:
            return
        room.awareness.pop(document_id, None)
        # Delete wins over a concurrent edit; in-flight updates for this
        # document are discarded on arrival (spec section 8.1).
        await room.broadcast_json(
            events.document_deleted(record, user.display_name, room.next_op_seq())
        )

    async def _delete_file(self, room: RoomInstance, user: User, file_id: str) -> None:
        if not is_uuid(file_id):
            return
        record = room.files.pop(file_id, None)
        if record is None:
            return
        # Any download already streaming keeps its open descriptor and finishes
        # normally (spec section 8.1).
        await self.svc.file_store.delete_file(room.room_instance_id, file_id)
        await room.broadcast_json(
            events.file_deleted(file_id, record.original_name, user.display_name)
        )

    # ------------------------------------------------------------------
    # Binary frames
    # ------------------------------------------------------------------

    async def handle_binary(self, frame: bytes) -> None:
        """Relay a Yjs sync or awareness frame.

        Dispatched by channel tag before any JSON parsing is attempted, and
        handed to the CRDT layer as raw bytes (spec section 5)."""
        room, user = self.room, self.user
        if room is None or user is None:
            return
        if len(frame) > self.settings.MAX_WS_MESSAGE_BYTES:
            await self.conn.send_json(
                events.error("message_too_large", "That update was too large and was ignored.")
            )
            return
        try:
            document_id, payload = sync.decode_frame(frame)
            kind = sync.message_kind(payload)
        except (sync.FrameError, ValueError):
            log.debug("dropped an undecodable binary frame")
            return

        doc = room.documents.doc(document_id)
        if doc is None:
            # The document was deleted while this update was in flight.
            return

        if kind == YMessageType.SYNC:
            before = doc.get_state()
            reply = sync.apply_sync(document_id, doc, payload)
            if reply is not None:
                await self.conn.send_bytes(reply)
            if room.documents.text_length(document_id) > self.settings.MAX_DOC_BYTES:
                await self.conn.send_json(
                    events.error(
                        "document_too_large",
                        "This document has reached its maximum size.",
                    )
                )
            # Relay only what actually changed the server's replica, so
                # duplicated and out-of-order frames cost nothing.
            update = doc.get_update(before)
            if update and update != b"\x00\x00":
                await room.broadcast_bytes(
                    sync.update_frame(document_id, update), exclude=user.user_id
                )
        else:
            awareness = room.awareness.get(document_id)
            if awareness is None:
                awareness = sync.new_awareness(doc)
                room.awareness[document_id] = awareness
            try:
                sync.apply_awareness(awareness, payload)
            except Exception:
                log.debug("dropped a malformed awareness frame")
                return
            await room.broadcast_bytes(frame, exclude=user.user_id)

    # ------------------------------------------------------------------
    # Departure
    # ------------------------------------------------------------------

    async def leave(self, *, explicit: bool) -> None:
        """Explicit departure removes the user immediately with no grace; a
        dropped socket retains them for USER_RECONNECT_GRACE_MS so a brief
        blip does not produce a spurious leave/join pair (spec sections 3, 27).
        """
        if self._left:
            return
        self._left = True
        room, user = self.room, self.user
        if room is None or user is None:
            return

        # Awareness is cleared immediately either way, so a stale cursor never
        # lingers behind a disconnected user.
        self._clear_awareness(room, user)

        # If this user has already been rebound to a newer socket, this session
        # is stale and must not act on their behalf (see mark_disconnected).
        superseded = user.connection is not None and user.connection is not self.conn

        if explicit:
            if superseded:
                return
            removed = room.remove_user(user.user_id)
            if removed is not None:
                await room.broadcast_json(events.user_left(removed))
        else:
            async def _on_removed(gone: User) -> None:
                await room.broadcast_json(events.user_left(gone))

            room.mark_disconnected(user, connection=self.conn, on_removed=_on_removed)
            if not superseded:
                await room.broadcast_json(events.presence(list(room.users.values())))

    def _clear_awareness(self, room: RoomInstance, user: User) -> None:
        for awareness in room.awareness.values():
            try:
                awareness.remove_awareness_states([user.client_id], "disconnect")
            except Exception:
                log.debug("failed to clear awareness for a departing user", exc_info=True)
