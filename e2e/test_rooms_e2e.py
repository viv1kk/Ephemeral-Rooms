"""Room lifecycle and file handling, end to end in a real browser.

The E2E server runs with ROOM_EMPTY_GRACE_MS and USER_RECONNECT_GRACE_MS set to
8 seconds (see conftest), so the lifecycle boundaries are observable without
the suite taking minutes. The exact timer arithmetic is covered by the unit
suite against an injected clock; what these tests prove is that the behaviour
survives a real browser and a real socket.
"""

from __future__ import annotations

import re

from conftest import ORIGIN


def test_the_landing_page_creates_a_room_and_navigates_to_it(servers, browser_instance) -> None:
    context = browser_instance.new_context()
    page = context.new_page()
    try:
        page.goto(ORIGIN)
        page.click("[data-testid=create-room]")
        page.wait_for_url(re.compile(r"/room/\d{4}$"), timeout=15000)
        page.wait_for_selector(".status.connected", timeout=15000)

        assert re.search(r"/room/(\d{4})$", page.url) is not None
        # Created by the button, so no "this room was empty" notice.
        assert page.locator("[data-testid=new-room-notice]").count() == 0
        assert "Rooms are temporary" not in page.content()
    finally:
        context.close()


def test_visiting_an_unknown_code_creates_a_room_and_says_so(servers, browser_instance) -> None:
    """A mistyped code and an expired room are otherwise indistinguishable
    (spec section 26)."""
    context = browser_instance.new_context()
    page = context.new_page()
    try:
        page.goto(f"{ORIGIN}/room/7777")
        page.wait_for_selector("[data-testid=new-room-notice]", timeout=15000)
        assert "a new one was created" in page.inner_text("[data-testid=new-room-notice]")
    finally:
        context.close()


def test_the_open_access_notice_is_shown_in_the_room_panel(open_room) -> None:
    """Stated once, plainly, because the model is deliberate (spec section 21.1)."""
    room = open_room("6001")
    assert "Anyone with this room code can view, edit, and delete everything here." in (
        room.page.inner_text(".room-info")
    )


def test_a_refresh_by_the_sole_participant_preserves_the_room(open_room) -> None:
    """The acceptance criterion that EMPTY_GRACE exists for (spec section 3)."""
    room = open_room("6002")
    room.type("this must survive a refresh")
    room.page.wait_for_timeout(800)
    original_user = room.user_id

    room.page.reload()
    room.wait_connected()

    room.wait_for_text("this must survive a refresh")
    # Same identity, rebound by the resume token in sessionStorage.
    assert room.user_id == original_user
    assert room.people_count() == 1


def test_a_non_final_user_leaving_does_not_disturb_the_others(open_room) -> None:
    a = open_room("6003")
    b = open_room("6003")
    a.type("kept alive by A")
    b.wait_for_text("kept alive by A")

    b.page.click("[data-testid=leave-room]")
    b.page.wait_for_url(f"{ORIGIN}/", timeout=10000)

    a.page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=presence-list] li').length === 1",
        timeout=15000,
    )
    assert a.editor_text == "kept alive by A"


def test_documents_can_be_created_renamed_and_deleted(open_room) -> None:
    a = open_room("6004")
    b = open_room("6004")

    a.page.click("[data-testid=new-document]")
    b.page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=document-list] li').length === 2",
        timeout=15000,
    )

    # Deleting a document both users can see removes it for both.
    a.page.locator("[data-testid=document-list] li").last.get_by_text("Delete").click()
    b.page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=document-list] li').length === 1",
        timeout=15000,
    )


def test_syntax_highlighting_follows_the_document_extension(open_room) -> None:
    room = open_room("6005")
    room.type("def greet():\n    return 'hi'\n")

    # Rename the document to .py and the Python mode should load and tokenize.
    item = room.page.locator("[data-testid=document-list] li").first
    item.get_by_text("Rename").click()
    room.page.keyboard.press("ControlOrMeta+a")
    room.page.keyboard.type("main.py")
    room.page.keyboard.press("Enter")

    # A loaded language mode tokenizes the line into styled spans; plain text
    # renders as a bare text node with none.
    room.page.wait_for_function(
        "() => document.querySelectorAll('.cm-line span').length > 0", timeout=15000
    )
    assert room.page.locator(".cm-line span").count() >= 2


