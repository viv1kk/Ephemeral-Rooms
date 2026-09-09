"""Manual check: does switching browser tabs disturb anyone else's view?

Not part of the automated suite, and it cannot be. A real tab switch needs a
real window manager: Playwright emulates focus so background pages still
report `document.hasFocus() === true`, and turning that emulation off does
nothing headless. Every headless attempt at this reproduced nothing and would
have reported the bug as fixed.

So this runs a headed browser, genuinely backgrounds one of two participants,
and asserts its own precondition - if the page still claims focus, it says so
and exits rather than passing vacuously.

Run the dev servers first (backend on :8000, vite on :5173), then:

    backend/.venv/Scripts/python e2e/manual/tab_switch_check.py <room> <seconds>
    backend/.venv/Scripts/python e2e/manual/tab_switch_check.py 7000 60

Expected: the participant who stays keeps the other's caret and their own
"You" flag for the whole absence. Seeing `caret=0` or `-NOCUR` on the staying
side is the regression.
"""

import sys, time
from playwright.sync_api import sync_playwright

ORIGIN = "http://127.0.0.1:5173"
ROOM = sys.argv[1]
AWAY = int(sys.argv[2])


def snap(page, who):
    d = page.evaluate("""() => {
        const aw = window.__room && window.__room.awareness ? window.__room.awareness() : null;
        const ed = document.querySelector('.cm-editor');
        return {
            caret: document.querySelectorAll('.cm-ySelectionCaret:not(.cm-yLocalCaret)').length,
            sel: document.querySelectorAll('.cm-ySelection').length,
            you: document.querySelectorAll('.cm-yLocalCaret').length,
            cm: ed ? ed.classList.contains('cm-focused') : null,
            df: document.hasFocus(), vis: document.visibilityState,
            others: aw ? Object.entries(aw.states).filter(([k]) => k !== String(aw.self))
                       .map(([k, v]) => k + (v.hasCursor ? '+cur' : '-NOCUR')) : [],
        };
    }""")
    print(f"  [{who}] caret={d['caret']} sel={d['sel']} you={d['you']} cm={d['cm']} "
          f"docFocus={d['df']} vis={d['vis']} others={d['others']}")
    return d


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)
    ctx_a = browser.new_context()
    ctx_b = browser.new_context()
    a = ctx_a.new_page(); b = ctx_b.new_page()
    for p in (a, b):
        p.goto(f"{ORIGIN}/room/{ROOM}")
        p.wait_for_selector(".status.connected", timeout=25000)
        p.wait_for_selector(".cm-content", timeout=25000)

    a.locator(".cm-content").click()
    a.keyboard.type("The quick brown fox jumps over the lazy dog")
    b.wait_for_function("() => window.__room.text().length > 20", timeout=20000)
    a.keyboard.press("Home")
    for _ in range(20): a.keyboard.press("Shift+ArrowRight")
    b.locator(".cm-content").click(); b.keyboard.press("End")
    time.sleep(2)
    print("\n--- both active ---"); snap(a, "A"); snap(b, "B")

    # Playwright keeps every page reporting as focused so tests are
    # deterministic. That is precisely what made the earlier reproductions
    # useless: document.hasFocus() stayed true no matter what. Turn it off so
    # this page behaves like a real background tab.
    cdp_b = ctx_b.new_cdp_session(b)
    cdp_b.send("Emulation.setFocusEmulationEnabled", {"enabled": False})

    other = ctx_b.new_page()
    other.goto("about:blank")
    other.bring_to_front()
    time.sleep(3)
    print(f"\n--- B switched away ({AWAY}s) ---"); snap(a, "A"); snap(b, "B")

    for t in range(20, AWAY + 1, 20):
        time.sleep(20); print(f"  t+{t}s"); snap(a, "A")

    b.bring_to_front()
    time.sleep(6)
    print("\n--- B switched back (+6s) ---"); snap(a, "A"); snap(b, "B")
    time.sleep(25)
    print("\n--- +25s ---"); snap(a, "A"); snap(b, "B")
    browser.close()
