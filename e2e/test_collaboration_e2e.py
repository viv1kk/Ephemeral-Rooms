"""M2 gate: the collaboration guarantees, proven in real browsers.

This is the permanent replacement for the M0 spike. It drives two independent
Chromium contexts against a real Uvicorn process, so it exercises the actual
JavaScript-to-Python wire format rather than pycrdt against itself
(spec sections 0.1, 37.1).
"""

from __future__ import annotations

import time

import pytest


def _settle(*rooms, seconds: float = 1.5) -> None:
    """Let updates round-trip. Used only where there is no DOM condition to
    wait on; every assertion that can wait on text uses wait_for_text."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        rooms[0].page.wait_for_timeout(100)


def test_the_browser_doc_uses_the_server_assigned_client_id(open_room) -> None:
    """Spec section 7.1: the tie-breaking clientID must be set on the browser's
    Y.Doc, not the server's, and must equal the room's join sequence number."""
    first = open_room("5001")
    second = open_room("5001")

    assert first.client_id == 1, "the first joiner gets join sequence 1"
    assert second.client_id == 2

    # The value the server assigned is really the one inside the Y.Doc.
    assert first.page.evaluate("() => window.__room.docClientId") == 1
    assert second.page.evaluate("() => window.__room.docClientId") == 2


def test_edits_propagate_between_two_browsers(open_room) -> None:
    a = open_room("5002")
    b = open_room("5002")

    a.type("hello from A")
    b.wait_for_text("hello from A")

    b.click_editor()
    b.page.keyboard.press("End")
    b.page.keyboard.type(" and B")
    a.wait_for_text("hello from A and B")

    assert a.editor_text == b.editor_text
    # The binding really renders it, not just the CRDT holding it.
    assert a.dom_text == "hello from A and B"


def test_concurrent_insertions_at_the_same_position_are_both_preserved(open_room) -> None:
    """The central guarantee, under a genuine network partition.

    Both browsers are taken offline, both type at position 0 without having
    seen the other, and then both come back. Neither edit may be discarded, and
    every replica must agree on the result (spec section 7.1)."""
    a = open_room("5003")
    b = open_room("5003")

    a.type("SEED")
    b.wait_for_text("SEED")

    # Partition.
    a.context.set_offline(True)
    b.context.set_offline(True)
    _settle(a, seconds=0.5)

    a.move_to_start()
    a.page.keyboard.type("AAA")
    b.move_to_start()
    b.page.keyboard.type("BBB")

    assert a.editor_text == "AAASEED"
    assert b.editor_text == "BBBSEED"

    # Heal.
    a.context.set_offline(False)
    b.context.set_offline(False)
    a.page.wait_for_selector(".status.connected", timeout=20000)
    b.page.wait_for_selector(".status.connected", timeout=20000)

    a.page.wait_for_function(
        "() => { const t = window.__room.text(); return t && t.includes('AAA') && t.includes('BBB'); }",
        timeout=20000,
    )
    b.page.wait_for_function(
        "() => { const t = window.__room.text(); return t && t.includes('AAA') && t.includes('BBB'); }",
        timeout=20000,
    )

    final_a = a.page.evaluate("() => window.__room.text()")
    final_b = b.page.evaluate("() => window.__room.text()")

    assert "AAA" in final_a and "BBB" in final_a, f"an insertion was lost: {final_a!r}"
    assert final_a == final_b, f"replicas diverged: {final_a!r} vs {final_b!r}"
    assert len(final_a) == len("AAABBBSEED")


def test_the_tie_break_direction_matches_the_python_implementation(open_room) -> None:
    """Spec section 7.1 obliges us to assert the observed direction and confirm
    that yjs and pycrdt agree on it.

    OBSERVED, yjs 13.6.32 / pycrdt 0.14.4: at a genuinely identical insertion
    point the LOWER clientID lands to the LEFT, so the EARLIER joiner's text
    appears first. This is the same result the Python suite asserts in
    backend/tests/test_collaboration.py."""
    a = open_room("5004")  # clientID 1
    b = open_room("5004")  # clientID 2
    assert a.client_id == 1 and b.client_id == 2

    a.click_editor()
    b.click_editor()

    a.context.set_offline(True)
    b.context.set_offline(True)
    _settle(a, seconds=0.5)

    a.page.keyboard.type("AAA")
    b.page.keyboard.type("BBB")

    a.context.set_offline(False)
    b.context.set_offline(False)
    a.page.wait_for_selector(".status.connected", timeout=20000)
    b.page.wait_for_selector(".status.connected", timeout=20000)

    a.page.wait_for_function(
        "() => window.__room.text() === 'AAABBB'", timeout=20000
    )
    assert a.page.evaluate("() => window.__room.text()") == "AAABBB"
    b.page.wait_for_function("() => window.__room.text() === 'AAABBB'", timeout=20000)


def test_undo_reverts_only_the_local_user_s_typing(open_room) -> None:
    """Ctrl+Z must never revert another participant's work (spec section 10)."""
    a = open_room("5005")
    b = open_room("5005")

    a.type("AAAA")
    b.wait_for_text("AAAA")

    b.click_editor()
    b.page.keyboard.press("End")
    b.page.keyboard.type("BBBB")
    a.wait_for_text("AAAABBBB")

    # B undoes. Only B's own insertion may disappear.
    b.undo()
    b.page.wait_for_function("() => window.__room.text() === 'AAAA'", timeout=10000)

    assert b.page.evaluate("() => window.__room.text()") == "AAAA"
    a.wait_for_text("AAAA")
    assert a.editor_text == "AAAA", "the other user's text was reverted"