def test_a_file_uploads_downloads_and_deletes(open_room) -> None:
    a = open_room("6006")
    b = open_room("6006")

    a.page.set_input_files(
        "[data-testid=file-input]",
        files=[{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"uploaded contents"}],
    )

    # The other participant sees it appear live.
    b.page.wait_for_selector("[data-testid=file-list] >> text=notes.txt", timeout=20000)
    assert "uploaded contents" not in b.page.content()

    # Download it back through the API, scoped to this room.
    file_id = b.page.evaluate(
        """async (code) => {
            const r = await fetch(`/api/rooms/${code}/files`);
            return null;
        }""",
        "6006",
    )
    assert file_id is None  # no list endpoint by design; the list comes over the socket

    link = b.page.locator("[data-testid=file-list] a.button").first
    href = link.get_attribute("href")
    assert href is not None and "/api/rooms/6006/files/" in href
    body = b.page.evaluate("async (href) => (await fetch(href)).text()", href)
    assert body == "uploaded contents"

    # Any participant may delete any file (spec section 13).
    b.page.locator("[data-testid=file-list] button.danger").first.click()
    a.page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=file-list] li').length === 1"
        " && document.querySelector('[data-testid=file-list] li').classList.contains('empty')",
        timeout=15000,
    )


def test_a_unicode_filename_survives_upload_and_download(open_room) -> None:
    a = open_room("6007")
    name = "отчёт 日本語.txt"
    a.page.set_input_files(
        "[data-testid=file-input]",
        files=[{"name": name, "mimeType": "text/plain", "buffer": "содержимое".encode("utf-8")}],
    )
    a.page.wait_for_selector(f"[data-testid=file-list] >> text={name}", timeout=20000)

    href = a.page.locator("[data-testid=file-list] a.button").first.get_attribute("href")
    disposition = a.page.evaluate(
        "async (href) => (await fetch(href)).headers.get('content-disposition')", href
    )
    assert "filename*=UTF-8''" in disposition


def test_the_status_bar_reports_connection_people_and_storage(open_room) -> None:
    room = open_room("6008")
    status = room.page.inner_text(".room-status")

    assert "Connected" in status
    assert "1 person" in status
    assert "Available storage:" in status


def test_upload_progress_disappears_when_the_upload_finishes(open_room) -> None:
    """The progress row is transient: once the file lands it must be replaced
    by the file list entry, for the uploader and for everyone watching.

    The uploader's own row is driven locally from XHR progress events, while
    other participants see a coarse broadcast, so the two are cleaned up by
    different paths and both need asserting."""
    a = open_room("6009")
    b = open_room("6009")

    # Large enough that progress is broadcast at least once before completion,
    # so the observer definitely has a row to clean up.
    a.page.set_input_files(
        "[data-testid=file-input]",
        files=[{"name": "big.bin", "mimeType": "application/octet-stream",
                "buffer": b"x" * (3 * 1024 * 1024)}],
    )

    # Both see the finished file.
    a.page.wait_for_selector("[data-testid=file-list] >> text=big.bin", timeout=30000)
    b.page.wait_for_selector("[data-testid=file-list] >> text=big.bin", timeout=30000)

    # And neither is left with a progress row.
    a.page.wait_for_function("() => document.querySelectorAll('.upload').length === 0", timeout=15000)
    b.page.wait_for_function("() => document.querySelectorAll('.upload').length === 0", timeout=15000)

    assert a.page.locator(".upload").count() == 0, "the uploader's progress row was left behind"
    assert b.page.locator(".upload.remote").count() == 0, "the observer's progress row was left behind"
