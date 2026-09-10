"""The editor stays quiet and responsive while cursors move.

These exist because of a reported bug that made the editor almost unusable and
that no unit test could have caught: arrow keys crawled - badly to the right,
slightly to the left, not at all vertically - once a second person put a cursor
in the document, and the lag survived everyone else leaving.

The cause was a clientID desynchronisation, and the reason it needs a BROWSER
test is that every part of it lives in libraries talking to each other:

  * `yjs` reassigns `doc.clientID` when a remote transaction carries structs
    authored under the id this replica is using. A resumed session keeps its
    server-assigned id and is then handed its own earlier writing on the first
    sync, so this fires on any reload-after-typing.
  * `y-protocols/awareness` caches `doc.clientID` at construction.
  * `y-codemirror.next` compares awareness state keys against `doc.clientID`,
    both to skip the local caret and to guard a re-entrant dispatch.

Nothing about that is visible from Python, and a mocked awareness would have
agreed with itself and proved nothing (spec section 0.1, the same reasoning
that puts the CRDT convergence tests in a real browser).

`window.__room.awareness()` reports `self` by reading `doc.clientID` live,
while the state keys are whatever `awareness.clientID` was when the cursor was
published - so "self is missing from states" is precisely the desynchronised
condition, and is what these assert against.
"""

from __future__ import annotations


def _console_errors(page):
    """Console errors and uncaught exceptions, in arrival order.

    CodeMirror catches plugin exceptions and logs them rather than letting them
    reach `pageerror`, so a listener on `console` is the only way to see the
    crash that caused this bug."""
    seen: list[str] = []
    page.on("console", lambda m: seen.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: seen.append(str(e)))
    return seen


def _editor_errors(log) -> list[str]:
    """Crashes caused by THIS application's collaboration layer.

    Deliberately not "any console error". A separate, upstream fault in stock
    CodeMirror logs `Cannot read properties of null (reading 'endSide')` once
    per arrow key, from `bracketMatching` building a decoration at a NaN
    position. It was confirmed upstream by bisection: it survives removing both
    `yCollab` and `localCursor`, so plain CodeMirror reproduces it on its own
    and nothing here can fix it. Matching on it would make these tests fail for
    a reason they are not about, and would hide the regression they exist for.
    """
    return [line for line in log if "update is in progress" in line]


def _awareness(page):
    return page.evaluate("() => window.__room.awareness()")


def test_a_resumed_client_keeps_its_awareness_id_aligned(open_room) -> None:
    """The desynchronisation itself, asserted directly rather than through its
    symptoms - this is the thing that breaks, and it breaks silently."""
    a = open_room("9201")
    a.type("some writing to author under this client id")
    a.wait_for_text("some writing to author under this client id")
    a.click_editor()

    before = _awareness(a.page)
    assert str(before["self"]) in before["states"], "aligned before the reload"

    # The trigger: a resumed session receives its own earlier structs as a
    # remote transaction, and yjs reassigns the clientID underneath it.
    a.page.reload()
    a.wait_connected()
    a.wait_for_text("some writing to author under this client id")
    a.click_editor()
    a.page.wait_for_timeout(300)

    after = _awareness(a.page)
    assert str(after["self"]) in after["states"], (
        f"awareness state is filed under {list(after['states'])} but the doc now "
        f"reports clientID {after['self']}. y-codemirror.next compares against "
        "the latter, so it will treat this user's own cursor as a remote one."
    )


def test_moving_the_cursor_does_not_crash_the_editor(open_room) -> None:
    a = open_room("9202")
    a.type("the quick brown fox jumps over the lazy dog")
    a.wait_for_text("the quick brown fox jumps over the lazy dog")

    a.page.reload()
    a.wait_connected()
    a.wait_for_text("the quick brown fox jumps over the lazy dog")

    log = _console_errors(a.page)
    a.click_editor()
    a.page.keyboard.press("ControlOrMeta+Home")
    # Rightward is the direction that was worst, because the phantom caret is
    # inserted with side: 1 and every step had to cross it.
    for _ in range(20):
        a.page.keyboard.press("ArrowRight")
    a.page.wait_for_timeout(400)

    crashes = _editor_errors(log)
    assert crashes == [], f"{len(crashes)} editor crashes, first: {crashes[0][:200]}"


def test_a_user_is_never_painted_as_their_own_remote_cursor(open_room) -> None:
    """The visible half of the same fault, and the reason for the lag: a second
    caret widget sitting on top of the native one, which arrow keys then had to
    step over."""
    a = open_room("9203")
    a.type("alone in this room")
    a.wait_for_text("alone in this room")

    a.page.reload()
    a.wait_connected()
    a.wait_for_text("alone in this room")
    a.click_editor()
    a.page.wait_for_timeout(400)

    # Remote carets are y-codemirror's; ours carries an extra class. With
    # nobody else here there must be no remote caret at all.
    remote = a.page.locator(".cm-ySelectionCaret:not(.cm-yLocalCaret)").count()
    assert remote == 0, f"{remote} phantom remote caret(s) for a lone user"


def test_two_people_moving_cursors_keep_the_editor_quiet(open_room) -> None:
    """The scenario as it was reported: two people, both with a cursor in the
    document, and the lag persisting after one of them leaves."""
    a = open_room("9204")
    b = open_room("9204")

    a.type("shared line for both of us to move around in")
    a.wait_for_text("shared line for both of us to move around in")
    b.wait_for_text("shared line for both of us to move around in")

    # Both put a cursor in, which is what made the fault visible.
    b.click_editor()
    a.click_editor()
    a.page.wait_for_timeout(400)

    log_a = _console_errors(a.page)
    log_b = _console_errors(b.page)

    for _ in range(15):
        a.page.keyboard.press("ArrowRight")
    for _ in range(15):
        b.page.keyboard.press("ArrowLeft")
    a.page.wait_for_timeout(400)

    assert _editor_errors(log_a) == [], "the first user's editor crashed"
    assert _editor_errors(log_b) == [], "the second user's editor crashed"

    # And it must stay quiet once the other person is gone - the fault used to
    # be permanent for the document, so leaving did not clear it.
    b.page.close()
    a.page.wait_for_timeout(500)
    log_after = _console_errors(a.page)
    for _ in range(15):
        a.page.keyboard.press("ArrowRight")
    a.page.wait_for_timeout(400)

    assert _editor_errors(log_after) == [], "the editor crashed after the other user left"
