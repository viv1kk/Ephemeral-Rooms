"""Presence tests (spec section 37, Presence)."""

from __future__ import annotations

import pytest

from app.deps import Services
from app.rooms.types import ConnectionState, RoomState
from app.ws.messages import SetNameMessage
from tests.fakes import FakeClock

pytestmark = pytest.mark.asyncio


async def test_join_is_broadcast_to_existing_participants(
    services: Services, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)

    joins = a.conn.events_of("user_joined")
    assert len(joins) == 1
    assert joins[0]["user"]["userId"] == b.user_id
    # The joiner is not told about their own arrival twice.
    assert b.conn.events_of("user_joined") == []


async def test_explicit_leave_is_broadcast_immediately(services: Services, make_peer) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)

    await b.session.leave(explicit=True)

    left = a.conn.events_of("user_left")
    assert len(left) == 1
    assert left[0]["userId"] == b.user_id
    assert b.user_id not in room.users


async def test_reconnect_within_grace_keeps_identity_and_emits_no_leave_join_pair(
    services: Services, clock: FakeClock, make_peer
) -> None:
    """A brief network blip must not produce a spurious 'left / joined' pair
    (spec section 23)."""
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)
    a.conn.json_frames.clear()

    b.conn.drop()
    await b.session.leave(explicit=False)

    assert room.users[b.user_id].connection_state is ConnectionState.DISCONNECTED
    assert a.conn.events_of("user_left") == [], "no leave event during the reconnect grace"
    # The observer is told about the dimmed state instead.
    presence = a.conn.last("presence")
    assert presence is not None
    dimmed = [u for u in presence["users"] if u["userId"] == b.user_id]
    assert dimmed and dimmed[0]["connected"] is False

    await clock.advance(5_000)
    reconnected = await make_peer(room.room_code, token=b.token)

    assert reconnected.user_id == b.user_id
    assert reconnected.client_id == b.client_id
    assert reconnected.joined["resumed"] is True
    assert a.conn.events_of("user_left") == []
    assert a.conn.events_of("user_joined") == [], "a resume is not a new arrival"


async def test_a_late_close_from_the_old_socket_does_not_disconnect_the_resumed_user(
    services: Services, make_peer
) -> None:
    """A refresh opens the new socket before the old one's close is processed.

    Acting on that stale close would null out the live connection, and the user
    would stay in the room but silently stop receiving broadcasts."""
    room = await services.manager.create()
    observer = await make_peer(room.room_code)
    original = await make_peer(room.room_code)

    # The new socket joins first...
    resumed = await make_peer(room.room_code, token=original.token)
    assert resumed.user_id == original.user_id

    # ...and only then does the old socket's close arrive.
    original.conn.drop()
    await original.session.leave(explicit=False)

    user = room.users[original.user_id]
    assert user.connection_state is ConnectionState.CONNECTED
    assert user.connection is resumed.conn
    assert user in room.connected_users

    # The resumed connection still receives broadcasts.
    resumed.conn.json_frames.clear()
    await observer.session.handle_message(
        SetNameMessage(type="set_name", displayName="Still Listening")
    )
    assert resumed.conn.last("user_updated") is not None


async def test_a_stale_explicit_leave_does_not_remove_the_resumed_user(
    services: Services, make_peer
) -> None:
    room = await services.manager.create()
    await make_peer(room.room_code)
    original = await make_peer(room.room_code)
    resumed = await make_peer(room.room_code, token=original.token)

    # The old tab's "leave" arrives after the new tab has taken over.
    await original.session.leave(explicit=True)

    assert original.user_id in room.users
    assert room.users[original.user_id].connection is resumed.conn


async def test_reconnect_after_grace_produces_a_new_identity(
    services: Services, clock: FakeClock, settings, make_peer
) -> None:
    room = await services.manager.create()
    keeper = await make_peer(room.room_code)  # keeps the room alive
    b = await make_peer(room.room_code)
    old_user_id, old_token, old_client_id = b.user_id, b.token, b.client_id

    b.conn.drop()
    await b.session.leave(explicit=False)
    await clock.advance(settings.USER_RECONNECT_GRACE_MS + 1)

    assert old_user_id not in room.users
    assert keeper.conn.events_of("user_left")[-1]["userId"] == old_user_id

    # A stale token is never an error; the user simply joins as new (section 4.1).
    fresh = await make_peer(room.room_code, token=old_token)
    assert fresh.user_id != old_user_id
    assert fresh.joined["resumed"] is False
    assert fresh.client_id > old_client_id, "join sequence never resets while the instance lives"


