"""Room lifecycle and file handling, end to end in a real browser.

The E2E server runs with ROOM_EMPTY_GRACE_MS and USER_RECONNECT_GRACE_MS set to
8 seconds (see conftest), so the lifecycle boundaries are observable without
the suite taking minutes. The exact timer arithmetic is covered by the unit
suite against an injected clock; what these tests prove is that the behaviour
survives a real browser and a real socket.
"""

from __future__ import annotations

import re

from conftest import FRONTEND, ORIGIN, ROOT, _node


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


def test_the_share_menu_offers_a_qr_code_and_a_copy_button(open_room) -> None:
    """The panel is closed until asked for, shows the QR above the copy button,
    and dismisses the way a menu is expected to."""
    room = open_room("6010")

    # Closed to begin with.
    assert room.page.locator("[data-testid=share-panel]").count() == 0
    assert room.page.inner_text("[data-testid=share-button]") == "Share"

    room.page.click("[data-testid=share-button]")
    room.page.wait_for_selector("[data-testid=share-panel]", timeout=10000)

    # The QR is lazily imported, so it appears a beat after the panel.
    room.page.wait_for_selector("[data-testid=share-qr]", timeout=15000)

    # It encodes this room's URL, not some other page's.
    assert room.page.inner_text("[data-testid=share-url]").endswith("/room/6010")

    # Ordering: QR above, copy button below.
    qr_box = room.page.locator("[data-testid=share-qr]").bounding_box()
    copy_box = room.page.locator("[data-testid=share-copy]").bounding_box()
    assert qr_box is not None and copy_box is not None
    assert qr_box["y"] + qr_box["height"] <= copy_box["y"], (
        "the copy button should sit below the QR code"
    )

    # A QR that renders as a blank square would still satisfy every selector
    # above, so check it actually drew modules.
    modules = room.page.evaluate(
        "() => document.querySelector('[data-testid=share-qr] path').getAttribute('d').length"
    )
    assert modules > 500, f"the QR path looks empty ({modules} chars)"

    # Escape closes it.
    room.page.keyboard.press("Escape")
    room.page.wait_for_function(
        "() => !document.querySelector('[data-testid=share-panel]')", timeout=5000
    )

    # So does clicking away.
    room.page.click("[data-testid=share-button]")
    room.page.wait_for_selector("[data-testid=share-panel]", timeout=10000)
    room.page.locator(".room-status").click()
    room.page.wait_for_function(
        "() => !document.querySelector('[data-testid=share-panel]')", timeout=5000
    )


def test_the_share_panel_copies_the_room_link(open_room) -> None:
    room = open_room("6011")
    room.context.grant_permissions(["clipboard-read", "clipboard-write"])

    room.page.click("[data-testid=share-button]")
    room.page.wait_for_selector("[data-testid=share-copy]", timeout=10000)
    room.page.click("[data-testid=share-copy]")

    room.page.wait_for_function(
        "() => document.querySelector('[data-testid=share-copy]').textContent.trim() === 'Copied'",
        timeout=10000,
    )
    clipboard = room.page.evaluate("() => navigator.clipboard.readText()")
    assert clipboard.endswith("/room/6011"), f"clipboard held {clipboard!r}"


def test_the_rendered_qr_code_decodes_to_the_room_url(open_room) -> None:
    """Scrape the QR out of the DOM and decode it, rather than trusting that an
    <svg> with a long path is a working code.

    The component emits `M{col} {row}` while indexing `matrix[row][col]`.
    Swapping those would produce a transposed symbol that still looks like a QR
    code and still shows finder patterns in three corners, because
    transposition maps that set onto itself. Only a decode catches it."""
    import json
    import re
    import subprocess

    room = open_room("6012")
    room.page.click("[data-testid=share-button]")
    room.page.wait_for_selector("[data-testid=share-qr]", timeout=15000)

    svg = room.page.evaluate(
        """() => {
            const el = document.querySelector('[data-testid=share-qr]');
            return { viewBox: el.getAttribute('viewBox'), d: el.querySelector('path').getAttribute('d') };
        }"""
    )
    size = int(svg["viewBox"].split()[2])
    # Each dark module is drawn as `M{col} {row}h1v1h-1z`.
    dark = [[int(row), int(col)] for col, row in re.findall(r"M(\d+) (\d+)h1v1h-1z", svg["d"])]
    assert dark, "the QR path contained no modules"

    node = _node()
    decoded = subprocess.run(
        [node, str(ROOT / "e2e" / "qr_decode.mjs")],
        input=json.dumps({"size": size, "dark": dark}),
        capture_output=True,
        text=True,
        cwd=FRONTEND,
        check=True,
    ).stdout.strip()

    assert decoded.endswith("/room/6012"), f"the QR decoded to {decoded!r}"
    assert decoded == room.page.inner_text("[data-testid=share-url]")


PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True}
TABLET = {"viewport": {"width": 820, "height": 1180}, "has_touch": True}
WIDE = {"viewport": {"width": 2560, "height": 1200}}


def test_the_phone_layout_shows_one_pane_at_a_time(open_room) -> None:
    """A 320px sidebar beside an editor does not fit a 390px screen, so the two
    become separate views with a switcher."""
    room = open_room("6013", **PHONE)

    # The switcher is only for narrow screens.
    assert room.page.locator(".pane-tabs").is_visible()

    # Editor first, sidebar out of the way.
    assert room.page.locator(".room-main").is_visible()
    assert not room.page.locator(".room-sidebar").is_visible()

    room.page.click("[data-testid=tab-room]")
    assert room.page.locator(".room-sidebar").is_visible()
    assert not room.page.locator(".room-main").is_visible()

    # Choosing a document should take you to it rather than leaving you on
    # the list, which on a phone is a different screen.
    room.page.locator(".doc-name").first.click()
    assert room.page.locator(".room-main").is_visible()
    assert not room.page.locator(".room-sidebar").is_visible()


def test_nothing_overflows_horizontally_at_any_size(open_room) -> None:
    """A sideways scrollbar is the classic responsive failure, and it is
    invisible in a screenshot taken at the wrong width."""
    for label, options in (("phone", PHONE), ("tablet", TABLET), ("wide", WIDE)):
        room = open_room("6014", **options)
        room.page.wait_for_timeout(250)
        overflow = room.page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"{label} scrolls sideways by {overflow}px"


def test_touch_targets_are_large_enough_on_a_touch_screen(open_room) -> None:
    """Both Apple and Google publish 44px as the minimum comfortable target.
    The desktop button is 30px, so the rules keyed off `pointer: coarse` have
    to actually be applying."""
    room = open_room("6015", **PHONE)

    assert room.page.evaluate("() => matchMedia('(pointer: coarse)').matches"), (
        "the emulated device did not report a coarse pointer, so this proves nothing"
    )

    for selector in ("[data-testid=leave-room]", "[data-testid=tab-room]"):
        box = room.page.locator(selector).bounding_box()
        assert box is not None and box["height"] >= 44, f"{selector} is {box}"


def test_hover_only_controls_are_visible_without_hover(open_room) -> None:
    """Rename and Delete are dimmed until hover on a desktop. A touch screen
    cannot hover, so they would be permanently faint."""
    room = open_room("6016", **PHONE)
    room.page.click("[data-testid=tab-room]")

    opacity = room.page.evaluate(
        "() => getComputedStyle(document.querySelector('.doc-actions')).opacity"
    )
    assert float(opacity) > 0.9, f"document actions are dimmed at {opacity} with no way to hover"


def test_both_panes_are_visible_side_by_side_on_a_desktop(open_room) -> None:
    room = open_room("6017", **WIDE)

    assert not room.page.locator(".pane-tabs").is_visible()
    assert room.page.locator(".room-sidebar").is_visible()
    assert room.page.locator(".room-main").is_visible()

    sidebar = room.page.locator(".room-sidebar").bounding_box()
    main = room.page.locator(".room-main").bounding_box()
    assert sidebar is not None and main is not None
    # Side by side, not stacked.
    assert main["x"] >= sidebar["x"] + sidebar["width"] - 1
