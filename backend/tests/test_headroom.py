"""What bounds a room, now that nothing fixed does (spec sections 17, 21.3).

The application no longer decides in advance how much a room may hold. Text
grows until the server is short of memory, files grow until it is short of
disk, and the only numbers in the way are the two headrooms it keeps for its
own operation. These tests pin that down from both directions: the caps that
used to refuse an operation no longer do, and the headroom that replaced them
still reports when it has been reached.

The memory half is driven through FakeMemory rather than by allocating for
real, for the same reason disk pressure is driven through FakeDiskSpace: a test
that genuinely exhausted the runner's RAM would be a test that occasionally
kills the runner.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.deps import build_services
from app.files.uploads import UploadError
from app.storage.memory import UNKNOWN, MemoryGuard
from app.ws.messages import CreateDocumentMessage, SubscribeDocumentMessage
from tests.crdt_client import CrdtClient
from tests.fakes import FakeMemory

pytestmark = pytest.mark.asyncio


async def _document(services, make_peer):
    room = await services.manager.create()
    peer = await make_peer(room.room_code)
    document_id = room.documents.records[0].document_id
    await peer.session.handle_message(
        SubscribeDocumentMessage(type="subscribe_document", documentId=document_id)
    )
    peer.conn.json_frames.clear()
    return room, peer, CrdtClient(peer.session, document_id, peer.client_id), document_id


# ----------------------------------------------------------------------
# Defaults: nothing fixed stands in the way
# ----------------------------------------------------------------------


async def test_no_size_or_count_cap_is_set_by_default() -> None:
    """The defaults are the feature, so they are asserted rather than assumed.

    Any of these regressing to a non-zero number silently reintroduces a
    ceiling that has nothing to do with what the server can carry."""
    settings = Settings(_env_file=None)

    assert settings.MAX_DOC_BYTES == 0
    assert settings.MAX_DOCS_PER_ROOM == 0
    assert settings.MAX_FILE_BYTES == 0
    assert settings.MAX_FILES_PER_ROOM == 0
    assert settings.MAX_ROOM_TOTAL_BYTES == 0
    assert settings.MAX_TOTAL_STORAGE_BYTES == 0

    # And the headroom that replaced them is not zero, or nothing is protecting
    # the server at all.
    assert settings.DISK_HEADROOM_BYTES > 0
    assert settings.MEMORY_HEADROOM_BYTES > 0


async def test_a_document_far_past_the_old_cap_is_accepted(services, make_peer) -> None:
    """5 MiB was the old MAX_DOC_BYTES. Nothing should notice it now."""
    room, peer, client, document_id = await _document(services, make_peer)

    client.insert(0, "x" * (6 * 1024 * 1024))
    await client.flush()

    assert room.documents.text_length(document_id) == 6 * 1024 * 1024
    assert peer.conn.last("error") is None


async def test_a_large_paste_arrives_as_one_frame(services, make_peer) -> None:
    """One paste is one Yjs update is one frame, and it is not size-checked.

    This is the case the old MAX_WS_MESSAGE_BYTES broke: a 1 MiB cap on binary
    frames is a 1 MiB cap on a single paste, however large the document is
    allowed to become."""
    room, peer, client, document_id = await _document(services, make_peer)

    client.insert(0, "y" * (4 * 1024 * 1024))
    assert any(
        len(f) > services.settings.MAX_WS_MESSAGE_BYTES for f in client.outbox
    ), "the test itself is wrong if no buffered frame exceeds the control-message cap"

    await client.flush()

    assert room.documents.text_length(document_id) == 4 * 1024 * 1024
    assert peer.conn.last("error") is None


async def test_documents_can_be_created_past_the_old_count_cap(services, make_peer) -> None:
    room = await services.manager.create()
    peer = await make_peer(room.room_code)

    for i in range(60):  # the old MAX_DOCS_PER_ROOM was 50
        await peer.session.handle_message(
            CreateDocumentMessage(type="create_document", name=f"doc-{i}.txt")
        )

    assert len(room.documents) == 61  # 60 plus the one every room opens with
    assert peer.conn.last("error") is None


async def test_a_file_larger_than_the_old_room_cap_is_admitted(services, make_peer, disk) -> None:
    """2 GiB was the old MAX_ROOM_TOTAL_BYTES default in the container."""
    disk.free_bytes = 100 * 1024**3
    room = await services.manager.create()
    peer = await make_peer(room.room_code)

    upload = await services.uploads.init(
        room_code=room.room_code,
        filename="big.bin",
        size=8 * 1024**3,
        uploader_id=peer.user_id,
    )

    assert upload.declared_size == 8 * 1024**3


# ----------------------------------------------------------------------
# Headroom is what refuses, and it still does
# ----------------------------------------------------------------------


async def test_an_upload_that_would_eat_the_disk_headroom_is_refused(
    services, make_peer, disk
) -> None:
    """No cap does not mean no limit. The reservation is still the gate."""
    disk.free_bytes = 4096
    room = await services.manager.create()
    peer = await make_peer(room.room_code)

    with pytest.raises(UploadError) as caught:
        await services.uploads.init(
            room_code=room.room_code,
            filename="too-big.bin",
            size=4096,  # would leave less than DISK_HEADROOM_BYTES behind
            uploader_id=peer.user_id,
        )

    assert caught.value.status == 507
    assert caught.value.code == "no_space"


async def test_low_memory_is_reported_to_the_author(services, make_peer, memory) -> None:
    # Set before the join, because the guard caches its reading: a test that
    # lowered it afterwards would be asserting on how quickly the runner got
    # from one line to the next.
    memory.available_bytes = 512  # below MEMORY_HEADROOM_BYTES in conftest
    room, peer, client, document_id = await _document(services, make_peer)

    client.insert(0, "text that lands on a server with no memory left")
    await client.flush()

    error = peer.conn.last("error")
    assert error is not None and error["code"] == "low_memory"


async def test_the_edit_is_still_applied_when_memory_is_low(
    services, make_peer, memory
) -> None:
    """The warning must never cost the sender their text.

    Refusing the update would leave that browser holding text the server does
    not have - a silently diverged replica, which is a worse failure than a
    room that is merely too big."""
    memory.available_bytes = 512
    room, peer, client, document_id = await _document(services, make_peer)

    client.insert(0, "kept")
    await client.flush()

    assert room.documents.text(document_id) == "kept"


async def test_the_low_memory_warning_is_not_repeated_per_keystroke(
    services, make_peer, memory, clock
) -> None:
    memory.available_bytes = 512
    room, peer, client, document_id = await _document(services, make_peer)

    for _ in range(10):
        client.insert(0, "x")
        await client.flush()

    assert len(peer.conn.events_of("error")) == 1

    # ... until enough time has passed that it is news again.
    clock.jump(60_000)
    client.insert(0, "x")
    await client.flush()

    assert len(peer.conn.events_of("error")) == 2


async def test_an_explicit_document_cap_is_still_honoured(
    services, settings, make_peer
) -> None:
    """Removing the default is not removing the knob. An operator who sets one
    still gets it, which is what makes a shared host workable."""
    settings.MAX_DOC_BYTES = 32
    room, peer, client, document_id = await _document(services, make_peer)

    client.insert(0, "x" * 100)
    await client.flush()

    error = peer.conn.last("error")
    assert error is not None and error["code"] == "document_too_large"


# ----------------------------------------------------------------------
# The figures the room is shown
# ----------------------------------------------------------------------


async def test_join_carries_both_headroom_figures(services, make_peer, disk, memory) -> None:
    disk.free_bytes = 50 * 1024**3
    memory.available_bytes = 4 * 1024**3

    peer = await make_peer((await services.manager.create()).room_code)

    joined = peer.conn.last("joined")
    assert joined is not None
    assert joined["storageAvailable"] == 50 * 1024**3 - services.settings.DISK_HEADROOM_BYTES
    assert joined["memoryAvailable"] == 4 * 1024**3 - services.settings.MEMORY_HEADROOM_BYTES


async def test_the_periodic_broadcast_carries_both(services, make_peer, disk, memory) -> None:
    disk.free_bytes = 20 * 1024**3
    memory.available_bytes = 2 * 1024**3
    room = await services.manager.create()
    peer = await make_peer(room.room_code)
    peer.conn.json_frames.clear()

    await services.storage_feed.broadcast_once()

    storage = peer.conn.last("storage")
    assert storage is not None
    assert storage["available"] == 20 * 1024**3 - services.settings.DISK_HEADROOM_BYTES
    assert storage["memoryAvailable"] == 2 * 1024**3 - services.settings.MEMORY_HEADROOM_BYTES


async def test_the_client_is_told_zero_rather_than_a_made_up_cap(services) -> None:
    """The browser has to be able to tell "no limit" from "a limit of zero",
    and the wire says so by sending the setting through unchanged."""
    from app.deps import client_limits

    limits = client_limits(services.settings)

    assert limits["maxDocBytes"] == 0
    assert limits["maxFileBytes"] == 0
    assert limits["maxDocs"] == 0
    assert limits["maxFiles"] == 0


# ----------------------------------------------------------------------
# MemoryGuard itself
# ----------------------------------------------------------------------


async def test_the_guard_subtracts_its_headroom() -> None:
    guard = MemoryGuard(
        memory=FakeMemory(1000), headroom_bytes=400, poll_interval_ms=1
    )

    assert await guard.available_for_new_text() == 600
    assert await guard.under_pressure() is False


async def test_the_guard_never_reports_negative_headroom() -> None:
    guard = MemoryGuard(memory=FakeMemory(100), headroom_bytes=400, poll_interval_ms=1)

    assert await guard.available_for_new_text() == 0
    assert await guard.under_pressure() is True


async def test_an_unmeasurable_platform_is_not_treated_as_pressure() -> None:
    """A machine that exposes nothing to read must behave as it did before this
    guard existed - unbounded - rather than as though it were full. Refusing to
    work because a metrics file could not be parsed would be the wrong trade."""
    guard = MemoryGuard(memory=FakeMemory(UNKNOWN), headroom_bytes=400, poll_interval_ms=1)

    assert await guard.available_for_new_text() == UNKNOWN
    assert await guard.under_pressure() is False


async def test_the_reading_is_cached_between_polls() -> None:
    """It is consulted once per applied CRDT update, so it must not probe once
    per keystroke."""
    fake = FakeMemory(1000)
    now = [0.0]
    guard = MemoryGuard(
        memory=fake,
        headroom_bytes=0,
        poll_interval_ms=1000,
        monotonic=lambda: now[0],
    )

    for _ in range(20):
        await guard.bytes_available()
    assert fake.calls == 1

    now[0] += 2.0
    await guard.bytes_available()
    assert fake.calls == 2


async def test_a_zero_headroom_disables_the_guard() -> None:
    """0 means "reserve nothing", the same way it means "no limit" on the caps.
    It must not become "everything is pressure"."""
    guard = MemoryGuard(memory=FakeMemory(0), headroom_bytes=0, poll_interval_ms=1)

    assert await guard.under_pressure() is False


async def test_a_negative_cap_is_rejected_at_startup() -> None:
    """0 is meaningful on these; a negative value would quietly invert the
    comparison that reads it."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, MAX_DOC_BYTES=-1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, MEMORY_HEADROOM_BYTES=-1)


