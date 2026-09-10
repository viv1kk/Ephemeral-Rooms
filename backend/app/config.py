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

    # The commit this image was built from, stamped in by CI as a build
    # argument and surfaced at /api/version. "dev" whenever that did not happen
    # - a local build, or a container run straight from source - which is the
    # honest answer rather than a version number that means nothing.
    BUILD_ID: str = "dev"
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
    #
    # What a room may hold is bounded by what the server can actually carry,
    # not by a number picked in advance. Every cap below that describes the
    # SIZE or COUNT of a room's contents therefore defaults to 0, meaning "no
    # application limit": text grows until memory headroom is gone, files grow
    # until disk headroom is gone, and the two headroom figures - not these
    # settings - are what refuses the operation. They remain configurable
    # because a shared host sometimes needs a smaller ceiling than the machine.
    #
    # The caps that are NOT about room contents keep real defaults: MAX_ROOMS
    # is bounded by the 4-digit code space, MAX_USERS_PER_ROOM by how many
    # cursors a room can usefully carry, MAX_UPLOADS_PER_USER by how many
    # concurrent transfers one browser should open (it queues the rest rather
    # than refusing them), and the name lengths by what fits a UI.
    MAX_ROOMS: int = 200
    MAX_USERS_PER_ROOM: int = 32
    MAX_DOCS_PER_ROOM: int = 0  # 0 = unlimited
    MAX_DOC_BYTES: int = 0  # 0 = unlimited; memory headroom is the real bound
    MAX_FILES_PER_ROOM: int = 0  # 0 = unlimited
    MAX_FILE_BYTES: int = 0  # 0 = unlimited; disk headroom is the real bound
    MAX_ROOM_TOTAL_BYTES: int = 0  # 0 = unlimited
    # Control frames only - `join`, `set_name`, `rename_document` and friends,
    # none of which is ever large. Document text does NOT travel this way; it
    # goes as binary CRDT frames, which are bounded by memory headroom and by
    # the transport ceiling below, never by this.
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

    # --- Memory headroom --------------------------------------------------
    #
    # Documents are the one thing a room holds that never reaches the disk:
    # every `pycrdt.Doc` lives in this process. With no fixed MAX_DOC_BYTES
    # above, memory is what actually bounds how much text a room can hold, so
    # it gets the same treatment the disk already had - a reserved slice the
    # application will not spend, and a guard that reports when what is left
    # has fallen into it.
    #
    # The guard is deliberately advisory: an update that has arrived is always
    # applied, because dropping it would leave the sender's replica holding
    # text the server does not have, and a silently diverged CRDT is far worse
    # than a full one. What the guard does is tell the room, so people can
    # delete a document or move to a new room before the process is in trouble.
    MEMORY_HEADROOM_BYTES: int = 512 * 1024**2
    # How long a memory reading is reused. The probe is cheap but it runs on
    # the CRDT hot path, once per applied update.
    MEMORY_POLL_INTERVAL_MS: int = 2_000

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
        "MAX_WS_MESSAGE_BYTES",
        "MAX_DISPLAY_NAME_CHARS",
        "MAX_DOC_NAME_CHARS",
        "MAX_FILENAME_CHARS",
        "MAX_UPLOADS_PER_USER",
        "UPLOAD_CHUNK_BYTES",
        "UPLOAD_STALE_MS",
        "REAPER_INTERVAL_MS",
        "DISK_RECHECK_INTERVAL_BYTES",
        "MEMORY_POLL_INTERVAL_MS",
    )
    @classmethod
    def _must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be greater than zero")
        return v

    @field_validator(
        "MAX_DOCS_PER_ROOM",
        "MAX_DOC_BYTES",
        "MAX_FILES_PER_ROOM",
        "MAX_FILE_BYTES",
        "MAX_ROOM_TOTAL_BYTES",
        "MAX_TOTAL_STORAGE_BYTES",
        "DISK_HEADROOM_BYTES",
        "MEMORY_HEADROOM_BYTES",
    )
    @classmethod
    def _must_not_be_negative(cls, v: int) -> int:
        # 0 is meaningful on every one of these: "no application limit" for the
        # caps, "reserve nothing" for the two headrooms. A negative value is
        # not, and would quietly invert the comparison that uses it.
        if v < 0:
            raise ValueError("must be zero or greater")
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
