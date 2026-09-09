"""End-to-end fixtures: a real Uvicorn process and a real Vite dev server.

The browser side has to be exercised through a real browser against a real
server, because the whole point is testing the JavaScript-to-Python wire format
(spec sections 0.1 and 37.1).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
BACKEND_PORT = 8010
VITE_PORT = 5174
ORIGIN = f"http://127.0.0.1:{VITE_PORT}"


def _free_wait(port: int, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise RuntimeError(f"port {port} never opened")


def _node() -> str:
    found = shutil.which("node")
    if found is not None:
        return found
    for candidate in ("C:/Program Files/nodejs/node.exe", "/usr/bin/node", "/usr/local/bin/node"):
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("node not found")


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def servers(tmp_path_factory) -> Iterator[None]:
    data_root = tmp_path_factory.mktemp("e2e-data")
    python = BACKEND / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = BACKEND / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)

    env = {
        **os.environ,
        "DATA_ROOT": str(data_root),
        "PUBLIC_ORIGIN": ORIGIN,
        # Short graces so lifecycle behaviour is observable in a browser test
        # without the suite taking minutes.
        "ROOM_EMPTY_GRACE_MS": "8000",
        "USER_RECONNECT_GRACE_MS": "8000",
        "WS_HEARTBEAT_INTERVAL_MS": "2000",
        "WS_HEARTBEAT_TIMEOUT_MS": "6000",
        "LOG_LEVEL": "WARNING",
    }
    # Exactly one worker; see spec section 20.1.
    api = subprocess.Popen(
        [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(BACKEND_PORT), "--workers", "1"],
        cwd=BACKEND,
        env=env,
    )
    vite = subprocess.Popen(
        [_node(), str(FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"),
         "--port", str(VITE_PORT), "--strictPort"],
        cwd=FRONTEND,
        env={**os.environ, "VITE_BACKEND_ORIGIN": f"http://127.0.0.1:{BACKEND_PORT}"},
    )
    try:
        _free_wait(BACKEND_PORT)
        _free_wait(VITE_PORT)
        yield
    finally:
        _kill_tree(vite)
        _kill_tree(api)


@pytest.fixture(scope="session")
def browser_instance() -> Iterator[Browser]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        yield browser
        browser.close()


class RoomPage:
    """One browser tab in a room, with the small conveniences the tests need."""

    def __init__(self, context: BrowserContext, page: Page, room_code: str) -> None:
        self.context = context
        self.page = page
        self.room_code = room_code

    # -- editor ---------------------------------------------------------

    @property
    def editor_text(self) -> str:
        """The document's CRDT state, which is what the guarantees are about."""
        return str(self.page.evaluate("() => window.__room.text()"))

    @property
    def dom_text(self) -> str:
        """What the editor actually renders.

        y-codemirror.next injects remote cursor and selection labels as widgets
        inside .cm-content, so a naive innerText read returns the other user's
        display name as if it were document content. They are stripped here."""
        return str(
            self.page.evaluate(
                """() => {
                    const clone = document.querySelector('.cm-content').cloneNode(true);
                    clone.querySelectorAll('.cm-ySelectionInfo, .cm-ySelectionCaret, .cm-widgetBuffer')
                         .forEach(n => n.remove());
                    return clone.textContent.replace(/[\\u200b\\u2060]/g, '');
                }"""
            )
        )

    def click_editor(self) -> None:
        self.page.locator(".cm-content").click()

    def type(self, text: str) -> None:
        self.click_editor()
        self.page.keyboard.type(text)

    def move_to_start(self) -> None:
        self.click_editor()
        self.page.keyboard.press("ControlOrMeta+Home")

    def undo(self) -> None:
        self.click_editor()
        self.page.keyboard.press("ControlOrMeta+z")

    def wait_for_text(self, expected: str, timeout: int = 15000) -> None:
        self.page.wait_for_function(
            "expected => window.__room?.text?.() === expected",
            arg=expected,
            timeout=timeout,
        )

    # -- room -----------------------------------------------------------

    @property
    def client_id(self) -> int:
        return int(self.page.evaluate("() => window.__room.clientId"))

    @property
    def user_id(self) -> str:
        return str(self.page.evaluate("() => window.__room.userId"))

    def wait_connected(self, timeout: int = 15000) -> None:
        self.page.wait_for_selector(".status.connected", timeout=timeout)
        self.page.wait_for_selector(".cm-content", timeout=timeout)

    def people_count(self) -> int:
        return self.page.locator("[data-testid=presence-list] li").count()

    def close(self) -> None:
        self.page.close()
        self.context.close()


@pytest.fixture
def open_room(servers, browser_instance: Browser):
    """Open a room in a fresh browser context.

    Each context has its own sessionStorage, so two contexts are two genuinely
    separate users rather than two views of one session (spec section 4.1)."""
    opened: list[RoomPage] = []

    def _open(room_code: str, **context_options) -> RoomPage:
        """`context_options` are passed to Playwright's new_context, so a test
        can ask for a phone-sized viewport with a touch screen. That matters
        for the responsive rules: the CSS keys off pointer and hover
        capability, and a default desktop context reports `hover: hover` no
        matter how narrow its viewport is."""
        context = browser_instance.new_context(**context_options)
        page = context.new_page()
        page.goto(f"{ORIGIN}/room/{room_code}")
        room = RoomPage(context, page, room_code)
        room.wait_connected()
        opened.append(room)
        return room

    yield _open

    for room in opened:
        try:
            room.close()
        except Exception:
            pass
