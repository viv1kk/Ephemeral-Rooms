"""File tests (spec section 37, Files)."""

from __future__ import annotations

import httpx
import pytest

from app.deps import Services
from app.rooms.types import RoomState
from app.ws.messages import DeleteFileMessage
from tests.fakes import FakeClock, FakeDiskSpace

pytestmark = pytest.mark.asyncio


async def _upload(
    api: httpx.AsyncClient, room_code: str, user_id: str, name: str, payload: bytes
) -> dict:
    """Run the full init -> PUT -> complete protocol."""
    init = await api.post(
        f"/api/rooms/{room_code}/uploads",
        json={"filename": name, "size": len(payload), "userId": user_id},
    )
    assert init.status_code == 200, init.text
    upload_id = init.json()["uploadId"]

    put = await api.put(
        f"/api/uploads/{upload_id}",
        content=payload,
        headers={"Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}"},
    )
    assert put.status_code == 200, put.text

    done = await api.post(f"/api/uploads/{upload_id}/complete")
    assert done.status_code == 200, done.text
    return done.json()["file"]


async def test_upload_and_download_round_trip(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)

    record = await _upload(api, room.room_code, a.user_id, "notes.txt", b"file contents here")

    got = await api.get(f"/api/rooms/{room.room_code}/files/{record['fileId']}")
    assert got.status_code == 200
    assert got.content == b"file contents here"
    # An uploaded HTML or SVG must not be able to execute on this origin.
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-disposition"].startswith("attachment;")


async def test_uploader_and_timestamp_come_from_the_server(
    services: Services, api: httpx.AsyncClient, clock: FakeClock, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    uploader_name = room.users[a.user_id].display_name

    record = await _upload(api, room.room_code, a.user_id, "x.bin", b"abc")

    assert record["uploaderId"] == a.user_id
    assert record["uploaderName"] == uploader_name
    # The server's clock, never the client's (spec section 13).
    assert record["uploadedAt"] == clock.now_ms()


async def test_uploader_name_is_snapshotted_at_upload_time(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    """Not a live reference: renaming yourself later must not rewrite history
    (spec section 13)."""
    from app.ws.messages import SetNameMessage

    room = await services.manager.create()
    a = await make_peer(room.room_code)
    await a.session.handle_message(SetNameMessage(type="set_name", displayName="Original Name"))

    record = await _upload(api, room.room_code, a.user_id, "x.bin", b"abc")
    await a.session.handle_message(SetNameMessage(type="set_name", displayName="Changed Later"))

    assert room.files[record["fileId"]].uploader_name == "Original Name"


async def test_duplicate_filenames_produce_distinct_files(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)

    first = await _upload(api, room.room_code, a.user_id, "report.pdf", b"first")
    second = await _upload(api, room.room_code, a.user_id, "report.pdf", b"second")

    assert first["fileId"] != second["fileId"]
    assert len(room.files) == 2

    a_body = await api.get(f"/api/rooms/{room.room_code}/files/{first['fileId']}")
    b_body = await api.get(f"/api/rooms/{room.room_code}/files/{second['fileId']}")
    assert a_body.content == b"first" and b_body.content == b"second"


async def test_a_unicode_filename_survives_the_round_trip(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    name = "отчёт 日本語 (final).txt"

    record = await _upload(api, room.room_code, a.user_id, name, b"data")
    got = await api.get(f"/api/rooms/{room.room_code}/files/{record['fileId']}")

    # RFC 5987 encoding carries the real name; the quoted parameter is a
    # plain-ASCII fallback (spec section 14).
    disposition = got.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%D0%BE" in disposition  # the Cyrillic 'о', percent-encoded


async def test_simultaneous_uploads_by_different_users(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)

    a_init = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "a.bin", "size": 4, "userId": a.user_id},
    )
    b_init = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "b.bin", "size": 4, "userId": b.user_id},
    )
    a_id, b_id = a_init.json()["uploadId"], b_init.json()["uploadId"]

    # Interleaved chunks from two uploads in flight at once.
    await api.put(f"/api/uploads/{a_id}", content=b"aa", headers={"Content-Range": "bytes 0-1/4"})
    await api.put(f"/api/uploads/{b_id}", content=b"bb", headers={"Content-Range": "bytes 0-1/4"})
    await api.put(f"/api/uploads/{a_id}", content=b"AA", headers={"Content-Range": "bytes 2-3/4"})
    await api.put(f"/api/uploads/{b_id}", content=b"BB", headers={"Content-Range": "bytes 2-3/4"})

    a_file = (await api.post(f"/api/uploads/{a_id}/complete")).json()["file"]
    b_file = (await api.post(f"/api/uploads/{b_id}/complete")).json()["file"]

    assert (await api.get(f"/api/rooms/{room.room_code}/files/{a_file['fileId']}")).content == b"aaAA"
    assert (await api.get(f"/api/rooms/{room.room_code}/files/{b_file['fileId']}")).content == b"bbBB"


