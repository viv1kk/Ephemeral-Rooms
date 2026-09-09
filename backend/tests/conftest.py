"""Shared fixtures.

Every test gets its own DATA_ROOT under tmp_path and its own Services graph,
so nothing leaks between tests and the "data root is empty after a full test
run" acceptance check is meaningful.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.deps import Services, build_services
from app.storage.fs import LocalFileStore
from app.ws.connection import Session
from tests.fakes import FakeClock, FakeDiskSpace, RecordingConnection


def make_test_settings(data_root: Path) -> Settings:
    """Build the settings every test runs against.

    A plain function rather than only a fixture, so the hermeticity guard in
    test_ws_protocol.py can call it inside a directory holding a deliberately
    divergent .env. Asserting on the fixture alone is not enough: it passes by
    accident on any machine whose real .env happens to match the defaults.

    `_env_file=None` is the load-bearing argument. Without it pydantic-settings
    reads a developer's local backend/.env for every field not named below, so
    editing that file would quietly change test outcomes on one machine and not
    another.
    """
    return Settings(
        _env_file=None,
        DATA_ROOT=data_root,
        PUBLIC_ORIGIN="http://testserver",
        ROOM_EMPTY_GRACE_MS=60_000,
        USER_RECONNECT_GRACE_MS=30_000,
        WS_HEARTBEAT_INTERVAL_MS=20_000,
        WS_HEARTBEAT_TIMEOUT_MS=45_000,
        DISK_HEADROOM_BYTES=1024,
        UPLOAD_STALE_MS=600_000,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_test_settings(tmp_path / "data")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def disk() -> FakeDiskSpace:
    return FakeDiskSpace()


@pytest.fixture
def file_store(settings: Settings) -> LocalFileStore:
    return LocalFileStore(settings.DATA_ROOT)


@pytest_asyncio.fixture
async def services(
    settings: Settings,
    clock: FakeClock,
    disk: FakeDiskSpace,
    file_store: LocalFileStore,
) -> AsyncIterator[Services]:
    await file_store.sweep_data_root()
    svc = build_services(settings, clock=clock, disk=disk, file_store=file_store)
    yield svc
    await svc.manager.shutdown()


@pytest_asyncio.fixture
async def api(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client bound to the app, sharing the test's Services graph.

    The app is constructed without its lifespan so the boot sweep does not run
    between fixtures; the boot sweep has its own dedicated test."""
    from app.main import create_app

    application = create_app(services.settings, services=services)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


class Peer:
    """A connection plus the Session driving it, so tests read like a script."""

    def __init__(self, connection: RecordingConnection, session: Session) -> None:
        self.conn = connection
        self.session = session

    @property
    def user_id(self) -> str:
        assert self.session.user is not None
        return self.session.user.user_id

    @property
    def client_id(self) -> int:
        assert self.session.user is not None
        return self.session.user.client_id

    @property
    def token(self) -> str:
        assert self.session.user is not None
        return self.session.user.session_token

    @property
    def joined(self) -> dict:
        frame = self.conn.last("joined")
        assert frame is not None, "peer never received a join acknowledgement"
        return frame


@pytest.fixture
def make_peer(services: Services):
    """Build an independent session against the given room code."""

    async def _make(room_code: str, *, name: str = "peer", token: str | None = None) -> Peer:
        from app.ws.messages import JoinMessage

        conn = RecordingConnection(name)
        session = Session(connection=conn, services=services)
        await session.handle_join(
            JoinMessage(type="join", roomCode=room_code, sessionToken=token)
        )
        return Peer(conn, session)

    return _make


@pytest.fixture(autouse=True)
def _anyio_backend() -> Iterator[None]:
    yield
