"""Collaboration tests (spec section 37, Collaboration).

These exercise the server's relay and its own replica against genuinely
independent client replicas. The browser-side half of the guarantee - that
JavaScript `yjs` agrees with Python `pycrdt` on all of this - is proven
separately by the Playwright suite, because a pure-Python test of pycrdt
against itself proves nothing about the wire format (spec section 0.1).
"""

from __future__ import annotations

import pytest

from app.collab.registry import TEXT_KEY
from app.deps import Services
from app.ws.messages import (
    CreateDocumentMessage,
    DeleteDocumentMessage,
    RenameDocumentMessage,
    SubscribeDocumentMessage,
)
from tests.crdt_client import CrdtClient, deliver_all

pytestmark = pytest.mark.asyncio


async def _room_with(services: Services, make_peer, n: int):
    room = await services.manager.create()
    peers = [await make_peer(room.room_code, name=f"p{i}") for i in range(n)]
    document_id = room.documents.records[0].document_id
    for p in peers:
        await p.session.handle_message(
            SubscribeDocumentMessage(type="subscribe_document", documentId=document_id)
        )
        p.conn.binary_frames.clear()
    clients = [CrdtClient(p.session, document_id, p.client_id) for p in peers]
    return room, peers, clients, document_id


async def _converge(room, document_id, peers, clients, rounds: int = 4) -> None:
    for _ in range(rounds):
        for c in clients:
            await c.flush()
        await deliver_all(clients, [p.conn for p in peers])


async def test_single_user_editing_reaches_the_server_replica(
    services: Services, make_peer
) -> None:
    room, peers, clients, document_id = await _room_with(services, make_peer, 1)
    clients[0].insert(0, "hello world")
    await clients[0].flush()
    assert room.documents.text(document_id) == "hello world"


async def test_two_user_editing_converges(services: Services, make_peer) -> None:
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    a, b = clients

    a.insert(0, "alpha ")
    await a.flush()
    await deliver_all(clients, [p.conn for p in peers])

    b.insert(len(b.text), "beta")
    await b.flush()
    await deliver_all(clients, [p.conn for p in peers])

    assert a.text == b.text == room.documents.text(document_id) == "alpha beta"


async def test_simultaneous_insertion_at_the_same_position_preserves_both(
    services: Services, make_peer
) -> None:
    """The central guarantee: no user's typing is discarded (spec section 7.1)."""
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    a, b = clients

    # Genuinely concurrent: neither replica has seen the other's edit.
    a.online = b.online = False
    a.insert(0, "AAA")
    b.insert(0, "BBB")

    await _converge(room, document_id, peers, clients)

    final = room.documents.text(document_id)
    assert "AAA" in final and "BBB" in final, f"an insertion was lost: {final!r}"
    assert a.text == b.text == final, "replicas diverged"
    assert len(final) == 6


async def test_the_join_order_tie_break_direction_is_asserted_not_assumed(
    services: Services, make_peer
) -> None:
    """Spec section 7.1 obliges us to assert the observed direction rather than
    recall it, and to document it in the README.

    OBSERVED RULE, pycrdt 0.14.4 / yjs 13.6.32: at a genuinely identical
    insertion point, the LOWER clientID lands to the LEFT. Because the server
    assigns clientID from the room's join sequence, the EARLIER joiner's text
    appears first. The same direction is asserted against a real browser in the
    Playwright suite, which is what proves the two implementations agree.

    Note what this test is careful to avoid: if one client has already received
    the other's insertion, the second insertion is causally *after* the first
    and is ordered by position, with no tie-break involved at all. Both
    replicas must therefore be edited before either update is exchanged."""
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    first_joiner, second_joiner = clients
    assert first_joiner.client_id == 1 and second_joiner.client_id == 2

    # Neither has seen the other; this is the tie-break case.
    first_joiner.insert(0, "AAA")
    second_joiner.insert(0, "BBB")
    await _converge(room, document_id, peers, clients)

    assert room.documents.text(document_id) == "AAABBB"
    assert first_joiner.text == second_joiner.text == "AAABBB"