async def test_insufficient_space_is_rejected_before_any_bytes_are_written(
    services: Services, api: httpx.AsyncClient, disk: FakeDiskSpace, file_store, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    disk.free_bytes = 5_000  # headroom is 1024 in the test settings

    resp = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "huge.bin", "size": 10_000, "userId": a.user_id},
    )

    assert resp.status_code == 507
    assert resp.json()["code"] == "no_space"
    assert "Not enough space" in resp.json()["message"]
    # No upload record, no reservation, and nothing on disk.
    assert room.uploads == {}
    assert services.ledger.reserved_bytes == 0
    assert await file_store.list_stale_parts(room.room_instance_id) == []


async def test_two_uploads_that_individually_fit_but_jointly_do_not(
    services: Services, api: httpx.AsyncClient, disk: FakeDiskSpace, make_peer
) -> None:
    """The reservation-ledger test (spec section 17).

    Without the ledger both of these pass their pre-flight check and then both
    fail partway through, which is exactly the failure the spec forbids."""
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    disk.free_bytes = 11_000  # room for one 8000-byte upload, not two

    first = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "one.bin", "size": 8_000, "userId": a.user_id},
    )
    second = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "two.bin", "size": 8_000, "userId": a.user_id},
    )

    assert first.status_code == 200
    assert second.status_code == 507, "the second upload must be refused, not merely warned"
    assert services.ledger.reserved_bytes == 8_000

    # Releasing the first frees the space for the second.
    await api.delete(f"/api/uploads/{first.json()['uploadId']}")
    third = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "three.bin", "size": 8_000, "userId": a.user_id},
    )
    assert third.status_code == 200


async def test_the_ledger_lock_serializes_check_and_reserve(
    services: Services, disk: FakeDiskSpace
) -> None:
    """The check-then-reserve sequence spans an await, so without the lock the
    event loop interleaves a second check between the first check and its
    reservation, and both pass (spec section 17).

    The probe hook yields control at exactly that point, so an unlocked ledger
    would fail this test."""
    import asyncio

    disk.free_bytes = 11_000
    released = asyncio.Event()

    async def yield_inside_the_probe() -> None:
        # Give any concurrent reserve() a chance to interleave.
        released.set()
        await asyncio.sleep(0)

    disk.probe_hook = yield_inside_the_probe

    results = await asyncio.gather(
        services.ledger.reserve("upload-a", 8_000),
        services.ledger.reserve("upload-b", 8_000),
    )

    denied = [r for r in results if r is not None]
    assert len(denied) == 1, "exactly one reservation must be refused"
    assert services.ledger.reserved_bytes == 8_000
    assert released.is_set()


async def test_a_chunk_that_does_not_start_at_the_committed_offset_is_refused(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    init = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "x.bin", "size": 10, "userId": a.user_id},
    )
    upload_id = init.json()["uploadId"]
    await api.put(f"/api/uploads/{upload_id}", content=b"abcde", headers={"Content-Range": "bytes 0-4/10"})

    # A gap: the client skipped ahead.
    bad = await api.put(
        f"/api/uploads/{upload_id}", content=b"xyz", headers={"Content-Range": "bytes 7-9/10"}
    )

    assert bad.status_code == 409
    # The current offset comes back so the client can resynchronize rather than guess.
    assert bad.json()["committedOffset"] == 5


async def test_an_interrupted_upload_resumes_from_the_committed_offset(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    payload = b"0123456789" * 3
    init = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "resume.bin", "size": len(payload), "userId": a.user_id},
    )
    upload_id = init.json()["uploadId"]

    await api.put(
        f"/api/uploads/{upload_id}",
        content=payload[:12],
        headers={"Content-Range": f"bytes 0-11/{len(payload)}"},
    )

    # The client "loses the network" and asks where to resume from.
    status = await api.get(f"/api/uploads/{upload_id}")
    offset = status.json()["committedOffset"]
    assert offset == 12

    await api.put(
        f"/api/uploads/{upload_id}",
        content=payload[offset:],
        headers={"Content-Range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}"},
    )
    record = (await api.post(f"/api/uploads/{upload_id}/complete")).json()["file"]

    got = await api.get(f"/api/rooms/{room.room_code}/files/{record['fileId']}")
    assert got.content == payload


