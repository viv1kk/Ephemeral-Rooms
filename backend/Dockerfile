# Uvicorn running the ASGI app. Built from the repository root:
#
#     docker build -f backend/Dockerfile -t ephemeral-rooms-backend .
#
#   !!  EXACTLY ONE WORKER. See the comment at the top of app/main.py and
#   !!  spec section 20.1. Every room, document, presence entry, session token
#   !!  and disk reservation lives in this one process's memory. Scaling this
#   !!  service to two replicas, or setting WEB_CONCURRENCY, does not scale the
#   !!  application - it silently splits it into N invisible copies, and two
#   !!  users typing the same room code land in different ones. The container
#   !!  is a packaging boundary here, not a horizontal-scaling unit.

# Debian slim, not Alpine, and this is load-bearing. pycrdt ships as a compiled
# Rust extension with manylinux (glibc) wheels only; on musl, pip finds no wheel
# and falls back to a source build that needs a full Rust toolchain. Pinned to a
# digest-stable tag so the CRDT quintet recorded in the README stays reproducible.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# mkfs.ext4, to build the fixed-size room filesystem on first start. mount,
# losetup, setpriv and truncate are already in the base image, so this is the
# only package added to it - still no compiler; see the note below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends e2fsprogs \
 && rm -rf /var/lib/apt/lists/*

# Runtime only - no build-essential, because nothing in requirements.txt should
# ever compile. If a `pip install` here starts invoking a compiler, that is the
# signal that a wheel is missing for this platform; fix the pin rather than
# installing gcc and hiding it.
WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Unprivileged, with a fixed uid so a bind-mounted data root has predictable
# ownership on the host.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin ephemeral

# The mount point, not the storage. The entrypoint mounts a fixed-size ext4
# image over this on every start, so all that is created here is the directory
# the mount lands on. Mode 0700 matches what the systemd deployment requires,
# and the entrypoint reapplies it to the mounted filesystem's own root.
ENV DATA_ROOT=/var/lib/ephemeral-rooms
RUN install -d -o ephemeral -g ephemeral -m 0700 "$DATA_ROOT"

# The commit being built, stamped in by CI and served at /api/version. Declared
# here, late, so changing it does not invalidate the dependency layers above -
# a new build id must not mean re-running pip install.
ARG BUILD_ID=dev
ENV BUILD_ID=${BUILD_ID}

COPY --chown=root:root backend/app ./app
COPY --chown=root:root --chmod=0755 backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Deliberately NOT `USER ephemeral`. The entrypoint needs root to mount the room
# filesystem, then drops to `ephemeral` with capabilities cleared before it
# execs the command below - so Uvicorn itself ends up exactly as unprivileged as
# it was when this image ran as that user throughout. See the script for why the
# mount cannot be left to Docker.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE 8000

# Exercises settings validation, the services graph and a real disk_usage()
# call against the data volume, so it fails if the volume is missing or
# unwritable rather than only if the process is dead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/storage', timeout=4).status == 200 else 1)"]

# 0.0.0.0 because the port is reachable only on the compose network - it is
# deliberately not published to the host; the web container is the only client.
#
# --forwarded-allow-ips '*' trusts X-Forwarded-Proto from any peer, which is
# safe precisely because of that: the only thing that can reach this port is
# the reverse proxy in front of it. Publishing port 8000 to the host would
# invalidate that reasoning - don't.
#
# Do not add --workers N. Do not add --reload.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
