"""ASGI entry point.

  !!  THIS APPLICATION MUST RUN AS EXACTLY ONE UVICORN WORKER.  !!

The RoomManager, every CRDT document, the presence table, the session-token
table and the disk reservation ledger are plain Python objects in this
process's memory. A second worker gets a second, empty copy of all of them, and
the failure is silent rather than loud: two users open the same room code, land
on different workers, and each sees an empty room containing only themselves.

    uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1         --ws-max-size 268435456

`--ws-max-size` is not optional in spirit. One paste into the editor is one
Yjs update is one WebSocket frame, and Uvicorn's 16 MiB default closes the
socket outright (1009) before the application sees the frame - so that default
is a hard cap on how much text a person may paste, imposed a layer below
anything `Settings` can express. The container sets it from
`WS_MAX_FRAME_BYTES` in `docker-entrypoint.sh`.

Do NOT deploy under `gunicorn -k uvicorn.workers.UvicornWorker -w N`. That is
the most commonly recommended FastAPI production pattern and it is wrong here
(spec section 20.1).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.cleanup.boot import sweep_data_root
from app.config import Settings, get_settings
from app.deps import Services, build_services
from app.files.routes import router as http_router
from app.security import SecurityHeadersMiddleware
from app.ws.routes import router as ws_router

log = logging.getLogger(__name__)


def _assert_single_worker() -> None:
    """A prominent warning if more than one worker is somehow detected.

    There is no portable way to ask Uvicorn how many workers it spawned, so
    this checks the two signals that are actually visible from inside a
    worker: the environment variables the common multi-worker launchers set."""
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        raw = os.environ.get(var)
        if raw and raw.strip() not in ("", "1"):
            log.error(
                "=" * 78 + "\n"
                "  %s=%s. THIS APPLICATION SUPPORTS EXACTLY ONE WORKER.\n"
                "  All room state is in this process's memory. With more than one\n"
                "  worker, users of the same room code will land in different,\n"
                "  invisible copies of that room. See spec section 20.1.\n" + "=" * 78,
                var,
                raw,
            )
    if os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn"):
        log.error("Running under gunicorn. Use `uvicorn --workers 1` instead (spec section 20.1).")


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        _assert_single_worker()
        svc: Services = application.state.services
        # No room survives a restart, so everything under the data root is by
        # definition garbage (spec section 15).
        await sweep_data_root(svc.file_store)
        svc.reaper.start()
        svc.storage_feed.start()
        log.info(
            "listening as %s; data root at %s",
            settings.PUBLIC_ORIGIN,
            settings.DATA_ROOT.resolve(),
        )
        try:
            yield
        finally:
            # Orderly shutdown: close sockets with a defined code, cancel
            # timers, and make no attempt to preserve room state.
            await svc.storage_feed.stop()
            await svc.reaper.stop()
            await svc.manager.shutdown()

    application = FastAPI(
        title="Ephemeral Collaborative Rooms",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.services = services or build_services(settings)
    # Added before the routers so it wraps every response, including the
    # streamed download and the SPA fallback below.
    application.add_middleware(
        SecurityHeadersMiddleware,
        redirect_https=settings.REDIRECT_HTTP_TO_HTTPS,
    )
    application.include_router(http_router)
    application.include_router(ws_router)

    static_dir = settings.SERVE_STATIC_DIR
    if static_dir is not None and static_dir.exists():
        # Local convenience only. In production Nginx serves `dist/` directly
        # and never proxies static assets through Uvicorn (spec section 30).
        index = static_dir / "index.html"
        application.mount(
            "/assets", StaticFiles(directory=static_dir / "assets"), name="assets"
        )

        @application.get("/{full_path:path}")
        async def spa(full_path: str) -> FileResponse:
            return FileResponse(index)

    return application


app = create_app()
