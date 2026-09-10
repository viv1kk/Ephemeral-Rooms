"""Application configuration.

Every knob in the spec is an environment variable with a documented default.
Settings are validated at import time so a malformed value fails the process at
startup rather than at the first request (spec section 38).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Deployment -------------------------------------------------------
    # No domain is ever hardcoded in the source (spec section 31).
    PUBLIC_ORIGIN: str = "http://127.0.0.1:5173"
    DATA_ROOT: Path = Path("./.data")
    LOG_LEVEL: str = "INFO"

    # Redirect plain HTTP to HTTPS when a proxy tells us the browser is on
    # HTTP. Inert without that header, so it cannot affect local development or
    # a direct connection - and behind the bundled Nginx it never fires either,
    # because Nginx already redirects on port 80. It exists for proxies that
    # forward port 80 straight through, such as a Cloudflare tunnel without
    # "Always Use HTTPS" enabled.
    REDIRECT_HTTP_TO_HTTPS: bool = True

    # --- Lifecycle timers (spec section 2.1) ------------------------------
    WS_HEARTBEAT_INTERVAL_MS: int = 20_000
    WS_HEARTBEAT_TIMEOUT_MS: int = 45_000
    USER_RECONNECT_GRACE_MS: int = 30_000
    ROOM_EMPTY_GRACE_MS: int = 60_000

    # --- Resource limits (spec section 21.3) ------------------------------
    MAX_ROOMS: int = 200
    MAX_USERS_PER_ROOM: int = 32
    MAX_DOCS_PER_ROOM: int = 50
    MAX_DOC_BYTES: int = 5_242_880
    MAX_FILES_PER_ROOM: int = 500
    MAX_FILE_BYTES: int = 0  # 0 = unlimited
    MAX_ROOM_TOTAL_BYTES: int = 0  # 0 = unlimited
    MAX_WS_MESSAGE_BYTES: int = 1_048_576
    MAX_DISPLAY_NAME_CHARS: int = 32
    MAX_DOC_NAME_CHARS: int = 128
    MAX_FILENAME_CHARS: int = 255
    MAX_UPLOADS_PER_USER: int = 5

    # --- Storage (spec section 17) ----------------------------------------

    # A ceiling on everything this application stores, across every room.
    # 0 leaves the volume itself as the only limit, which is the right default
    # on a dedicated server. Set it when the data root lives on a volume shared
    # with other things - a container on a developer machine, most obviously -
    # where "free space" is not the same question as "space this app may use".
    #
    # Enforced by capping the figure the reservation ledger reads, so admission,
    # the UI number and the mid-transfer re-check all honour it. See
    # BudgetedDiskSpace in app/storage/fs.py for why it is done there and not
    # with a filesystem quota.
    MAX_TOTAL_STORAGE_BYTES: int = 0

    DISK_HEADROOM_BYTES: int = 2 * 1024**3
    DISK_RECHECK_INTERVAL_BYTES: int = 64 * 1024**2
    UPLOAD_CHUNK_BYTES: int = 8 * 1024**2
    UPLOAD_STALE_MS: int = 600_000
    REAPER_INTERVAL_MS: int = 60_000
    STORAGE_BROADCAST_INTERVAL_MS: int = 15_000

    # --- Static assets ----------------------------------------------------
    # In production Nginx serves the built frontend directly; this is only for
    # running the whole thing from one process locally.
    SERVE_STATIC_DIR: Path | None = None

    @field_validator(
        "WS_HEARTBEAT_INTERVAL_MS",
        "WS_HEARTBEAT_TIMEOUT_MS",
        "USER_RECONNECT_GRACE_MS",
        "ROOM_EMPTY_GRACE_MS",
        "MAX_ROOMS",
        "MAX_USERS_PER_ROOM",
        "MAX_DOCS_PER_ROOM",
        "MAX_DOC_BYTES",
        "MAX_FILES_PER_ROOM",
        "MAX_WS_MESSAGE_BYTES",
        "MAX_DISPLAY_NAME_CHARS",
        "MAX_DOC_NAME_CHARS",
        "MAX_FILENAME_CHARS",
        "MAX_UPLOADS_PER_USER",
        "UPLOAD_CHUNK_BYTES",
        "UPLOAD_STALE_MS",
        "REAPER_INTERVAL_MS",
        "DISK_RECHECK_INTERVAL_BYTES",
    )
    @classmethod
    def _must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be greater than zero")
        return v

    @field_validator("MAX_ROOMS")
    @classmethod
    def _rooms_fit_the_code_space(cls, v: int) -> int:
        # Room codes are 4 digits, so 10000 is the hard ceiling. Allocation
        # picks a random unused code and must never be asked to find one in a
        # space that is full (spec section 1).
        if v > 9_000:
            raise ValueError("MAX_ROOMS must leave room in the 4-digit code space (<= 9000)")
        return v

    @field_validator("WS_HEARTBEAT_TIMEOUT_MS")
    @classmethod
    def _timeout_exceeds_interval(cls, v: int, info) -> int:  # type: ignore[no-untyped-def]
        interval = info.data.get("WS_HEARTBEAT_INTERVAL_MS")
        if interval is not None and v <= interval:
            raise ValueError("WS_HEARTBEAT_TIMEOUT_MS must exceed WS_HEARTBEAT_INTERVAL_MS")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