async def test_services_expose_the_guard(tmp_path) -> None:
    settings = Settings(_env_file=None, DATA_ROOT=tmp_path / "data")

    services = build_services(settings, memory=FakeMemory(2000))

    assert await services.memory.bytes_available() == 2000


# ----------------------------------------------------------------------
# The Linux probes
# ----------------------------------------------------------------------
#
# Production is Linux in a container; a Windows development machine runs the
# `GlobalMemoryStatusEx` branch and never touches any of this, so a green suite
# there would say nothing about the code that actually ships. These drive the
# parsers against real file formats regardless of host, which is the same
# reasoning that put the statvfs tests in test_disk_space.py.


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


async def test_it_reads_a_cgroup_v2_limit(tmp_path, monkeypatch) -> None:
    from app.storage import memory as mod

    _write(tmp_path / "memory.max", "2147483648\n")
    _write(tmp_path / "memory.current", "1073741824\n")
    _write(tmp_path / "memory.stat", "anon 500\ninactive_file 1048576\nslab 12\n")
    monkeypatch.setattr(mod, "_CGROUP_V2", tmp_path)

    # limit - current, plus the page cache that would be reclaimed under
    # pressure rather than counted as lost.
    assert await mod.SystemMemory().bytes_available() == 2147483648 - 1073741824 + 1048576