async def test_completing_a_short_upload_is_refused(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    init = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "x.bin", "size": 10, "userId": a.user_id},
    )
    upload_id = init.json()["uploadId"]
    await api.put(f"/api/uploads/{upload_id}", content=b"abc", headers={"Content-Range": "bytes 0-2/10"})

    resp = await api.post(f"/api/uploads/{upload_id}/complete")
    assert resp.status_code == 409
    assert resp.json()["committedOffset"] == 3


async def test_abort_leaves_no_part_file_and_releases_the_reservation(
    services: Services, api: httpx.AsyncClient, file_store, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    init = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "x.bin", "size": 10, "userId": a.user_id},
    )
    upload_id = init.json()["uploadId"]
    await api.put(f"/api/uploads/{upload_id}", content=b"abcde", headers={"Content-Range": "bytes 0-4/10"})
    assert await file_store.part_size(room.room_instance_id, upload_id) == 5

    resp = await api.delete(f"/api/uploads/{upload_id}")

    assert resp.status_code == 204
    assert await file_store.list_stale_parts(room.room_instance_id) == []
    assert services.ledger.reserved_bytes == 0
    assert room.uploads == {}


async def test_the_stale_upload_reaper_releases_abandoned_uploads(
    services: Services, api: httpx.AsyncClient, clock: FakeClock, settings, file_store, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    init = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "abandoned.bin", "size": 100, "userId": a.user_id},
    )
    upload_id = init.json()["uploadId"]
    await api.put(f"/api/uploads/{upload_id}", content=b"partial", headers={"Content-Range": "bytes 0-6/100"})

    # Not yet stale.
    assert await services.reaper.sweep_once() == 0
    assert services.ledger.reserved_bytes > 0

    await clock.advance(settings.UPLOAD_STALE_MS + 1)
    assert await services.reaper.sweep_once() == 1

    assert room.uploads == {}
    assert services.ledger.reserved_bytes == 0
    assert await file_store.list_stale_parts(room.room_instance_id) == []


async def test_any_participant_can_delete_any_file(
    services: Services, api: httpx.AsyncClient, file_store, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)
    record = await _upload(api, room.room_code, a.user_id, "shared.bin", b"data")

    # B deletes A's file. There are no privileged users (spec section 13).
    await b.session.handle_message(DeleteFileMessage(type="delete_file", fileId=record["fileId"]))

    assert record["fileId"] not in room.files
    assert not await file_store.file_exists(room.room_instance_id, record["fileId"])
    event = a.conn.last("file_deleted")
    assert event is not None and event["fileId"] == record["fileId"]

    gone = await api.get(f"/api/rooms/{room.room_code}/files/{record['fileId']}")
    assert gone.status_code == 404


