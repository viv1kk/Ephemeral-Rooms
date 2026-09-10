"""Headed sweep of the editor, to rule out anything else before shipping.

Written after three wrong diagnoses of one bug report, so it is deliberately
broad: it exercises the paths a person actually uses rather than the one that
was reported, and it asserts on what the editor DID rather than how long it
took. Timing is what hid this bug for two rounds - nothing was ever slow, the
key presses simply did nothing.

Headed on purpose. The whole failure lived in rendering and DOM structure - an
inline widget that stole cursor stops - and headless Chromium reproduced the
symptom only partly and hid its severity. The same reasoning as
tab_switch_check.py beside this file.

Run the dev servers first (backend on :8000, vite on :5173), then:

    backend/.venv/Scripts/python e2e/manual/cursor_investigation.py

Every scenario asserts two things unless stated otherwise: the cursor moved on
every key press that should move it, and nothing was written to the console.
A non-zero exit means at least one scenario failed; the summary says which.

It has teeth, which is the only reason it is worth keeping. Against the
duplicate @codemirror/state this was written to rule out, it scores 33/46 -
thirteen failures across seven of the nine scenarios - and 46/46 once the
duplicate is collapsed. A sweep that passes either way would have been worse
than nothing here, because it would have looked like evidence.
"""

from __future__ import annotations

import sys
import time

from playwright.sync_api import Page, sync_playwright

# Defaults to the Vite dev server; pass an origin to check the PRODUCTION
# bundle instead, which is what a user actually loads and which resolves its
# modules differently:
#
#     ... cursor_investigation.py http://127.0.0.1:8001
#
# A second argument picks the engine - chromium (default), firefox or webkit -
# for when a fault might plausibly be engine-specific:
#
#     ... cursor_investigation.py http://127.0.0.1:5173 firefox
ORIGIN = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5173"
ENGINE = sys.argv[2] if len(sys.argv) > 2 else "chromium"
HEAD = "() => document.querySelector('.cm-content').cmView.view.state.selection.main.head"
SEL = """() => {
    const s = document.querySelector('.cm-content').cmView.view.state.selection.main;
    return { anchor: s.anchor, head: s.head, empty: s.empty };
}"""

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")