async def test_an_unlimited_cgroup_v2_falls_through(tmp_path, monkeypatch) -> None:
    """`memory.max` reads "max" when no limit is set, and the host's real
    memory is then the honest answer - not zero, and not a parse failure."""
    from app.storage import memory as mod

    _write(tmp_path / "memory.max", "max\n")
    _write(tmp_path / "memory.current", "1073741824\n")
    _write(tmp_path / "meminfo", "MemTotal:       16000000 kB\nMemAvailable:    8000000 kB\n")
    monkeypatch.setattr(mod, "_CGROUP_V2", tmp_path)
    monkeypatch.setattr(mod, "_CGROUP_V1", tmp_path / "absent")
    monkeypatch.setattr(mod, "_MEMINFO", tmp_path / "meminfo")

    assert await mod.SystemMemory().bytes_available() == 8000000 * 1024


async def test_it_reads_a_cgroup_v1_limit(tmp_path, monkeypatch) -> None:
    from app.storage import memory as mod

    _write(tmp_path / "memory.limit_in_bytes", "2147483648\n")
    _write(tmp_path / "memory.usage_in_bytes", "1073741824\n")
    _write(tmp_path / "memory.stat", "total_inactive_file 1048576\n")
    monkeypatch.setattr(mod, "_CGROUP_V2", tmp_path / "absent")
    monkeypatch.setattr(mod, "_CGROUP_V1", tmp_path)

    assert await mod.SystemMemory().bytes_available() == 2147483648 - 1073741824 + 1048576