async def test_a_file_id_from_another_room_returns_404(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    """404, never 403: the response must not confirm the file exists elsewhere
    (spec section 14)."""
    room_a = await services.manager.create()
    room_b = await services.manager.create()
    a = await make_peer(room_a.room_code)
    await make_peer(room_b.room_code)

    record = await _upload(api, room_a.room_code, a.user_id, "secret.bin", b"data")

    cross = await api.get(f"/api/rooms/{room_b.room_code}/files/{record['fileId']}")
    assert cross.status_code == 404
    assert "code" in cross.json() and cross.json()["code"] == "not_found"


async def test_a_malformed_file_id_never_reaches_the_filesystem(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    await make_peer(room.room_code)

    for bad in ("../../etc/passwd", "not-a-uuid", "..", "%2e%2e"):
        resp = await api.get(f"/api/rooms/{room.room_code}/files/{bad}")
        assert resp.status_code == 404, bad


async def test_all_files_are_removed_when_the_room_closes(
    services: Services, api: httpx.AsyncClient, clock: FakeClock, settings, file_store, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    instance_id = room.room_instance_id
    await _upload(api, room.room_code, a.user_id, "one.bin", b"aaaa")
    await _upload(api, room.room_code, a.user_id, "two.bin", b"bbbb")
    assert await file_store.room_exists(instance_id)

    await a.session.leave(explicit=True)
    await clock.advance(settings.ROOM_EMPTY_GRACE_MS + 1)
    await room.wait_closed()

    assert room.state is RoomState.CLOSED
    assert not await file_store.room_exists(instance_id)


async def test_room_cleanup_aborts_in_flight_uploads(
    services: Services, api: httpx.AsyncClient, clock: FakeClock, settings, file_store, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    instance_id = room.room_instance_id
    init = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "inflight.bin", "size": 100, "userId": a.user_id},
    )
    await api.put(
        f"/api/uploads/{init.json()['uploadId']}",
        content=b"partial",
        headers={"Content-Range": "bytes 0-6/100"},
    )
    assert services.ledger.reserved_bytes > 0

    await a.session.leave(explicit=True)
    await clock.advance(settings.ROOM_EMPTY_GRACE_MS + 1)
    await room.wait_closed()

    assert services.ledger.reserved_bytes == 0
    assert not await file_store.room_exists(instance_id)


async def test_the_boot_sweep_clears_the_data_root(services: Services, file_store) -> None:
    """Layer 3 of orphan prevention. Everything under the data root at startup
    is garbage, because no room survives a restart (spec section 15)."""
    from app.cleanup.boot import sweep_data_root

    room = await services.manager.create()
    await file_store.append_part(room.room_instance_id, "0" * 8, b"left over from a crash")
    assert await file_store.room_exists(room.room_instance_id)

    await sweep_data_root(file_store)

    assert not await file_store.room_exists(room.room_instance_id)


async def test_storage_available_subtracts_reservations_and_headroom(
    services: Services, api: httpx.AsyncClient, disk: FakeDiskSpace, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    disk.free_bytes = 100_000

    before = (await api.get("/api/storage")).json()["available"]
    assert before == 100_000 - services.settings.DISK_HEADROOM_BYTES

    await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "x.bin", "size": 30_000, "userId": a.user_id},
    )
    after = (await api.get("/api/storage")).json()["available"]
    assert after == before - 30_000


async def test_max_uploads_per_user_is_enforced(
    services: Services, api: httpx.AsyncClient, settings, make_peer
) -> None:
    settings.MAX_UPLOADS_PER_USER = 2
    room = await services.manager.create()
    a = await make_peer(room.room_code)

    for _ in range(2):
        ok = await api.post(
            f"/api/rooms/{room.room_code}/uploads",
            json={"filename": "x.bin", "size": 10, "userId": a.user_id},
        )
        assert ok.status_code == 200

    refused = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "x.bin", "size": 10, "userId": a.user_id},
    )
    assert refused.status_code == 429
    assert refused.json()["code"] == "too_many_uploads"


async def test_a_user_not_in_the_room_cannot_start_an_upload(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    await make_peer(room.room_code)

    resp = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "x.bin", "size": 10, "userId": "00000000-0000-4000-8000-000000000000"},
    )
    assert resp.status_code == 403


async def test_an_upload_cannot_exceed_its_declared_size(
    services: Services, api: httpx.AsyncClient, make_peer
) -> None:
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    init = await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "x.bin", "size": 4, "userId": a.user_id},
    )
    upload_id = init.json()["uploadId"]

    resp = await api.put(
        f"/api/uploads/{upload_id}",
        content=b"far too much data",
        headers={"Content-Range": "bytes 0-16/17"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "over_declared_size"


async def test_the_storage_figure_is_pushed_to_connected_rooms(
    services: Services, api: httpx.AsyncClient, disk: FakeDiskSpace, make_peer
) -> None:
    """The figure must reflect other people's uploads, not just this client's,
    so it is pushed on a timer rather than only computed at join
    (spec section 17)."""
    room = await services.manager.create()
    a = await make_peer(room.room_code)
    b = await make_peer(room.room_code)
    disk.free_bytes = 500_000
    a.conn.json_frames.clear()
    b.conn.json_frames.clear()

    # B reserves space; A must learn about it without doing anything.
    await api.post(
        f"/api/rooms/{room.room_code}/uploads",
        json={"filename": "big.bin", "size": 100_000, "userId": b.user_id},
    )
    sent = await services.storage_feed.broadcast_once()

    expected = 500_000 - 100_000 - services.settings.DISK_HEADROOM_BYTES
    assert sent == expected
    pushed = a.conn.last("storage")
    assert pushed is not None and pushed["available"] == expected


async def test_the_storage_feed_skips_rooms_with_nobody_in_them(
    services: Services, make_peer
) -> None:
    await services.manager.create()  # created but never joined
    assert await services.storage_feed.broadcast_once() == 0
