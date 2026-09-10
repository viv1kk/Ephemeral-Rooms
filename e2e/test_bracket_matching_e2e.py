"""Bracket matching still matches, and no longer throws on every key press.

Both halves matter. The fix is a guard inside `renderMatch` (see
editor/brackets.ts), and a guard that is too eager would silently take the
feature away - which is exactly the kind of regression that goes unnoticed
because nothing errors.
"""

from __future__ import annotations


def _end_side_errors(page) -> list[str]:
    out: list[str] = []
    page.on(
        "console",
        lambda m: out.append(m.text) if m.type == "error" and "endSide" in m.text else None,
    )
    return out


def test_arrow_keys_log_nothing(open_room) -> None:
    """`bracketMatching` was building a decoration at an undefined position and
    throwing inside RangeSetBuilder once per press, in every direction."""
    a = open_room("9251")
    a.click_editor()
    for i in range(5):
        a.page.keyboard.type(f"line {i} the quick brown fox")
        a.page.keyboard.press("Enter")
    a.page.wait_for_timeout(700)

    log = _end_side_errors(a.page)
    a.page.keyboard.press("ControlOrMeta+Home")
    for key in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"):
        for _ in range(6):
            a.page.keyboard.press(key)
            a.page.wait_for_timeout(60)

    assert log == [], f"{len(log)} exceptions while moving the cursor: {log[:1]}"


def test_a_matching_pair_is_still_highlighted(open_room) -> None:
    a = open_room("9252")
    a.click_editor()
    a.page.keyboard.type("call(arg)")
    a.page.wait_for_timeout(400)

    # Cursor sits just after the closing bracket, which is where CodeMirror
    # looks backwards for a pair.
    a.page.wait_for_selector(".cm-matchingBracket", timeout=8000)
    assert a.page.locator(".cm-matchingBracket").count() >= 1, (
        "the guard dropped a legitimate match"
    )


def test_an_unmatched_bracket_is_still_marked(open_room) -> None:
    """The other branch of the renderer, and the one whose decoration class
    appeared in the crash - so it needs its own cover."""
    a = open_room("9253")
    a.click_editor()
    # A lone closer. Typing an opener would not do: closeBrackets() inserts the
    # partner for you, so "call(arg" becomes "call(arg)" and is matched.
    a.page.keyboard.type("orphan)")
    a.page.wait_for_timeout(600)
    print("  doc:", a.editor_text)

    assert a.page.locator(".cm-nonmatchingBracket").count() >= 1, (
        "an unmatched bracket should still be marked"
    )