async def test_the_cgroup_v1_unlimited_sentinel_is_not_taken_literally(
    tmp_path, monkeypatch
) -> None:
    """v1 spells "no limit" as a number near 2**63. Treating it as a real limit
    would report several exabytes free and disable the guard silently."""
    from app.storage import memory as mod

    _write(tmp_path / "memory.limit_in_bytes", "9223372036854771712\n")
    _write(tmp_path / "memory.usage_in_bytes", "1073741824\n")
    _write(tmp_path / "meminfo", "MemAvailable:    4000000 kB\n")
    monkeypatch.setattr(mod, "_CGROUP_V2", tmp_path / "absent")
    monkeypatch.setattr(mod, "_CGROUP_V1", tmp_path)
    monkeypatch.setattr(mod, "_MEMINFO", tmp_path / "meminfo")

    assert await mod.SystemMemory().bytes_available() == 4000000 * 1024


async def test_meminfo_is_read_in_kilobytes(tmp_path, monkeypatch) -> None:
    """A unit mix-up here would be off by 1024 and still look plausible."""
    from app.storage import memory as mod

    _write(tmp_path / "meminfo", "MemTotal:       16316360 kB\nMemAvailable:   12345678 kB\n")
    monkeypatch.setattr(mod, "_CGROUP_V2", tmp_path / "absent")
    monkeypatch.setattr(mod, "_CGROUP_V1", tmp_path / "absent")
    monkeypatch.setattr(mod, "_MEMINFO", tmp_path / "meminfo")

    assert await mod.SystemMemory().bytes_available() == 12345678 * 1024


async def test_a_platform_with_nothing_readable_reports_unknown(tmp_path, monkeypatch) -> None:
    from app.storage import memory as mod

    monkeypatch.setattr(mod, "_CGROUP_V2", tmp_path / "absent")
    monkeypatch.setattr(mod, "_CGROUP_V1", tmp_path / "absent")
    monkeypatch.setattr(mod, "_MEMINFO", tmp_path / "absent")
    monkeypatch.setattr(mod.SystemMemory, "_windows", lambda self: mod.UNKNOWN)

    assert await mod.SystemMemory().bytes_available() == mod.UNKNOWN


async def test_a_corrupt_metrics_file_is_not_fatal(tmp_path, monkeypatch) -> None:
    """Garbage falls through to the next source rather than raising. The guard
    runs on the CRDT hot path; an exception there would drop an update."""
    from app.storage import memory as mod

    _write(tmp_path / "memory.max", "not a number\n")
    _write(tmp_path / "memory.current", "\x00\x00\x00\n")
    _write(tmp_path / "meminfo", "MemAvailable:    999 kB\n")
    monkeypatch.setattr(mod, "_CGROUP_V2", tmp_path)
    monkeypatch.setattr(mod, "_CGROUP_V1", tmp_path / "absent")
    monkeypatch.setattr(mod, "_MEMINFO", tmp_path / "meminfo")

    assert await mod.SystemMemory().bytes_available() == 999 * 1024
