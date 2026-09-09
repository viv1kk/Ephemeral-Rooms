# Ephemeral Collaborative Rooms

A temporary shared workspace. Someone creates a room, gets a four-digit URL,
and shares it. Everyone who opens that URL edits the same documents live and
shares the same files. When the last person leaves, the room and everything in
it are destroyed.

No registration, no accounts, no database, no persistence.

```
Temporary Workspace
Collaborate. Share files. Nothing is saved.

            [ Create Room ]
```

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Local setup](#local-setup)
- [Environment variables](#environment-variables)
- [**How conflict resolution actually works**](#how-conflict-resolution-actually-works)
- [**How ephemeral cleanup is guaranteed**](#how-ephemeral-cleanup-is-guaranteed)
- [The room lifecycle](#the-room-lifecycle)
- [Uploads and the reservation ledger](#uploads-and-the-reservation-ledger)
- [Security model](#security-model)
- [Testing](#testing)
- [Production build](#production-build)
- [EC2 deployment](#ec2-deployment) — full runbook: [docs/aws-setup.md](docs/aws-setup.md)
- [DNS](#dns)
- [HTTPS with certbot](#https-with-certbot)
- [Verified version quintet](#verified-version-quintet)
- [Known limitations](#known-limitations)

---

## What it does

- **Rooms.** One click creates a room with a random unused four-digit code.
  Visiting an unknown code creates a room with that code, and says so, because
  otherwise a mistyped code is indistinguishable from an expired one.
- **Collaborative editing.** Multiple people edit the same document
  simultaneously with live cursors, labelled by display name and coloured by
  user id. Conflicts converge; nobody's typing is discarded.
- **Multiple documents** per room, with syntax highlighting for Python,
  JavaScript, TypeScript, C, C++, Java, HTML, CSS, JSON, YAML, Markdown, Shell,
  SQL, and a plain-text fallback. The mode follows the file extension, with a
  manual override.
- **File sharing.** Arbitrary types, resumable chunked uploads, live progress,
  and download by id. Any participant can delete anything.
- **Presence.** Live join and leave, dimmed "reconnecting" state, display names.
- **Nothing is saved.** No database. A server restart loses every room.

---

## Architecture

```
Browser  (React, CodeMirror 6, yjs)
   │ HTTPS / WSS
   ▼
Nginx  (TLS termination, static assets, reverse proxy)
   │
   ▼
Uvicorn, ONE worker, systemd  (Python 3.12)
   └── FastAPI app
        ├── HTTP: upload protocol, download, room creation, storage figure
        ├── WebSocket /ws: presence, document RPCs, CRDT relay
        ├── RoomManager: dict[room_code, RoomInstance], asyncio timers
        ├── pycrdt Docs in memory, one per document
        ├── UploadManager: reservation ledger, .part files, reaper
        └── Filesystem: <DATA_ROOT>/rooms/<roomInstanceId>/
```

```
backend/app/
  main.py            ASGI entry point, lifespan (boot sweep, orderly shutdown)
  config.py          settings, validated at startup
  deps.py            service graph; the single place production and test wiring differ
  rooms/
    types.py         room data types, PeerConnection seam
    instance.py      RoomInstance, the lifecycle state machine
    manager.py       code allocation, lookup, the CLOSING handoff
  ws/
    routes.py        the /ws endpoint (Starlette transport)
    connection.py    Session: transport-agnostic per-connection logic
    messages.py      Pydantic v2 discriminated-union inbound models
    events.py        outbound event constructors
    heartbeat.py     ping/pong, timeout detection
  collab/
    registry.py      per-room document registry of pycrdt Docs
    sync.py          binary frame codec, sync and awareness relay
  files/
    routes.py        upload protocol + download endpoints
    uploads.py       upload state machine
    reservations.py  disk reservation ledger + asyncio.Lock
    storage_feed.py  periodic available-storage push
  storage/
    protocols.py     Clock, DiskSpaceProvider, FileStore (typing.Protocol)
    fs.py            real filesystem implementations
  cleanup/
    room.py          room cleanup, with verification and retry
    reaper.py        stale-upload sweep
    boot.py          data-root sweep on startup
  util/              ids, names, sanitize

frontend/src/
  routes/            landing, room, useRoom (all room state)
  editor/            CodeMirror binding, collab replica, clientID assignment
  files/             upload queue, file panel
  presence/          user list, toasts
  ws/                client, reconnect, resume token, wire protocol
  ui/                styles
```

**The server holds real CRDT documents, not opaque bytes.** That is what lets
it answer a reconnecting client with a state-vector diff instead of replaying
the room's whole history, and lets it enforce `MAX_DOC_BYTES` because it can
actually read the document length.

---

## Local setup

Requires **Python 3.12** and **Node 20+** (Node is a build-time dependency only;
it is not needed at runtime on the server).

```bash
# Backend
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

# Frontend, in a second terminal
cd frontend
npm install
npm run dev            # http://127.0.0.1:5173
```

The Vite dev server proxies `/api` and `/ws` to the backend, so the browser
only ever talks to one origin — the same shape Nginx provides in production.

**Do not expose the dev server publicly**, through a tunnel or otherwise. It
serves unminified source, and its Content-Security-Policy has to accommodate
Vite's Fast Refresh, so it is looser than the one you ship. To share or scan
something representative, serve the built bundle from the backend instead —
one process, and the real production headers:

```bash
cd frontend && npm run build && cd ../backend
SERVE_STATIC_DIR=../frontend/dist .venv/bin/uvicorn app.main:app     --host 127.0.0.1 --port 8000 --workers 1
```

Open <http://127.0.0.1:5173>, click **Create Room**, and open the resulting URL
in a second browser window to see collaboration working.

---

## Environment variables

Every one is documented with its default in
[`backend/.env.example`](backend/.env.example). Settings are validated at
startup, so a malformed value fails the process immediately rather than at the
first request. Nothing — no domain, port, or path — is hardcoded in the source.

The four that most change behaviour:

| Variable | Default | Effect |
|---|---|---|
| `ROOM_EMPTY_GRACE_MS` | `60000` | How long an empty room keeps everything before becoming unrecoverable. This is what makes refresh safe. |
| `USER_RECONNECT_GRACE_MS` | `30000` | How long a disconnected user keeps their identity, name, and CRDT clientID. |
| `WS_HEARTBEAT_INTERVAL_MS` | `20000` | Ping period. **Must stay comfortably below Nginx's `proxy_read_timeout`**; do not raise one without the other. |
| `DISK_HEADROOM_BYTES` | `2 GiB` | Never filled into, so the OS and logs are unaffected. |

---

## How conflict resolution actually works

This is the part worth reading before changing anything.

### Document text: nothing is discarded

"The later edit wins" is register semantics. Applied to collaborative text it
means throwing away one person's keystrokes, which is exactly what a CRDT
exists to prevent. The guarantee here is **convergence, not victory**: every
replica ends up in the same state, and no insertion is lost.

Ordering is decided in this order:

1. **Causal position.** Each inserted character records its left and right
   neighbours at the moment of insertion. Insertions at unambiguously different
   positions are ordered by position, and there is no conflict to resolve.
2. **Tie-break by join order.** When two insertions have *identical* neighbours
   — genuinely concurrent, at the same logical point — the tie is broken by
   comparing the two clients' CRDT client identifiers. The server assigns each
   user a `clientID` equal to the room's join sequence number, starting at 1.
   Join order therefore decides the outcome.
3. **Deletion is idempotent and commutative.** A character deleted concurrently
   by two people is deleted once. A character inserted concurrently with the
   deletion of its surrounding range **survives**; it is not swept up by a
   deletion that did not causally know about it.

### Where the clientID lives, and why it matters

The tie-break compares the ids of the clients that *authored* the insertions.
Insertions are authored in the browser. So the assigned number is set on the
**browser's** `Y.Doc`, before the editor binding is attached and before the
first sync frame is sent:

```ts
const doc = new Y.Doc();
doc.clientID = clientId;   // from the join acknowledgement
```

The server's own `pycrdt` document never authors an edit, and its client id is
irrelevant to ordering. Setting the id on the server document instead produces
a system that *appears* to work while silently breaking the join-order rule, so
there is an explicit test for it
(`e2e/test_collaboration_e2e.py::test_the_browser_doc_uses_the_server_assigned_client_id`).

This is why the join acknowledgement must arrive **before** any sync traffic:

```
open socket → send join → receive {userId, clientId, sessionToken} →
construct Y.Doc with that clientID → begin sync
```

A user who reconnects with a valid resume token keeps their original clientID,
which is why session resume is a prerequisite for the tie-break rule rather
than a convenience.

### Worked two-user example

Alice opens room 4827 first, so she is assigned `clientID 1`. Bob opens it
second and is assigned `clientID 2`. The document is empty. The network
partitions — neither browser has seen the other's edit.

```
        Alice (clientID 1)          Bob (clientID 2)
        types "AAA" at 0            types "BBB" at 0
        local state: "AAA"          local state: "BBB"
                    \                  /
                     \                /
                      network heals
                            │
                            ▼
                    both converge on:

                         "AAABBB"
```

**Observed rule, at the pinned versions: at a genuinely identical insertion
point, the LOWER clientID lands to the LEFT.** Because clientID is the join
sequence number, **the earlier joiner's text appears first**.

Both insertions are preserved. Neither user loses a keystroke. Alice, Bob, and
the server all hold the identical string.

This direction is asserted, not remembered, in two places that must agree:

- `backend/tests/test_collaboration.py::test_the_join_order_tie_break_direction_is_asserted_not_assumed`
  (Python replicas against the server)
- `e2e/test_collaboration_e2e.py::test_the_tie_break_direction_matches_the_python_implementation`
  (two real Chromium browsers, partitioned with `set_offline`)

**A caution that cost real debugging time.** If one client has already received
the other's insertion, the second insertion is causally *after* the first and
is ordered by position — with no tie-break involved at all. That case produces
`"BBBAAA"`, the apparent opposite. A test that types in two browsers without
partitioning them measures causality, not the tie-break, and will happily
assert the wrong rule. Both tests above take the replicas offline first, and
`test_a_causally_later_insertion_is_ordered_by_position_not_the_tie_break`
pins down the contrast deliberately.

### Scalar metadata: last write wins

Document names, display names, and similar single-value fields are not CRDT
text and *do* use last-write-wins — but ordering never comes from client clocks:

- the server keeps one monotonically increasing `opSeq` per room;
- every metadata mutation is stamped with the next `opSeq` on arrival;
- higher `opSeq` wins.

Because `opSeq` is assigned in the order the server processes messages, it is
total and ties are impossible by construction. **Client clocks are not
consulted for any ordering decision anywhere in the system.** Client-supplied
timestamps are accepted only as display metadata and are overwritten by the
server's clock.

### Undo

Undo uses `Y.UndoManager` with `trackedOrigins` restricted to the local user's
own operations. Pressing Ctrl+Z never reverts another participant's typing.
There is an explicit test: two users type, one undoes, the other's text is
asserted intact.

---

## How ephemeral cleanup is guaranteed

Follow one room from the moment the last person leaves.

### 1. `ACTIVE` → `EMPTY_GRACE`

The last connected user's socket drops. The room now has zero `CONNECTED`
users, so it enters `EMPTY_GRACE` and starts a `ROOM_EMPTY_GRACE_MS` timer.

**Nothing has been deleted.** Every document, file, and session token is still
in memory. A join during this window returns the user to the *same* instance,
with everything intact. This is what makes browser refresh safe, including for
someone who is the only person in the room.

A room enters `EMPTY_GRACE` on zero *connected* users regardless of how many
are still inside their own reconnect grace.

### 2. `EMPTY_GRACE` → `CLOSING` — the one-way door

If the timer elapses with nobody back, the room transitions to `CLOSING`. This
happens in exactly one place, `RoomInstance._on_empty_grace_elapsed`, and it is
irreversible. Any path that could still return the room to `ACTIVE` must have
cancelled that timer before now — joining does exactly that, first thing.

### 3. Cleanup, in an order that matters

```
1. Remove the room from the manager, so the code no longer resolves to it   ← the race fix
2. Reject all new operations for the instance
3. Abort in-flight uploads; release their reservations; delete their .part files
4. Close remaining sockets with a defined close code
5. Drop all pycrdt references, releasing the Rust allocations
6. rmtree <DATA_ROOT>/rooms/<roomInstanceId>, in a thread
7. VERIFY the directory no longer exists
8. Clear presence, timers, and the session-token table
9. Mark CLOSED
```

Step 6 runs through `asyncio.to_thread`. Deleting a directory holding tens of
gigabytes takes seconds, and doing it inline would freeze every other room in
the process.

Step 7 is a check, not an assumption. If the directory is still there, cleanup
retries with exponential backoff up to a bounded count and logs an operational
error. It never leaves room contents in place on the grounds that deletion
errored.

### 4. Why the race cannot resurrect data

The dangerous interleaving is this one:

```
A is the last user → A disconnects → EMPTY_GRACE elapses → CLOSING begins
                                        ↓
                          before cleanup finishes, B opens /room/4827
```

Two facts make it harmless:

1. **Step 1 removes the code from the manager before a single byte is
   deleted.** B's lookup therefore misses, and B receives a brand-new instance
   with a fresh `roomInstanceId`.
2. **Directories are keyed by instance UUID, not by the reusable room code.**
   The old instance is deleting `rooms/<old-uuid>/`; the new one is writing to
   `rooms/<new-uuid>/`. The paths are disjoint, so the collision is
   *structurally impossible* rather than merely unlikely.

So a room's contents can never survive because someone arrived during its
destruction, and a new room can never have its files deleted by an old room's
cleanup. Joins arriving during `EMPTY_GRACE` — before `CLOSING` — are the other
case, and they *do* resurrect the room with its state intact. The boundary
between the two is the single point where `CLOSING` is entered.

### 5. The three layers that catch everything else

Cleanup on room close is only one path. Uploads can be abandoned without one:

1. **Explicit** — abort, error, and room-cleanup paths all delete `.part` files
   and release reservations.
2. **Reaper** — a periodic sweep deletes any upload idle for `UPLOAD_STALE_MS`
   and releases its reservation. This catches the client that simply stops
   talking.
3. **Boot sweep** — on startup the server deletes the entire contents of
   `<DATA_ROOT>/rooms`. Since no room survives a restart, everything there is
   by definition garbage. This closes the crash-leaves-orphans gap that the
   other two cannot, and it is what makes "no persistence" true in practice
   rather than only in intent.

---

## The room lifecycle

```
                  first user connects
   (none) ─────────────────────────────────► ACTIVE
                                               │
                          last connection drops│
                                               ▼
                                          EMPTY_GRACE
                                          │         │
        a user joins/reconnects (same code)│         │ROOM_EMPTY_GRACE_MS elapses
                                          │         ▼
                                          │      CLOSING
                                          ▼         │
                                       ACTIVE       │ cleanup completes and is verified
                                                    ▼
                                                 CLOSED
```

Per-user states are separate from the room's: a user whose socket drops becomes
`DISCONNECTED` and is kept in the presence list, dimmed and labelled
"reconnecting", for `USER_RECONNECT_GRACE_MS`. Their awareness state (cursor,
selection) is cleared immediately; their identity is not. Clicking **Leave
Room** removes them at once with no grace.

**Session resume.** On first join the server issues a 128-bit `sessionToken`,
stored in `sessionStorage` — not `localStorage`, so it dies with the tab and
two tabs are correctly treated as two separate users. On reconnect the client
presents it, and the server rebinds the same `userId`, display name and
`clientID` if the user is still within their grace. An expired or unknown token
is never an error; the user simply joins as new.

---

## Uploads and the reservation ledger

Uploads go over HTTP, not the WebSocket, which keeps large binary transfers off
the collaboration channel and gives native progress events and backpressure.

| Step | Endpoint | Behaviour |
|---|---|---|
| Init | `POST /api/rooms/:code/uploads` | Validates, checks and **reserves** space, returns an `uploadId`. |
| Send | `PUT /api/uploads/:uploadId` + `Content-Range` | Appends to a single `.part` file. Rejects a range that does not begin exactly at the committed offset. |
| Query | `GET /api/uploads/:uploadId` | Returns `committedOffset` so a client can resume. |
| Complete | `POST /api/uploads/:uploadId/complete` | Verifies the size, fsyncs, atomically renames into `files/<fileId>`, releases the reservation, broadcasts. |
| Abort | `DELETE /api/uploads/:uploadId` | Deletes the `.part` file, releases the reservation. |

One part file with a byte offset, rather than N chunk files, means there is no
reassembly step and therefore no window in which a half-assembled file can be
observed.

**Why the ledger exists.** A bare pre-flight check passes two concurrent 40 GB
uploads against 50 GB of free space, and then both fail partway through. So:

```
availableForNewUpload = free_bytes − sum(active reservations) − headroom
```

**And why it needs a lock.** `bytes_available()` runs in a thread executor, so
check-then-reserve spans an `await`, so the event loop can interleave another
upload's check between your check and your reservation. Both then pass. "Python
is single-threaded" does not make this safe. The lock is held across
read-free-space, subtract, compare, and record — and released *before* any byte
is written, because it guards the ledger, not the transfer. There is a test
that fails against an unlocked ledger
(`test_the_ledger_lock_serializes_check_and_reserve`).

**FastAPI specifics.** `UploadFile` and `File(...)` are deliberately not used
anywhere: FastAPI spools multipart bodies into a `SpooledTemporaryFile`,
writing every chunk to disk a second time before the handler sees it, which
doubles transient disk usage and puts those bytes outside the ledger's
accounting. Chunks arrive as a raw `PUT` body read through `request.stream()`.
`python-multipart` is not even installed.

---

## Security model

**Stated plainly, because it is a deliberate design and not an oversight.**

Accepted by design:

- Room codes are four digits and **enumerable**. Anyone can scan the space and
  find active rooms.
- **Anyone with a room code has full access**: read every document, edit them,
  download every file, delete every file, delete every document.
- There is no confidentiality guarantee between rooms beyond the obscurity of
  the code.
- No malware scanning. Uploaded files are served as-is.

This is shown once in the room panel: *"Anyone with this room code can view,
edit, and delete everything here."*

Still enforced, as hygiene rather than access control:

- Every WebSocket message is validated against a Pydantic v2 discriminated
  union before it reaches a handler; oversized frames are rejected. A
  `ValidationError` never terminates a socket, though repeated abuse does.
- **No filesystem path is ever constructed from user input.** Paths are built
  only from server-generated UUIDs. A `fileId` that is not a well-formed UUID
  is rejected before it can reach the filesystem layer.
- File lookups are scoped to the room instance bound to the requested code, and
  a `fileId` from another room returns **404, never 403** — the response must
  not confirm that it exists elsewhere.
- Filenames, display names, and document names are all sanitized identically:
  NFC normalization, control and directional-override characters stripped,
  trimmed, length-capped. They are rendered as text, never as HTML.
- Downloads are served `Content-Disposition: attachment` with
  `X-Content-Type-Options: nosniff`, so an uploaded HTML or SVG cannot execute
  on the site's origin.
- No IP logging, no analytics, no personal information persisted.

---

## Testing

```bash
# Backend unit and integration tests (fast; no sleeping through timers)
cd backend && .venv/bin/pytest -q

# Type check
cd backend && .venv/bin/mypy

# End-to-end, real browsers against a real server
.venv/bin/playwright install chromium     # once
cd .. && backend/.venv/bin/pytest e2e -q

# Frontend type check and build
cd frontend && npm run typecheck && npm run build
```

Current state: **92 backend tests, 17 browser tests, `mypy --strict` clean.**

Three seams are injected rather than imported, because otherwise several of the
required tests cannot be written honestly:

- `Clock` — grace periods and reaper intervals are advanced instantly.
  **No test sleeps through a timer.** A suite that waited out
  `ROOM_EMPTY_GRACE_MS` would take minutes and get deleted by whoever
  maintained it next.
- `DiskSpaceProvider` — "insufficient disk space" is testable without filling a
  real volume.
- `FileStore` — upload interruption and orphan cleanup are testable directly.

Every two-user collaboration test drives two genuinely independent replicas.
One client sending two messages does not exercise concurrency and would pass
while the real behaviour was broken.

The cross-language guarantee is checked in a real browser against a real
server process. A pure-Python test of `pycrdt` against itself proves nothing
about the JavaScript-to-Python wire format.

---

## Production build

The frontend is built on a developer machine or in CI — Node is needed only
here — and the resulting `dist/` is copied to the instance, where Nginx serves
it directly rather than proxying it through Uvicorn.

```bash
cd frontend
npm ci
npm run build          # -> frontend/dist
rsync -av dist/ user@host:/opt/ephemeral-rooms/frontend/dist/
```

---

## EC2 deployment

Single instance. No S3, RDS, Redis, ECS, Kubernetes, or load balancer.

> **Setting this up from nothing?** [`docs/aws-setup.md`](docs/aws-setup.md) is the
> full runbook: instance, security group, Elastic IP, DNS, TLS, GitHub
> secrets, costs, teardown, and troubleshooting. This section is the summary.

### The short version

`deploy/bootstrap.sh` does steps 3-8 below in one go, and is idempotent, so it
is also how you ship a new build. It deliberately stops before certbot, because
that must not run until DNS actually resolves to the instance.

```bash
# 1. On your machine: build the frontend (Node is never installed on the server)
cd frontend && npm ci && npm run build && cd ..

# 2. Copy the repo across
rsync -av --exclude node_modules --exclude .venv --exclude .git ./ ubuntu@<elastic-ip>:/tmp/ephemeral-rooms/

# 3. On the instance
cd /tmp/ephemeral-rooms
sudo ./deploy/bootstrap.sh example.com          # --data-root /mnt/uploads to override
```

It preflights the instance (glibc, Python 3.12, a built frontend) before
changing anything, installs every dependency with `--only-binary=:all:` so a
missing wheel fails loudly instead of silently demanding a Rust toolchain,
refuses to install a unit file that has lost its `--workers 1`, and finishes
with a smoke test that creates a room through the API and fetches the landing
page through Nginx. It never overwrites an existing `/etc/ephemeral-rooms.env`.

Until certbot has run it installs an HTTP-only Nginx site so the ACME challenge
can be served; certbot then rewrites it with the TLS configuration.

### The long version

If you would rather do it by hand, or want to know what the script is doing:

**1. Provision.** Ubuntu 24.04 LTS, t3.small or larger. Size the root volume
for the uploads you expect; files live on local disk.

**2. Security group.** Inbound 22 (your IP only), 80 (`0.0.0.0/0`, needed for
ACME and the redirect), 443 (`0.0.0.0/0`). Nothing else. Uvicorn binds
`127.0.0.1` and is never reachable directly.

**3. System packages.**

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv nginx certbot python3-certbot-nginx
```

**4. Service user and directories.**

```bash
sudo useradd --system --home /opt/ephemeral-rooms --shell /usr/sbin/nologin ephemeral
sudo mkdir -p /opt/ephemeral-rooms /var/lib/ephemeral-rooms
sudo chown -R ephemeral:ephemeral /opt/ephemeral-rooms /var/lib/ephemeral-rooms
sudo chmod 700 /var/lib/ephemeral-rooms
```

**5. Application and virtualenv.**

```bash
# copy backend/ to /opt/ephemeral-rooms/backend and dist/ to
# /opt/ephemeral-rooms/frontend/dist
sudo -u ephemeral python3.12 -m venv /opt/ephemeral-rooms/.venv
sudo -u ephemeral /opt/ephemeral-rooms/.venv/bin/pip install \
     -r /opt/ephemeral-rooms/backend/requirements.txt
```

> **Check this before you deploy, not on the instance.** `pycrdt` is a compiled
> Rust extension. Confirm a manylinux wheel exists for the instance's Python
> version *and* CPU architecture (x86_64, or aarch64 on Graviton). If none
> matches, pip falls back to a source build and will demand a Rust toolchain.
> `pip download pycrdt==0.14.4 --only-binary=:all: --platform manylinux_2_17_x86_64 --python-version 3.12`
> answers this in a second.

**6. Configuration.**

```bash
sudo cp backend/.env.example /etc/ephemeral-rooms.env
sudo editor /etc/ephemeral-rooms.env       # set PUBLIC_ORIGIN and DATA_ROOT
sudo chown root:ephemeral /etc/ephemeral-rooms.env
sudo chmod 640 /etc/ephemeral-rooms.env
```

**7. systemd.**

```bash
sudo cp deploy/ephemeral-rooms.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ephemeral-rooms
sudo systemctl status ephemeral-rooms
journalctl -u ephemeral-rooms -f
```

**8. Nginx.**

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/ephemeral-rooms
sudo editor /etc/nginx/sites-available/ephemeral-rooms   # replace example.com
sudo ln -sf /etc/nginx/sites-available/ephemeral-rooms /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Restart and log inspection:

```bash
sudo systemctl restart ephemeral-rooms     # loses every active room, by design
journalctl -u ephemeral-rooms -n 200 --no-pager
```

---

## DNS

1. **Allocate an Elastic IP and associate it with the instance.** Without one,
   the public IP changes on stop/start, and both DNS and the certificate break.
2. `A` record for the apex → the Elastic IP.
3. `CNAME` (or a second `A`) for `www`.
4. **Verify propagation before running certbot**, which fails if DNS has not
   resolved yet:

```bash
dig +short example.com
dig +short www.example.com
```

---

## HTTPS with certbot

AWS Certificate Manager public certificates **cannot** be installed on an EC2
instance — they terminate only at an ALB, CloudFront, or API Gateway. Using ACM
would require adding a load balancer, and would additionally require raising
its default 60-second idle timeout to keep WebSockets alive. So: Let's Encrypt,
terminating at Nginx on the instance.

```bash
sudo certbot --nginx -d example.com -d www.example.com
sudo systemctl status certbot.timer     # renewal is automatic
sudo certbot renew --dry-run            # must pass before you call this done
```

`proxy_read_timeout 3600s` in the `/ws` block must comfortably exceed
`WS_HEARTBEAT_INTERVAL_MS`. Verify a socket survives past 60 seconds idle —
that is the timeout that catches people out.

The WebSocket URL is derived from `window.location`, so `https:` maps to `wss:`
automatically and a mixed-content error is impossible. It is never hardcoded.

---

## Verified version quintet

The browser runs `yjs`; the server runs `pycrdt`, which wraps `yrs`, the Rust
port of Yjs. Their binary-protocol compatibility is the assumption the entire
collaboration design rests on, so it is **verified rather than trusted**.

| Package | Version |
|---|---|
| `pycrdt` | **0.14.4** |
| `pycrdt-websocket` | **0.16.4** |
| `yjs` | **13.6.32** |
| `y-protocols` | **1.0.7** |
| `y-codemirror.next` | **0.3.6** |

None of the five carries a caret or tilde range.

**Upgrading any one of them requires re-running the compatibility test**
(`e2e/test_collaboration_e2e.py`) and confirming that the observed tie-break
direction still matches what this README documents and what
`backend/tests/test_collaboration.py` asserts.

Note that `pycrdt-websocket` is used only for reference; the protocol
primitives this project actually calls (`create_sync_message`,
`handle_sync_message`, `Awareness`, and the encoder/decoder) now ship in
`pycrdt` itself. Its `YRoom` and server classes are deliberately **not** used:
they are built for persistent rooms, and bending their lifecycle to fit the
ephemeral model would cost more than implementing it directly against the
protocol helpers.

---

## Known limitations

- **A server restart loses every room and every file.** This is accepted and
  intentional; simplicity and ephemerality take priority over durability. It is
  stated on the landing page.
- **Exactly one Uvicorn worker.** This application cannot be scaled
  horizontally, or even vertically by adding workers. All room state lives in
  one process's memory, so a second worker gets a second, empty copy of
  everything, and the failure is silent: two users of the same room code land
  in different, invisible copies of that room. Do not deploy under
  `gunicorn -k uvicorn.workers.UvicornWorker -w N`, which is the usual FastAPI
  production recommendation and is wrong here.
- **Room codes are enumerable and confer full access.** Four digits is 9000
  codes; anyone can scan them.
- **CRDT documents accumulate tombstones.** A long-lived, heavily-edited room
  grows in memory. Bounded in practice by `MAX_DOC_BYTES` and by rooms being
  short-lived.
- **Uploads require ordered chunks.** Resume works, but out-of-order chunk
  delivery does not; that is the cost of the single-part-file design.
- **Large uploads consume disk for their full duration**, and their reservation
  holds space against other uploads until they complete or are reaped.
- **`pycrdt` is a compiled extension**, so the deployment target's architecture
  must have a matching wheel.
- Capacity is bounded by one Python process on one instance: `MAX_ROOMS` 200,
  `MAX_USERS_PER_ROOM` 32. The workload is almost entirely I/O-bound and the
  CRDT merge work happens in Rust, so this is comfortable at this scale.