async def test_a_causally_later_insertion_is_ordered_by_position_not_the_tie_break(
    services: Services, make_peer
) -> None:
    """The companion to the test above, and the reason it must partition the
    network: once B has seen A's edit, B inserting at index 0 lands to the left
    purely by position, which looks like the opposite tie-break direction."""
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    a, b = clients

    a.insert(0, "AAA")
    await _converge(room, document_id, peers, clients)
    assert b.text == "AAA", "B must have seen A's edit for this to be causal"

    b.insert(0, "BBB")
    await _converge(room, document_id, peers, clients)

    assert room.documents.text(document_id) == "BBBAAA"


async def test_concurrent_overlapping_deletion_is_idempotent(
    services: Services, make_peer
) -> None:
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    a, b = clients
    a.insert(0, "abcdefghij")
    await _converge(room, document_id, peers, clients)
    assert a.text == b.text == "abcdefghij"

    # Overlapping ranges: a deletes 2-6, b deletes 4-8.
    a.delete(2, 4)
    b.delete(4, 4)
    await _converge(room, document_id, peers, clients)

    final = room.documents.text(document_id)
    # A character deleted by both is deleted once, not twice.
    assert final == "abij"
    assert a.text == b.text == final


async def test_insertion_into_a_concurrently_deleted_range_survives(
    services: Services, make_peer
) -> None:
    """A character inserted concurrently with the deletion of its surrounding
    range is not swept up by a deletion that did not causally know about it
    (spec section 7.1 rule 3)."""
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    a, b = clients
    a.insert(0, "abcdefgh")
    await _converge(room, document_id, peers, clients)

    b.delete(2, 4)          # b removes "cdef"
    a.insert(4, "-NEW-")    # a types inside the range b is removing

    await _converge(room, document_id, peers, clients)

    final = room.documents.text(document_id)
    assert "-NEW-" in final, f"a concurrent insertion was swept up by a deletion: {final!r}"
    assert "cdef" not in final
    assert a.text == b.text == final


async def test_out_of_order_delivery_converges(services: Services, make_peer) -> None:
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    a, b = clients

    a.insert(0, "one")
    a.insert(3, "two")
    a.insert(6, "three")
    await a.flush(reverse=True)  # server receives the updates backwards

    await deliver_all(clients, [p.conn for p in peers])
    assert room.documents.text(document_id) == "onetwothree"
    assert b.text == "onetwothree"


async def test_duplicate_delivery_is_harmless(services: Services, make_peer) -> None:
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    a, b = clients

    a.insert(0, "once")
    await a.flush(duplicate=True)  # every update sent twice
    await deliver_all(clients, [p.conn for p in peers])

    assert room.documents.text(document_id) == "once"
    assert b.text == "once"


async def test_a_client_reconnecting_after_missing_updates_catches_up(
    services: Services, make_peer
) -> None:
    """A reconnecting client syncs against the server's replica with a
    state-vector diff, not a replay of the whole history (spec section 6)."""
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    a, b = clients

    # b is offline and misses everything.
    b.online = False
    for word in ("alpha ", "beta ", "gamma ", "delta"):
        a.insert(len(a.text), word)
    await a.flush()
    peers[1].conn.binary_frames.clear()

    server_doc = room.documents.doc(document_id)
    assert server_doc is not None
    await b.sync_with_server(server_doc)

    assert b.text == a.text == "alpha beta gamma delta"


async def test_three_clients_converge_from_divergent_states(
    services: Services, make_peer
) -> None:
    room, peers, clients, document_id = await _room_with(services, make_peer, 3)
    a, b, c = clients

    a.insert(0, "aaa")
    b.insert(0, "bbb")
    c.insert(0, "ccc")

    await _converge(room, document_id, peers, clients, rounds=6)

    final = room.documents.text(document_id)
    assert sorted(final) == sorted("aaabbbccc")
    assert a.text == b.text == c.text == final


async def test_the_server_holds_a_real_document_not_opaque_bytes(
    services: Services, make_peer
) -> None:
    """The server must be able to read the text, which is what lets it serve
    state-vector diffs and report a document's real size (spec section 36)."""
    room, peers, clients, document_id = await _room_with(services, make_peer, 1)
    clients[0].insert(0, "measurable")
    await clients[0].flush()

    doc = room.documents.doc(document_id)
    assert doc is not None
    assert str(doc[TEXT_KEY]) == "measurable"
    assert room.documents.text_length(document_id) == len("measurable")


