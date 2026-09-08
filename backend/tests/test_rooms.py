"""Room lifecycle tests (spec section 37, Rooms).

Every grace period here is crossed by advancing the FakeClock, never by
sleeping.
"""

from __future__ import annotations

import pytest

from app.deps import Services
from app.rooms.manager import NoCodesAvailableError
from app.rooms.types import RoomState
from tests.fakes import FakeClock

pytestmark = pytest.mark.asyncio


async def test_create_allocates_a_four_digit_code(services: Services) -> None:
    room = await services.manager.create()
    assert room.room_code.isdigit() and len(room.room_code) == 4
    assert room.state is RoomState.ACTIVE
    assert services.manager.get(room.room_code) is room


async def test_each_user_gets_a_unique_server_generated_uuid(services: Services, make_peer) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)
    assert a.user_id != b.user_id
    # The client never supplies it; join carries no identity field at all.
    assert a.joined["userId"] == a.user_id


async def test_multiple_users_join_the_same_room(services: Services, make_peer) -> None:
    room = await services.manager.create()
    await make_peer(room.room_code)
    await make_peer(room.room_code)
    await make_peer(room.room_code)
    assert len(room.connected_users) == 3


async def test_a_non_final_user_leaving_does_not_destroy_the_room(
    services: Services, clock: FakeClock, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    await make_peer(room.room_code)

    await a.session.leave(explicit=True)
    await clock.advance(10 * 60_000)

    assert room.state is RoomState.ACTIVE
    assert services.manager.get(room.room_code) is room


async def test_final_participant_leaving_destroys_the_room_after_the_grace(
    services: Services, clock: FakeClock, settings, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)

    await a.session.leave(explicit=True)
    assert room.state is RoomState.EMPTY_GRACE, "state must be retained during the grace window"

    # One millisecond before the deadline the room is still fully recoverable.
    await clock.advance(settings.ROOM_EMPTY_GRACE_MS - 1)
    assert room.state is RoomState.EMPTY_GRACE

    await clock.advance(2)
    assert room.state in (RoomState.CLOSING, RoomState.CLOSED)
    assert services.manager.get(room.room_code) is None


async def test_join_during_empty_grace_resurrects_the_same_instance(
    services: Services, clock: FakeClock, settings, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    document_id = room.documents.records[0].document_id
    room.documents.doc(document_id)["text"] += "work in progress"

    await a.session.leave(explicit=True)
    assert room.state is RoomState.EMPTY_GRACE

    await clock.advance(settings.ROOM_EMPTY_GRACE_MS // 2)
    b = await make_peer(room.room_code)

    resumed_room = services.manager.get(room.room_code)
    assert resumed_room is room, "the same instance must be returned"
    assert room.state is RoomState.ACTIVE
    assert room.documents.text(document_id) == "work in progress"

    # The closing timer must actually be cancelled, not merely ignored.
    await clock.advance(settings.ROOM_EMPTY_GRACE_MS * 2)
    assert room.state is RoomState.ACTIVE
    assert b.session.room is room


async def test_refresh_as_the_sole_participant_does_not_destroy_the_room(
    services: Services, clock: FakeClock, make_peer
) -> None:
    """The acceptance criterion that EMPTY_GRACE exists for."""
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    document_id = room.documents.records[0].document_id
    room.documents.doc(document_id)["text"] += "survives a refresh"
    token = a.token
    original_user_id = a.user_id
    original_client_id = a.client_id

    # A refresh is a socket close with no explicit leave.
    a.conn.drop()
    await a.session.leave(explicit=False)
    assert room.state is RoomState.EMPTY_GRACE

    await clock.advance(1_000)
    b = await make_peer(room.room_code, token=token)

    assert services.manager.get(room.room_code) is room
    assert b.joined["resumed"] is True
    assert b.user_id == original_user_id
    # Same clientID, so the user keeps their place in the CRDT tie-break order.
    assert b.client_id == original_client_id
    assert room.documents.text(document_id) == "survives a refresh"


async def test_reopening_a_destroyed_room_yields_a_fresh_empty_instance(
    services: Services, clock: FakeClock, settings, make_peer
) -> None:
    room = await services.manager.create()
    code = room.room_code
    original_instance_id = room.room_instance_id
    a = await make_peer(code)
    document_id = room.documents.records[0].document_id
    room.documents.doc(document_id)["text"] += "this must not survive"

    await a.session.leave(explicit=True)
    await clock.advance(settings.ROOM_EMPTY_GRACE_MS + 1)
    await room.wait_closed()

    fresh, created = await services.manager.get_or_create(code)
    assert created is True
    assert fresh is not room
    assert fresh.room_instance_id != original_instance_id
    assert len(fresh.documents) == 0
    assert fresh.files == {}


async def test_join_during_closing_yields_a_new_instance(
    services: Services, clock: FakeClock, settings, make_peer
) -> None:
    """Spec section 34: the code is released before deletion begins, so an
    arrival mid-cleanup can never touch the old instance's directory."""
    room = await services.manager.create()
    code = room.room_code
    a = await make_peer(code)
    await a.session.leave(explicit=True)
    await clock.advance(settings.ROOM_EMPTY_GRACE_MS + 1)

    assert room.state in (RoomState.CLOSING, RoomState.CLOSED)
    # The map no longer resolves the code, which is what makes B's lookup miss.
    assert services.manager.get(code) is None

    b_room, created = await services.manager.get_or_create(code)
    assert created is True
    assert b_room.room_instance_id != room.room_instance_id


async def test_max_rooms_is_enforced(services: Services, settings) -> None:
    settings.MAX_ROOMS = 3
    for _ in range(3):
        await services.manager.create()
    with pytest.raises(NoCodesAvailableError):
        await services.manager.create()


async def test_room_files_are_deleted_on_close(
    services: Services, clock: FakeClock, settings, file_store, make_peer
) -> None:
    room = await services.manager.create()
    instance_id = room.room_instance_id
    a = await make_peer(room.room_code)
    await file_store.append_part(instance_id, "0" * 8, b"payload")
    assert await file_store.room_exists(instance_id)

    await a.session.leave(explicit=True)
    await clock.advance(settings.ROOM_EMPTY_GRACE_MS + 1)
    await room.wait_closed()

    # Step 7 of spec section 33: the directory is verified gone, not assumed.
    assert not await file_store.room_exists(instance_id)


async def test_a_room_always_opens_with_a_document(services: Services, make_peer) -> None:
    room = await services.manager.create()
    await make_peer(room.room_code)
    assert len(room.documents) == 1


async def test_url_visit_to_an_unknown_code_flags_the_room_as_created(
    services: Services, make_peer
) -> None:
    """A mistyped code and an expired room are indistinguishable otherwise
    (spec section 26)."""
    peer = await make_peer("4827")
    assert peer.joined["roomWasCreated"] is True

    second = await make_peer("4827")
    assert second.joined["roomWasCreated"] is False