class Client:
    def __init__(self, ctx, room: str, label: str) -> None:
        self.label = label
        self.errors: list[str] = []
        self.page: Page = ctx.new_page()
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}") if m.type == "error" else None,
        )
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        # domcontentloaded rather than load: the page opens a WebSocket and keeps
        # it open, and Firefox counts that against "load" for longer than
        # Chromium does. What matters is that the room connects, which the two
        # waits below assert directly.
        self.page.goto(f"{ORIGIN}/room/{room}", wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_selector(".status.connected", timeout=30000)
        self.page.wait_for_selector(".cm-content", timeout=30000)

    def click(self) -> None:
        self.page.locator(".cm-content").click()

    def type(self, text: str) -> None:
        self.page.keyboard.type(text)

    def press(self, key: str, n: int = 1, delay: int = 70) -> None:
        for _ in range(n):
            self.page.keyboard.press(key)
            self.page.wait_for_timeout(delay)

    @property
    def head(self) -> int:
        return int(self.page.evaluate(HEAD))

    @property
    def text(self) -> str:
        return str(self.page.evaluate("() => window.__room.text()"))

    def moves(self, key: str, n: int, delay: int = 70) -> list[int]:
        """Change in document offset per press."""
        prev, out = self.head, []
        for _ in range(n):
            self.page.keyboard.press(key)
            self.page.wait_for_timeout(delay)
            now = self.head
            out.append(now - prev)
            prev = now
        return out

    def clear_errors(self) -> None:
        self.errors.clear()

    def fresh_errors(self) -> list[str]:
        return list(self.errors)


def check_moves(name: str, moves: list[int], expected: int) -> None:
    bad = [i for i, d in enumerate(moves) if d != expected]
    record(
        name,
        not bad,
        "" if not bad else f"presses {bad} did not move by {expected}: {moves}",
    )


# The browser's OWN message when a WebSocket cannot be opened - which happens
# legitimately whenever a peer's tab is closing, or the dev server's proxy is
# between states. It is not the application reporting anything, and Firefox
# surfaces it where Chromium stays quiet.
#
# Narrow on purpose. The bug this sweep exists for announced itself as a
# TypeError, and a filter loose enough to swallow that would make the whole
# file worthless - so this matches the connection message and nothing else,
# and what it skips is still counted and printed rather than hidden.
_NETWORK_NOISE = (
    "can't establish a connection",
    "can’t establish a connection",
    "WebSocket connection to",
    "Firefox can",
    # Reported when a socket is torn down because its page is closing or
    # navigating, which every scenario does deliberately at the end.
    "was interrupted while the page was loading",
)


def _is_network_noise(text: str) -> bool:
    return any(p in text for p in _NETWORK_NOISE)


def check_quiet(name: str, *clients: Client) -> None:
    raw = [f"[{c.label}] {e}" for c in clients for e in c.fresh_errors()]
    app = [e for e in raw if not _is_network_noise(e)]
    noise = len(raw) - len(app)
    detail = ""
    if app:
        detail = f"{len(app)} application errors, first: {app[0][:160]}"
    elif noise:
        detail = f"clean ({noise} browser socket message(s) ignored)"
    record(name, not app, detail)


# ---------------------------------------------------------------------------


def scenario_single_user(ctx) -> None:
    print("\n[1] one person, every kind of cursor motion")
    a = Client(ctx, "8001", "A")
    a.click()
    a.type("the quick brown fox jumps over the lazy dog")
    a.page.keyboard.press("Enter")
    a.type("second line with more words in it")
    a.page.keyboard.press("Enter")
    a.type("third line here")
    a.page.wait_for_timeout(600)
    a.clear_errors()

    a.press("ControlOrMeta+Home")
    check_moves("1.1 ArrowRight x12", a.moves("ArrowRight", 12), 1)
    check_moves("1.2 ArrowLeft x8", a.moves("ArrowLeft", 8), -1)

    a.press("ControlOrMeta+Home")
    down = a.moves("ArrowDown", 2)
    record("1.3 ArrowDown moves", all(d > 0 for d in down), f"{down}")
    up = a.moves("ArrowUp", 2)
    record("1.4 ArrowUp moves", all(d < 0 for d in up), f"{up}")

    a.press("End")
    at_end = a.head
    a.press("Home")
    record("1.5 Home/End move", a.head < at_end, f"end={at_end} home={a.head}")

    # Word-wise motion, a different code path to single characters.
    a.press("ControlOrMeta+Home")
    word = a.moves("Control+ArrowRight", 4)
    record("1.6 word-wise right", all(d > 0 for d in word), f"{word}")

    # Shift-extension: the anchor must stay put while the head travels.
    a.press("ControlOrMeta+Home")
    a.press("Shift+ArrowRight", 5)
    sel = a.page.evaluate(SEL)
    record("1.7 shift extends selection", sel["anchor"] == 0 and sel["head"] == 5, str(sel))

    check_quiet("1.8 console silent throughout", a)
    a.page.close()


def scenario_two_users(ctx) -> None:
    print("\n[2] two people, both carets in the document - the reported case")
    a = Client(ctx, "8002", "A")
    b = Client(ctx, "8002", "B")
    a.click()
    a.type("shared line for both of us to move around in")
    a.page.wait_for_timeout(500)
    b.page.wait_for_timeout(500)

    b.click()
    b.press("ControlOrMeta+Home")
    b.press("ArrowRight", 6)
    a.click()
    a.press("ControlOrMeta+Home")
    a.page.wait_for_timeout(600)
    a.clear_errors()
    b.clear_errors()

    record(
        "2.1 both carets rendered",
        a.page.locator(".cm-ySelectionCaret:not(.cm-yLocalCaret)").count() == 1
        and a.page.locator(".cm-yLocalCaret").count() == 1,
        f"remote={a.page.locator('.cm-ySelectionCaret:not(.cm-yLocalCaret)').count()} "
        f"local={a.page.locator('.cm-yLocalCaret').count()}",
    )

    check_moves("2.2 A ArrowRight x15", a.moves("ArrowRight", 15), 1)
    check_moves("2.3 A ArrowLeft x10", a.moves("ArrowLeft", 10), -1)

    # Straight through the other person's caret, which is where an inline
    # widget would have cost a press.
    a.press("ControlOrMeta+Home")
    through = a.moves("ArrowRight", 12)
    check_moves("2.4 A moves through B's caret", through, 1)

    # Re-focus B first. Only one window has OS focus at a time in a headed
    # browser, and a blurred editor correctly ignores arrow keys - without this
    # the scenario measures focus, not movement.
    b.click()
    b.press("ControlOrMeta+Home")
    check_moves("2.5 B ArrowRight x10", b.moves("ArrowRight", 10), 1)
    check_quiet("2.6 console silent, both", a, b)

    # And it must stay correct once the other person is gone.
    b.page.close()
    a.page.wait_for_timeout(1200)
    a.clear_errors()
    a.press("ControlOrMeta+Home")
    check_moves("2.7 A after B leaves", a.moves("ArrowRight", 10), 1)
    check_quiet("2.8 console silent after leave", a)
    a.page.close()


def scenario_three_users(ctx) -> None:
    print("\n[3] three people")
    a = Client(ctx, "8003", "A")
    b = Client(ctx, "8003", "B")
    c = Client(ctx, "8003", "C")
    a.click()
    a.type("three of us in this room together now")
    a.page.wait_for_timeout(700)

    for who, n in ((b, 5), (c, 11)):
        who.click()
        who.press("ControlOrMeta+Home")
        who.press("ArrowRight", n)
    a.click()
    a.press("ControlOrMeta+Home")
    a.page.wait_for_timeout(800)
    a.clear_errors()

    record(
        "3.1 two remote carets",
        a.page.locator(".cm-ySelectionCaret:not(.cm-yLocalCaret)").count() == 2,
        f"{a.page.locator('.cm-ySelectionCaret:not(.cm-yLocalCaret)').count()}",
    )
    check_moves("3.2 A through both carets", a.moves("ArrowRight", 15), 1)
    check_quiet("3.3 console silent", a, b, c)
    for p in (a, b, c):
        p.page.close()


def scenario_reload(ctx) -> None:
    print("\n[4] reload and resume - the clientID reassignment path")
    a = Client(ctx, "8004", "A")
    b = Client(ctx, "8004", "B")
    a.click()
    a.type("written before the reload")
    a.page.wait_for_timeout(600)
    b.click()
    b.page.wait_for_timeout(400)

    a.page.reload()
    a.page.wait_for_selector(".status.connected", timeout=30000)
    a.page.wait_for_selector(".cm-content", timeout=30000)
    a.page.wait_for_timeout(1200)
    a.clear_errors()

    aw = a.page.evaluate("() => window.__room.awareness()")
    record("4.1 awareness id aligned", str(aw["self"]) in aw["states"], str(aw))
    record("4.2 document survived", a.text == "written before the reload", repr(a.text))

    a.click()
    a.press("ControlOrMeta+Home")
    check_moves("4.3 movement after reload", a.moves("ArrowRight", 10), 1)
    record(
        "4.4 no phantom self-caret",
        a.page.locator(".cm-ySelectionCaret:not(.cm-yLocalCaret)").count() <= 1,
        f"{a.page.locator('.cm-ySelectionCaret:not(.cm-yLocalCaret)').count()} remote carets for 1 other person",
    )
    check_quiet("4.5 console silent", a)
    a.page.close()
    b.page.close()


def scenario_large_document(ctx) -> None:
    print("\n[5] a large paste - what removing the size caps made possible")
    a = Client(ctx, "8005", "A")
    b = Client(ctx, "8005", "B")
    a.click()
    a.page.evaluate(
        """(n) => {
            const view = document.querySelector('.cm-content').cmView.view;
            const NL = String.fromCharCode(10);
            const line = 'the quick brown fox jumps over the lazy dog';
            const lines = [];
            while (lines.length * (line.length + 1) < n) lines.push(line);
            view.dispatch({ changes: { from: 0, insert: lines.join(NL) } });
        }""",
        120000,
    )
    a.page.wait_for_timeout(2500)
    b.click()
    b.page.wait_for_timeout(1500)
    a.click()
    a.clear_errors()

    length = a.page.evaluate("() => document.querySelector('.cm-content').cmView.view.state.doc.length")
    record("5.1 large document loaded", length > 100000, f"{length} chars")

    a.press("ControlOrMeta+Home")
    check_moves("5.2 movement at the start", a.moves("ArrowRight", 10), 1)

    a.press("ControlOrMeta+End")
    a.page.wait_for_timeout(400)
    check_moves("5.3 movement at the end", a.moves("ArrowLeft", 10), -1)

    t0 = time.time()
    a.moves("ArrowRight", 20, delay=0)
    per = (time.time() - t0) * 1000 / 20
    record("5.4 still responsive at size", per < 60, f"{per:.0f} ms/press")
    check_quiet("5.5 console silent", a, b)
    a.page.close()
    b.page.close()


def scenario_wrapped_lines(ctx) -> None:
    print("\n[6] one very long wrapped line - vertical motion inside a wrap")
    a = Client(ctx, "8006", "A")
    b = Client(ctx, "8006", "B")
    a.click()
    a.type("word " * 160)
    a.page.wait_for_timeout(900)
    b.click()
    b.page.wait_for_timeout(700)
    a.click()
    a.press("ControlOrMeta+Home")
    a.clear_errors()

    down = a.moves("ArrowDown", 3)
    record("6.1 down moves within the wrap", all(d > 0 for d in down), f"{down}")
    up = a.moves("ArrowUp", 2)
    record("6.2 up moves within the wrap", all(d < 0 for d in up), f"{up}")
    check_moves("6.3 right across a wrap boundary", a.moves("ArrowRight", 12), 1)
    check_quiet("6.4 console silent", a, b)
    a.page.close()
    b.page.close()


def scenario_editing(ctx) -> None:
    print("\n[7] editing, brackets, undo/redo, convergence")
    a = Client(ctx, "8007", "A")
    b = Client(ctx, "8007", "B")
    a.click()
    a.type("call(arg) and [more] and {here}")
    a.page.wait_for_timeout(700)
    b.click()
    b.page.wait_for_timeout(600)
    a.click()
    a.clear_errors()
    b.clear_errors()

    a.press("ControlOrMeta+Home")
    a.press("ArrowRight", 9)
    a.page.wait_for_timeout(400)
    record(
        "7.1 bracket matching still works",
        a.page.locator(".cm-matchingBracket").count() >= 1,
        f"{a.page.locator('.cm-matchingBracket').count()} marks",
    )

    a.press("ControlOrMeta+End")
    a.type(" tail")
    a.page.wait_for_timeout(700)
    record("7.2 edit reached B", "tail" in b.text, repr(b.text[-30:]))

    a.press("ControlOrMeta+z")
    a.page.wait_for_timeout(700)
    record("7.3 undo removed only that", "tail" not in a.text, repr(a.text[-30:]))

    # Both typing at once, then convergence.
    a.press("ControlOrMeta+Home")
    b.click()
    b.press("ControlOrMeta+End")
    a.type("AAA")
    b.type("BBB")
    a.page.wait_for_timeout(1500)
    b.page.wait_for_timeout(500)
    record("7.4 replicas converged", a.text == b.text, f"{a.text[:24]!r} vs {b.text[:24]!r}")
    check_quiet("7.5 console silent", a, b)
    a.page.close()
    b.page.close()


def scenario_multiple_documents(ctx) -> None:
    print("\n[8] a second document, and switching between them")
    a = Client(ctx, "8008", "A")
    b = Client(ctx, "8008", "B")
    a.click()
    a.type("first document")
    a.page.wait_for_timeout(500)

    a.page.locator("[data-testid=new-document]").click()
    a.page.wait_for_timeout(900)
    docs = a.page.locator("[data-testid=document-list] .doc-name")
    record("8.1 the room now lists two documents", docs.count() == 2, f"{docs.count()} listed")

    # Creating a document deliberately does not switch to it - `document_created`
    # only appends to the list - so open it explicitly, the way a person would.
    docs.nth(1).click()
    a.page.wait_for_timeout(900)
    a.click()
    a.type("second document")
    a.page.wait_for_timeout(800)
    b.page.wait_for_timeout(600)
    a.clear_errors()

    record("8.2 the new document holds only its own text", a.text == "second document", repr(a.text))
    a.press("ControlOrMeta+Home")
    check_moves("8.3 movement in the new document", a.moves("ArrowRight", 8), 1)

    # Back to the first, which is where a stale editor binding would show up.
    docs.nth(0).click()
    a.page.wait_for_timeout(900)
    record("8.4 the first document is unchanged", a.text == "first document", repr(a.text))
    a.click()
    a.press("ControlOrMeta+Home")
    check_moves("8.5 movement after switching back", a.moves("ArrowRight", 6), 1)
    check_quiet("8.6 console silent", a, b)
    a.page.close()
    b.page.close()


def scenario_rapid_repeat(ctx) -> None:
    print("\n[9] holding the key down - auto-repeat, no pause between presses")
    a = Client(ctx, "8009", "A")
    b = Client(ctx, "8009", "B")
    a.click()
    a.type("the quick brown fox jumps over the lazy dog again and again")
    a.page.wait_for_timeout(600)
    b.click()
    b.page.wait_for_timeout(600)
    a.click()
    a.press("ControlOrMeta+Home")
    a.clear_errors()

    start = a.head
    for _ in range(40):
        a.page.keyboard.press("ArrowRight")
    a.page.wait_for_timeout(900)
    record("9.1 40 rapid presses land 40 characters", a.head - start == 40, f"moved {a.head - start}")
    check_quiet("9.2 console silent under repeat", a, b)
    a.page.close()
    b.page.close()


def main() -> int:
    with sync_playwright() as pw:
        print(f"{ENGINE} against {ORIGIN}")
        browser = getattr(pw, ENGINE).launch(headless=False)
        try:
            for scenario in (
                scenario_single_user,
                scenario_two_users,
                scenario_three_users,
                scenario_reload,
                scenario_large_document,
                scenario_wrapped_lines,
                scenario_editing,
                scenario_multiple_documents,
                scenario_rapid_repeat,
            ):
                # A context per scenario, closed before the next one. Leaving
                # them open accumulates live WebSockets and, in Firefox, starts
                # timing out page loads several scenarios later.
                ctx = browser.new_context()
                try:
                    scenario(ctx)
                finally:
                    ctx.close()
        finally:
            browser.close()

    failed = [(n, d) for n, ok, d in results if not ok]
    print(f"\n{'=' * 70}\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name, detail in failed:
        print(f"  FAILED  {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
