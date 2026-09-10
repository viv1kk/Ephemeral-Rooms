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


def test_a_remote_selection_survives_your_own_selection_overlapping_it(open_room) -> None:
    """Reported as selection highlighting being inconsistent, and reproducible.

    CodeMirror leaves the browser to paint the selection unless drawSelection()
    is enabled, and the native highlight is opaque: it covered any remote
    selection it overlapped, so a collaborator's highlight vanished the moment
    you selected the same text. The marks were in the DOM the whole time, which
    is why it looked intermittent rather than broken.

    drawSelection() renders the local selection as a layer behind the content,
    leaving the remote marks - which sit on the text itself - visible through
    it."""
    a = open_room("5009")
    b = open_room("5009")

    a.type("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    b.wait_for_text("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    a.click_editor()
    a.page.keyboard.press("Home")
    for _ in range(20):
        a.page.keyboard.press("Shift+ArrowRight")
    b.page.wait_for_selector(".cm-ySelection", timeout=15000)
    before = b.page.locator(".cm-ySelection").count()
    assert before >= 1

    # B now selects a range overlapping A's.
    b.click_editor()
    b.page.keyboard.press("Home")
    for _ in range(12):
        b.page.keyboard.press("Shift+ArrowRight")
    b.page.wait_for_timeout(600)

    assert b.page.locator(".cm-ySelection").count() >= 1, (
        "the remote selection left the DOM once the local one overlapped it"
    )

    # The selection is drawn by CodeMirror behind the content, not by the
    # browser on top of it. Without this the marks above would still be present
    # and still be invisible, so asserting their presence alone proves nothing.
    layer = b.page.evaluate(
        """() => {
            const el = document.querySelector('.cm-selectionLayer');
            return el === null ? null : Number(getComputedStyle(el).zIndex);
        }"""
    )
    assert layer is not None, "drawSelection() is not active; the browser is painting the selection"
    assert layer < 0, f"the selection layer is not behind the text (z-index {layer})"


def test_your_own_caret_is_flagged_once_someone_else_is_editing(open_room) -> None:
    """Every other caret is a labelled colour flag; without this yours is a
    thin blinking line, which is the hardest one to find."""
    a = open_room("5010")
    a.click_editor()
    a.page.wait_for_timeout(400)

    # Alone, the flag would be pure clutter.
    assert a.page.locator(".cm-yLocalCaret").count() == 0

    b = open_room("5010")
    b.click_editor()
    a.page.wait_for_timeout(900)

    a.page.wait_for_selector(".cm-yLocalCaret", timeout=10000)
    # The label is a ::after pseudo-element, not a child node, so it has to be
    # read from the computed style. That is deliberate: as a real element it was
    # an inline widget, and an inline widget is a stop for horizontal cursor
    # motion - which cost roughly two of every three ArrowRight presses. See
    # editor/localCursor.ts and test_cursor_stability_e2e.py.
    label = a.page.evaluate(
        "() => getComputedStyle(document.querySelector('.cm-yLocalCaret'), '::after').content"
    )
    assert "You" in label, f"the flag should read You, got {label!r}"


def test_a_caret_disappears_when_that_user_leaves_the_text_area(open_room) -> None:
    """y-codemirror.next never clears the published cursor on blur - its guard
    requires focus to be true inside the branch reached only when focus is
    false - so a caret stayed parked on everyone else's screen for the life of
    the tab."""
    a = open_room("5011")
    b = open_room("5011")

    a.type("shared text")
    b.wait_for_text("shared text")
    a.click_editor()
    b.page.wait_for_selector(".cm-ySelectionCaret", timeout=15000)

    # A clicks away from the editor, into the display-name field.
    a.page.locator("[data-testid=display-name]").click()

    b.page.wait_for_function(
        "() => document.querySelectorAll('.cm-ySelectionCaret:not(.cm-yLocalCaret)').length === 0",
        timeout=15000,
    )

    # And it comes back when they return.
    a.click_editor()
    b.page.wait_for_selector(".cm-ySelectionCaret", timeout=15000)


def test_a_reconnect_does_not_tear_down_the_editor(open_room) -> None:
    """Reported as cursors and highlighting disappearing after leaving the tab
    and coming back, and surviving several earlier attempts at a fix.

    The cause was not awareness at all. An outage longer than
    USER_RECONNECT_GRACE_MS makes the server issue a fresh identity, and a
    fresh identity has a different colour. The colour was a dependency of the
    effect that constructs the editor, so the whole EditorView was destroyed
    and rebuilt: focus lost, selection cleared, scroll reset. y-codemirror only
    publishes your cursor while the editor has focus, so the rebuild also made
    you invisible to everyone else until you happened to click back in - which
    is exactly why it looked like an awareness bug.

    Asserted through a real disconnect rather than by inspecting React, so it
    fails if any future change reintroduces a rebuild by another route."""
    a = open_room("5012")
    b = open_room("5012")

    a.type("The quick brown fox")
    b.wait_for_text("The quick brown fox")
    a.click_editor()
    b.click_editor()
    b.page.wait_for_selector(".cm-ySelectionCaret", timeout=15000)

    identity_before = b.user_id

    # The outage has to outlast the server noticing (heartbeat timeout, 6s in
    # the e2e config) plus USER_RECONNECT_GRACE_MS (8s). Anything shorter is
    # resumed with the same identity and the same colour, and would not have
    # triggered the rebuild at all - the assertion below guards against this
    # test quietly proving nothing if those timings change.
    b.context.set_offline(True)
    b.page.wait_for_timeout(20000)
    b.context.set_offline(False)
    b.page.wait_for_selector(".status.connected", timeout=30000)
    b.page.wait_for_timeout(4000)

    assert b.user_id != identity_before, (
        "the reconnect was resumed, so no new colour was assigned and this "
        "test would pass whether or not the bug is present"
    )

    state = b.page.evaluate(
        """() => ({
            focused: document.querySelector('.cm-editor').classList.contains('cm-focused'),
            remoteCaret: document.querySelectorAll('.cm-ySelectionCaret:not(.cm-yLocalCaret)').length,
        })"""
    )
    assert state["focused"], "the editor was rebuilt across the reconnect and lost focus"
    assert state["remoteCaret"] >= 1, "the other participant's caret did not come back"

    # And the other side can still see this one, with no interaction here.
    a.page.wait_for_function(
        "() => document.querySelectorAll('.cm-ySelectionCaret:not(.cm-yLocalCaret)').length >= 1",
        timeout=20000,
    )


def test_switching_tabs_does_not_erase_your_caret_for_others(open_room) -> None:
    """Reported repeatedly: leave the tab, come back, cursors and highlighting
    are gone for the people who stayed.

    CodeMirror's `hasFocus` is `document.hasFocus() && activeElement is the
    content`, so it goes false for two different reasons - the user clicked
    elsewhere on the page, or the whole tab went to the background. Clearing
    the published cursor on both meant switching tabs erased your caret for
    everyone still working, and took their "You" flag with it, because that
    only shows while another participant has a cursor.

    A real tab switch cannot be produced headlessly: Playwright emulates focus
    so background pages still report focused, and disabling that emulation has
    no effect without a real window manager. The behaviour is verified against
    a real browser by e2e/manual/tab_switch_check.py. This pins the branch
    itself by stubbing the one input it reads, which is deterministic and runs
    everywhere."""
    a = open_room("5013")
    b = open_room("5013")

    a.type("shared text")
    b.wait_for_text("shared text")
    b.click_editor()
    a.page.wait_for_selector(".cm-ySelectionCaret", timeout=15000)

    # Make B look like a backgrounded tab, then blur the editor exactly as the
    # browser does when the window loses focus.
    b.page.evaluate(
        """() => {
            document.hasFocus = () => false;
            document.querySelector('.cm-content').blur();
        }"""
    )
    b.page.wait_for_timeout(2500)

    still_there = a.page.evaluate(
        "() => document.querySelectorAll('.cm-ySelectionCaret:not(.cm-yLocalCaret)').length"
    )
    assert still_there >= 1, (
        "the other participant's caret was erased just because they looked at "
        "another tab"
    )

    # The complement still has to hold: leaving the text area while the page
    # itself is still in front does clear it, which is the behaviour that was
    # asked for. The editor has to regain focus first, or clicking away
    # produces no focus change for it and nothing would run either way.
    b.page.evaluate("() => { document.hasFocus = () => true; }")
    b.click_editor()
    b.page.wait_for_timeout(1200)
    b.page.locator("[data-testid=display-name]").click()
    a.page.wait_for_function(
        "() => document.querySelectorAll('.cm-ySelectionCaret:not(.cm-yLocalCaret)').length === 0",
        timeout=15000,
    )
