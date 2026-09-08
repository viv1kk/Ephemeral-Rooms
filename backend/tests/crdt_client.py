"""A test client that speaks the room's binary frame protocol.

It holds its own CRDT replica with its own clientID, exactly as a browser does,
and exchanges frames with a `Session`. Two of these are two genuinely
independent replicas, which is what the concurrency tests require; one client
sending two messages would pass while the real behaviour was broken
(spec section 37.1).

`online` models the network. Setting it False buffers outbound frames so a test
can create real concurrency - both replicas editing without having seen each
other - and then deliver the backlog in any order it likes.
"""

from __future__ import annotations

from pycrdt import Doc, Text, create_update_message

from app.collab import sync
from app.collab.registry import TEXT_KEY
from app.ws.connection import Session


class CrdtClient:
    def __init__(self, session: Session, document_id: str, client_id: int) -> None:
        self.session = session
        self.document_id = document_id
        # The tie-breaking clientID lives on the *authoring* replica, which in
        # production is the browser (spec section 7.1).
        self.doc = Doc(client_id=client_id)
        self.doc[TEXT_KEY] = Text()
        self.online = True
        self.outbox: list[bytes] = []
        self._last_sent_state = self.doc.get_state()
        self._sub = self.doc.observe(self._on_update)

    @property
    def text(self) -> str:
        return str(self.doc[TEXT_KEY])

    @property
    def client_id(self) -> int:
        return int(self.doc.client_id)

    def _on_update(self, event) -> None:  # type: ignore[no-untyped-def]
        self.outbox.append(sync.update_frame(self.document_id, event.update))

    # -- editing -----------------------------------------------------------

    def insert(self, index: int, value: str) -> None:
        self.doc[TEXT_KEY].insert(index, value)

    def delete(self, index: int, length: int) -> None:
        del self.doc[TEXT_KEY][index : index + length]

    # -- network -----------------------------------------------------------

    async def flush(self, *, reverse: bool = False, duplicate: bool = False) -> None:
        """Deliver buffered updates to the server.

        `reverse` delivers them out of order and `duplicate` sends each twice;
        a CRDT must converge under both (spec section 6)."""
        frames = list(reversed(self.outbox)) if reverse else list(self.outbox)
        self.outbox.clear()
        for frame in frames:
            await self.session.handle_binary(frame)
            if duplicate:
                await self.session.handle_binary(frame)

    async def receive(self, frame: bytes) -> None:
        """Apply a frame the server relayed to this client."""
        document_id, payload = sync.decode_frame(frame)
        if document_id != self.document_id:
            return
        if payload[0] != 0:  # not a SYNC message
            return
        kind = payload[1]
        body = payload[2:]
        from pycrdt import read_message

        if kind in (1, 2):  # SYNC_STEP2 or SYNC_UPDATE
            update = read_message(body)
            if update != b"\x00\x00":
                self.doc.apply_update(update)

    async def sync_with_server(self, server_doc: Doc) -> None:
        """Two-way state-vector exchange, as the browser does on join."""
        # Server -> client: everything the client is missing.
        self.doc.apply_update(server_doc.get_update(self.doc.get_state()))
        # Client -> server: everything the server is missing.
        update = self.doc.get_update(server_doc.get_state())
        if update != b"\x00\x00":
            await self.session.handle_binary(
                sync.encode_frame(self.document_id, create_update_message(update))
            )


async def deliver_all(clients: list[CrdtClient], connections: list) -> None:
    """Route every frame the server broadcast back into the right replicas.

    Mirrors what each browser's WebSocket would do, so convergence is asserted
    across genuinely separate replicas rather than one shared object."""
    for client, conn in zip(clients, connections):
        frames = list(conn.binary_frames)
        conn.binary_frames.clear()
        for frame in frames:
            await client.receive(frame)
