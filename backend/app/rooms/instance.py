"""RoomInstance: the lifecycle state machine (spec section 2.1).

The one boundary worth understanding before changing anything here is
EMPTY_GRACE vs CLOSING:

  * EMPTY_GRACE  - zero connected users, but every document, file and session
                   token is still in memory. A join returns the user to the
                   SAME instance with state intact. This is what makes browser
                   refresh safe, including for a sole participant.
  * CLOSING      - irreversible. The manager has already dropped the code, so
                   nothing can find this instance any more; a user arriving at
                   the same code gets a brand-new instance with a different
                   roomInstanceId and a different directory. That disjointness
                   is what makes the section 34 race harmless rather than
                   merely unlikely.

Crossing that boundary is a one-way door, and it happens in exactly one place:
`_on_empty_grace_elapsed`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from app.collab.registry import DocumentRegistry
from app.rooms.types import (
    CloseCode,
    ConnectionState,
    FileRecord,
    PeerConnection,
    RoomState,
    UploadRecord,
    User,
)
from app.storage.protocols import Clock
from app.util.ids import new_session_token, new_uuid
from app.util.names import random_display_name

log = logging.getLogger(__name__)


class RoomFullError(Exception):
    pass


class RoomNotJoinableError(Exception):
    """The instance has passed the CLOSING boundary and can never accept a join."""


class RoomInstance:
    def __init__(
        self,
        *,
        room_code: str,
        clock: Clock,
        settings: Any,
        on_closing: Callable[["RoomInstance"], Awaitable[None]],
    ) -> None:
        self.room_code = room_code
        self.room_instance_id = new_uuid()
        self.clock = clock
        self.settings = settings
        self.created_at = clock.now_ms()
        self.state = RoomState.ACTIVE

        # Monotonic counters. op_seq totally orders scalar metadata writes
        # (section 7.2); join_seq becomes each browser's Yjs clientID and so
        # decides CRDT insertion ties (section 7.1). Neither is ever reset
        # while the instance lives.
        self.op_seq = 0
        self.join_seq = 0

        self.users: dict[str, User] = {}
        self.documents = DocumentRegistry()
        self.files: dict[str, FileRecord] = {}
        self.uploads: dict[str, UploadRecord] = {}
        # Server-side awareness, one per document; see collab/sync.py.
        self.awareness: dict[str, Any] = {}
        self.created_by_url_visit = False

        self._empty_grace_timer: Any = None
        self._on_closing = on_closing
        self._closed = asyncio.Event()

    # ------------------------------------------------------------------
    # Sequencing
    # ------------------------------------------------------------------

    def next_op_seq(self) -> int:
        """Stamp a metadata mutation. Assigned in the order the server
        processes messages, so it is total and ties are impossible."""
        self.op_seq += 1
        return self.op_seq

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    @property
    def connected_users(self) -> list[User]:
        return [u for u in self.users.values() if u.connection_state is ConnectionState.CONNECTED]

    @property
    def is_joinable(self) -> bool:
        return self.state in (RoomState.ACTIVE, RoomState.EMPTY_GRACE)

    def find_by_token(self, session_token: str) -> User | None:
        if not session_token:
            return None
        for user in self.users.values():
            if user.session_token == session_token:
                return user
        return None

    def join(
        self,
        *,
        connection: PeerConnection,
        session_token: str | None,
        requested_name: str | None = None,
    ) -> tuple[User, bool]:
        """Attach a connection. Returns (user, resumed).

        A valid token rebinds the existing identity: same userId, same display
        name, and critically the same clientID, so a reconnecting user keeps
        their position in the CRDT tie-break ordering (spec section 4.1). An
        invalid or expired token is never an error; the user simply joins as
        new.
        """
        if not self.is_joinable:
            raise RoomNotJoinableError(self.room_code)

        # A join during EMPTY_GRACE resurrects the room. Cancel the closing
        # timer before anything else so the room cannot be torn down underneath
        # the user we are about to admit.
        self._cancel_empty_grace()

        existing = self.find_by_token(session_token or "")
        if existing is not None:
            if existing.disconnect_timer is not None:
                existing.disconnect_timer.cancel()
                existing.disconnect_timer = None
            # A second socket presenting the same token supersedes the first.
            previous = existing.connection
            existing.connection = connection
            existing.connection_state = ConnectionState.CONNECTED
            existing.last_pong_at = self.clock.now_ms()
            if previous is not None and previous is not connection and previous.is_open:
                asyncio.create_task(
                    _safe_close(previous, CloseCode.REPLACED_BY_RESUME, "resumed elsewhere")
                )
            self.state = RoomState.ACTIVE
            return existing, True

        if len(self.connected_users) >= self.settings.MAX_USERS_PER_ROOM:
            raise RoomFullError(self.room_code)

        self.join_seq += 1
        user = User(
            user_id=new_uuid(),
            display_name=requested_name or random_display_name(),
            session_token=new_session_token(),
            client_id=self.join_seq,
            joined_at=self.clock.now_ms(),
            connection=connection,
            connection_state=ConnectionState.CONNECTED,
            last_pong_at=self.clock.now_ms(),
        )
        self.users[user.user_id] = user
        self.state = RoomState.ACTIVE
        return user, False

    def mark_disconnected(
        self,
        user: User,
        *,
        connection: PeerConnection,
        on_removed: Callable[[User], Awaitable[None]],
    ) -> None:
        """Socket gone without an explicit leave. Identity is retained for the
        grace window; awareness is dropped immediately by the caller so a stale
        cursor does not linger.

        `connection` is the socket that actually died. It matters because a
        resume can overtake it: the browser opens a new socket and rebinds this
        same user before the old socket's close is processed. Acting on that
        stale close would null out the live connection and silently stop the
        user receiving broadcasts, so a superseded socket is ignored.
        """
        if user.user_id not in self.users:
            return
        if user.connection is not None and user.connection is not connection:
            return
        user.connection = None
        user.connection_state = ConnectionState.DISCONNECTED

        async def _remove() -> None:
            current = self.users.get(user.user_id)
            if current is None or current.connection_state is ConnectionState.CONNECTED:
                return
            self.users.pop(user.user_id, None)
            await on_removed(current)
            self._maybe_start_empty_grace()

        if user.disconnect_timer is not None:
            user.disconnect_timer.cancel()
        user.disconnect_timer = self.clock.schedule(
            self.settings.USER_RECONNECT_GRACE_MS, _remove
        )
        self._maybe_start_empty_grace()

    def remove_user(self, user_id: str) -> User | None:
        """Explicit departure: no grace at all (spec section 27)."""
        user = self.users.pop(user_id, None)
        if user is not None and user.disconnect_timer is not None:
            user.disconnect_timer.cancel()
            user.disconnect_timer = None
        self._maybe_start_empty_grace()
        return user

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _maybe_start_empty_grace(self) -> None:
        """A room enters EMPTY_GRACE on zero CONNECTED users, regardless of how
        many are still inside their own reconnect grace (spec section 2.1).
        """
        if self.state is not RoomState.ACTIVE:
            return
        if self.connected_users:
            return
        self.state = RoomState.EMPTY_GRACE
        self._empty_grace_timer = self.clock.schedule(
            self.settings.ROOM_EMPTY_GRACE_MS, self._on_empty_grace_elapsed
        )

    def _cancel_empty_grace(self) -> None:
        if self._empty_grace_timer is not None:
            self._empty_grace_timer.cancel()
            self._empty_grace_timer = None

    async def _on_empty_grace_elapsed(self) -> None:
        # The one-way door. Anything that could still return the room to ACTIVE
        # must have cancelled this timer before now.
        if self.state is not RoomState.EMPTY_GRACE:
            return
        self._empty_grace_timer = None
        self.state = RoomState.CLOSING
        await self._on_closing(self)

    def mark_closed(self) -> None:
        self.state = RoomState.CLOSED
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def cancel_all_timers(self) -> None:
        self._cancel_empty_grace()
        for user in self.users.values():
            if user.disconnect_timer is not None:
                user.disconnect_timer.cancel()
                user.disconnect_timer = None

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def broadcast_json(self, payload: dict[str, Any], *, exclude: str | None = None) -> None:
        await asyncio.gather(
            *(
                _safe_send_json(u.connection, payload)
                for u in self.connected_users
                if u.user_id != exclude and u.connection is not None
            ),
            return_exceptions=True,
        )

    async def broadcast_bytes(self, payload: bytes, *, exclude: str | None = None) -> None:
        await asyncio.gather(
            *(
                _safe_send_bytes(u.connection, payload)
                for u in self.connected_users
                if u.user_id != exclude and u.connection is not None
            ),
            return_exceptions=True,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "roomCode": self.room_code,
            "users": [u.public() for u in self.users.values()],
            "documents": [d.public() for d in self.documents.records],
            "files": [f.public() for f in self.files.values()],
        }


async def _safe_send_json(conn: PeerConnection | None, payload: dict[str, Any]) -> None:
    if conn is None or not conn.is_open:
        return
    try:
        await conn.send_json(payload)
    except Exception:  # a dead socket must not abort the broadcast to everyone else
        log.debug("dropped json frame to a closed connection", exc_info=True)


async def _safe_send_bytes(conn: PeerConnection | None, payload: bytes) -> None:
    if conn is None or not conn.is_open:
        return
    try:
        await conn.send_bytes(payload)
    except Exception:
        log.debug("dropped binary frame to a closed connection", exc_info=True)


async def _safe_close(conn: PeerConnection, code: int, reason: str) -> None:
    try:
        await conn.close(code, reason)
    except Exception:
        log.debug("close failed on an already-dead connection", exc_info=True)