def test_remote_cursors_are_rendered_with_a_label(open_room) -> None:
    a = open_room("5006")
    b = open_room("5006")

    a.type("shared line")
    b.wait_for_text("shared line")
    b.click_editor()
    b.page.keyboard.press("End")

    # y-codemirror.next renders remote selections through awareness.
    a.page.wait_for_selector(".cm-ySelectionCaret", timeout=15000)
    assert a.page.locator(".cm-ySelectionCaret").count() >= 1


def test_a_third_browser_joining_late_receives_the_current_document(open_room) -> None:
    a = open_room("5007")
    b = open_room("5007")
    a.type("written before C arrived")
    b.wait_for_text("written before C arrived")

    c = open_room("5007")

    # A late joiner syncs from the server's replica by state-vector diff.
    c.wait_for_text("written before C arrived")
    assert c.people_count() == 3


def test_a_remote_selection_is_highlighted_in_the_selecting_users_colour(open_room) -> None:
    """Spec section 6: selections, not just carets, are shown and attributed.

    y-codemirror.next renders a remote selection as `.cm-ySelection` with an
    inline `background-color` taken from the awareness state's `colorLight`.
    Its own base theme for that class is empty, so if the value is not valid
    CSS the browser drops it and the selection is invisible while every
    element is still present in the DOM. Asserting the element exists is
    therefore not enough; this asserts the computed colour."""
    a = open_room("5008")
    b = open_room("5008")

    a.type("select this whole line please")
    b.wait_for_text("select this whole line please")

    # B selects the line; A should see it highlighted.
    b.click_editor()
    b.page.keyboard.press("Home")
    b.page.keyboard.press("Shift+End")

    a.page.wait_for_selector(".cm-ySelection", timeout=15000)

    painted = a.page.evaluate(
        """() => {
            const el = document.querySelector('.cm-ySelection');
            const bg = getComputedStyle(el).backgroundColor;
            return { bg, inline: el.getAttribute('style') };
        }"""
    )

    assert painted["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"), (
        f"the remote selection is invisible: computed {painted['bg']!r} "
        f"from inline style {painted['inline']!r}"
    )

    # And the label naming the selecting user is actually visible, not hidden
    # behind a hover on a 1px caret the way the library ships it.
    name = b.page.input_value("[data-testid=display-name]")
    label = a.page.evaluate(
        """() => {
            const el = document.querySelector('.cm-ySelectionInfo');
            if (!el) return null;
            return { text: el.textContent, opacity: getComputedStyle(el).opacity };
        }"""
    )
    assert label is not None, "no label identifying the remote user"
    assert label["text"] == name, f"label said {label['text']!r}, expected {name!r}"
    assert float(label["opacity"]) > 0.9, (
        f"the label is present but invisible (opacity {label['opacity']})"
    )