async def test_an_oversized_document_is_reported_to_the_author(
    services: Services, settings, make_peer
) -> None:
    """Only when an operator has set a cap - it is 0, meaning no limit, by
    default. What bounds a document otherwise is memory headroom; see
    tests/test_headroom.py."""
    settings.MAX_DOC_BYTES = 32
    room, peers, clients, document_id = await _room_with(services, make_peer, 1)

    clients[0].insert(0, "x" * 100)
    await clients[0].flush()

    error = peers[0].conn.last("error")
    assert error is not None and error["code"] == "document_too_large"


# ----------------------------------------------------------------------
# Document management (server-authoritative, not a CRDT)
# ----------------------------------------------------------------------


async def test_metadata_conflicts_resolve_by_op_seq_last_write_winning(
    services: Services, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)
    document_id = room.documents.records[0].document_id

    await a.session.handle_message(
        RenameDocumentMessage(type="rename_document", documentId=document_id, name="from-a.py")
    )
    await b.session.handle_message(
        RenameDocumentMessage(type="rename_document", documentId=document_id, name="from-b.py")
    )

    renames = a.conn.events_of("document_renamed")
    assert [r["name"] for r in renames] == ["from-a.py", "from-b.py"]
    # Strictly increasing, assigned in server processing order, so ties are
    # impossible by construction (spec section 7.2).
    assert renames[1]["opSeq"] > renames[0]["opSeq"]
    # Everyone converges on the higher-sequence name.
    assert room.documents.get(document_id).name == "from-b.py"


async def test_two_documents_may_share_a_name_and_stay_distinct(
    services: Services, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)

    await a.session.handle_message(CreateDocumentMessage(type="create_document", name="main.py"))
    await a.session.handle_message(CreateDocumentMessage(type="create_document", name="main.py"))

    named = [d for d in room.documents.records if d.name == "main.py"]
    assert len(named) == 2
    assert named[0].document_id != named[1].document_id
    # The UI disambiguates by creation order (spec section 8.1).
    assert named[0].create_seq < named[1].create_seq


async def test_delete_wins_over_a_concurrent_edit(services: Services, make_peer) -> None:
    """A deletes while B is typing: the delete wins, B is told who did it, and
    B's in-flight edits are discarded on arrival (spec section 8.1)."""
    room, peers, clients, document_id = await _room_with(services, make_peer, 2)
    a_peer, b_peer = peers
    b_client = clients[1]

    b_client.insert(0, "typing while it is deleted")
    await a_peer.session.handle_message(
        DeleteDocumentMessage(type="delete_document", documentId=document_id)
    )

    notice = b_peer.conn.last("document_deleted")
    assert notice is not None
    assert notice["documentId"] == document_id
    assert notice["deletedBy"] == room.users[a_peer.user_id].display_name

    # The in-flight update arrives after the delete and is dropped, not applied.
    await b_client.flush()
    assert room.documents.get(document_id) is None
    assert document_id not in room.documents


async def test_deleting_the_last_document_is_allowed(services: Services, make_peer) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    document_id = room.documents.records[0].document_id

    await a.session.handle_message(
        DeleteDocumentMessage(type="delete_document", documentId=document_id)
    )
    assert len(room.documents) == 0


async def test_document_names_are_sanitized_and_capped(services: Services, make_peer) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)

    await a.session.handle_message(
        CreateDocumentMessage(type="create_document", name="ev‮il\x00.py" + "z" * 400)
    )
    created = room.documents.records[-1]
    assert "‮" not in created.name and "\x00" not in created.name
    assert len(created.name) <= services.settings.MAX_DOC_NAME_CHARS


async def test_max_docs_per_room_is_enforced(services: Services, settings, make_peer) -> None:
    settings.MAX_DOCS_PER_ROOM = 2
    room = await services.manager.create()
    a = await make_peer(room.room_code)  # the room opens with one document

    await a.session.handle_message(CreateDocumentMessage(type="create_document", name="two.py"))
    await a.session.handle_message(CreateDocumentMessage(type="create_document", name="three.py"))

    assert len(room.documents) == 2
    error = a.conn.last("error")
    assert error is not None and error["code"] == "too_many_documents"