async def test_a_temporary_network_failure_does_not_destroy_a_single_user_room(
    services: Services, clock: FakeClock, settings, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    document_id = room.documents.records[0].document_id
    room.documents.doc(document_id)["text"] += "still here"

    a.conn.drop()
    await a.session.leave(explicit=False)

    # Zero CONNECTED users means EMPTY_GRACE, even though A is still inside
    # their own reconnect grace (spec section 2.1).
    assert room.state is RoomState.EMPTY_GRACE

    # Inside the user's own reconnect grace, identity is rebound too.
    await clock.advance(settings.USER_RECONNECT_GRACE_MS - 1_000)
    back = await make_peer(room.room_code, token=a.token)

    assert back.user_id == a.user_id
    assert room.state is RoomState.ACTIVE
    assert room.documents.text(document_id) == "still here"


async def test_the_room_outlives_the_user_grace_for_a_sole_participant(
    services: Services, clock: FakeClock, settings, make_peer
) -> None:
    """The two graces are independent: losing your identity after
    USER_RECONNECT_GRACE_MS does not close the room, which keeps running until
    ROOM_EMPTY_GRACE_MS (spec section 2.1)."""
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    document_id = room.documents.records[0].document_id
    room.documents.doc(document_id)["text"] += "still here"

    a.conn.drop()
    await a.session.leave(explicit=False)

    # Past the user grace: the identity is gone, the room is not.
    await clock.advance(settings.USER_RECONNECT_GRACE_MS + 1)
    assert a.user_id not in room.users
    assert room.state is RoomState.EMPTY_GRACE

    # A stale token joins as new, into the same instance with its state intact.
    back = await make_peer(room.room_code, token=a.token)
    assert back.user_id != a.user_id
    assert back.joined["resumed"] is False
    assert room.state is RoomState.ACTIVE
    assert room.documents.text(document_id) == "still here"


async def test_display_name_change_is_broadcast_and_stamped_with_op_seq(
    services: Services, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)

    await b.session.handle_message(SetNameMessage(type="set_name", displayName="Quiet Panda"))

    update = a.conn.last("user_updated")
    assert update is not None
    assert update["user"]["displayName"] == "Quiet Panda"
    assert update["opSeq"] >= 1


async def test_display_names_are_sanitized(services: Services, make_peer) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)

    # Control characters, a directional override, and excess length.
    await a.session.handle_message(
        SetNameMessage(type="set_name", displayName="Bad‮name\x00\x07  spaced   " + "x" * 200)
    )
    name = room.users[a.user_id].display_name
    assert "‮" not in name
    assert "\x00" not in name and "\x07" not in name
    assert len(name) <= services.settings.MAX_DISPLAY_NAME_CHARS


async def test_an_empty_display_name_falls_back_to_the_current_one(
    services: Services, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    before = room.users[a.user_id].display_name

    await a.session.handle_message(SetNameMessage(type="set_name", displayName="   "))
    assert room.users[a.user_id].display_name == before


async def test_max_users_per_room_is_enforced(services: Services, settings, make_peer) -> None:
    settings.MAX_USERS_PER_ROOM = 2
    room = await services.manager.create()
    await make_peer(room.room_code)
    await make_peer(room.room_code)

    rejected = await make_peer(room.room_code)
    assert rejected.conn.last("error") is not None
    assert rejected.conn.last("error")["code"] == "room_full"
    assert len(room.connected_users) == 2


async def test_users_are_assigned_stable_distinct_colours(services: Services, make_peer) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)

    from app.util.names import color_for

    assert color_for(a.user_id) == a.joined["color"]
    assert color_for(a.user_id) == color_for(a.user_id), "colour must be stable"
    assert a.joined["color"] != b.joined["color"] or a.user_id == b.user_id
