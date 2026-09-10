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
- [**What bounds a room**](#what-bounds-a-room)
- [**How conflict resolution actually works**](#how-conflict-resolution-actually-works)
- [**How ephemeral cleanup is guaranteed**](#how-ephemeral-cleanup-is-guaranteed)
- [The room lifecycle](#the-room-lifecycle)
- [Uploads and the reservation ledger](#uploads-and-the-reservation-ledger)
- [Security model](#security-model)
- [Testing](#testing)
- [Production build](#production-build)
- [Docker](#docker) — [continuous deployment](#continuous-deployment), [the 10 GB room storage](#the-10-gb-room-storage)
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
the room's whole history, and what lets it say how long a document actually is
— which is how the room gets told what it is holding, and how an operator-set
`MAX_DOC_BYTES` is enforced at all. There is no such cap by default; see
[what bounds a room](#what-bounds-a-room).

---

## Local setup

Requires **Python 3.12** and **Node 20+** (Node is a build-time dependency only;
it is not needed at runtime on the server).

```bash
# Backend
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --ws-max-size 268435456

# --ws-max-size is not optional here: one paste is one WebSocket frame, and
# Uvicorn's 16 MiB default drops a larger one before the app ever sees it.

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

Build once, then point `SERVE_STATIC_DIR` at the result. The tidiest way is to
uncomment that line in `backend/.env`, which needs no shell-specific syntax:

```bash
cd frontend && npm run build            # -> frontend/dist
```

```ini
# backend/.env
SERVE_STATIC_DIR=../frontend/dist
```

```bash
cd backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --ws-max-size 268435456
```

On Windows the launcher is `.venv\Scripts\uvicorn.exe`. Everything is then on
**<http://127.0.0.1:8000>** — the page, the client-side routes, the API and the
WebSocket — so that is the port to tunnel or point a browser at.

To set it for a single run instead of editing `.env`:

```bash
# bash
SERVE_STATIC_DIR=../frontend/dist .venv/bin/uvicorn app.main:app --port 8000 --workers 1 --ws-max-size 268435456
```

```powershell
# PowerShell - an inline VAR=value prefix is a parser error here
$env:SERVE_STATIC_DIR = "../frontend/dist"
.venv\Scripts\uvicorn.exe app.main:app --port 8000 --workers 1 --ws-max-size 268435456
```

This serves the built files, so there is no hot reload: rebuild after any
frontend change.

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

## What bounds a room

**Nothing fixed does.** There is no cap on how much you can type into a
document, no cap on how large a file may be, and no cap on how many of either a
room may hold. A room fills up when the *server* is full, and not before.

That is a deliberate reversal of the usual arrangement, for a simple reason: a
number chosen in advance is either smaller than the machine can carry — in which
case it refuses work there was room for — or larger, in which case it protects
nothing. So the limit is measured instead, and it is measured against the thing
each kind of content actually consumes.

| What | Bounded by | Where |
|---|---|---|
| Files | free disk − outstanding reservations − `DISK_HEADROOM_BYTES` | [`reservations.py`](backend/app/files/reservations.py) |
| Document text | free memory − `MEMORY_HEADROOM_BYTES` | [`memory.py`](backend/app/storage/memory.py) |

Both figures are pushed to every room and shown in the status bar as
**Storage left** and **Memory left**, refreshed on a timer so each one reflects
what everyone else is doing rather than only what this browser has done.

**Why memory, and not just disk.** Documents are the one thing a room holds
that never reaches the filesystem: every CRDT document lives in the backend
process. With no `MAX_DOC_BYTES` there would otherwise be nothing at all
bounding how much text a room can accumulate, so memory gets exactly the
treatment the disk already had — a reserved slice the application will not
spend, and a guard that reports when what is left has fallen into it. The
reading comes from the container's cgroup limit where there is one, so it
reflects `--memory` rather than the host's total, and falls back to
`/proc/meminfo`'s `MemAvailable`.

**The memory guard never refuses an edit.** An update that has arrived is always
applied. Rejecting it would leave that browser holding text the server does not
have, and a silently diverged CRDT is a far worse failure than a room that is
merely large. What happens instead is that the room is told — once, then at most
every 30 seconds — so someone can delete a document or a file. A platform that
exposes no memory reading at all is treated as "not under pressure", never as
"full": refusing to work because a metrics file could not be parsed would be the
wrong trade by a wide margin.

**Files still get a hard refusal**, because they can: an upload declares its
size before it starts, so the ledger can reserve atomically and say no before a
byte is written. That is the whole point of §17, and it is unchanged.

**One ceiling that is not the application's.** One paste into the editor is one
Yjs update is one WebSocket frame, and Uvicorn closes the socket with 1009 on a
frame past `--ws-max-size` — 16 MiB by default — before the application sees any
of it. `WS_MAX_FRAME_BYTES` (256 MiB) is passed through by
[`docker-entrypoint.sh`](backend/docker-entrypoint.sh); a deployment that starts
Uvicorn some other way must pass it too, or it silently caps how much text a
person can paste at once.

**The knobs still exist.** `MAX_DOC_BYTES`, `MAX_DOCS_PER_ROOM`,
`MAX_FILE_BYTES`, `MAX_FILES_PER_ROOM` and `MAX_ROOM_TOTAL_BYTES` all still
work; they simply default to `0`, which means "no application limit". Set one
when the server needs a ceiling smaller than the machine it runs on — a shared
host, most obviously. `MAX_UPLOADS_PER_USER` is not one of these: it bounds how
many transfers a browser opens *at once*, and the client queues the rest, so
every file you choose still arrives.

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

## Docker

Three containers. Two of them are the systemd deployment packaged — Uvicorn
holding all the room state, and Nginx serving the built frontend and
reverse-proxying `/api` and `/ws` to it. The third is `cloudflared`, which
carries public traffic in from the Cloudflare edge and does nothing else. Node
lives in a build stage and is not in any shipped image, so the runtime has no
Node on it — exactly as the EC2 instance does not.

```bash
cp .env.example .env           # fill in the tunnel UUID and credentials path
docker compose up -d --build   # local: http://127.0.0.1:8080
```

```
                internet
                    |  https
            +-------v--------+
            | Cloudflare edge|   TLS terminates here
            +-------+--------+
                    |  QUIC, outbound-initiated - no inbound port on your host
         +----------v----------+
         | tunnel  cloudflared |  routing only, holds no state
         +----------+----------+
                    |  http://web:80        (compose network)
         +----------v----------+
         | web     nginx       |  dist/, security headers, SPA fallback
         +----------+----------+
                    |  http://backend:8000  (never published to the host)
         +----------v----------+
         | backend uvicorn     |  every room, doc, presence entry, reservation
         +----------+----------+
                    |
        rooms.img on the room-backing volume
        (fixed-size ext4, mounted by the entrypoint, wiped on every start)
```

| File | What it is |
| --- | --- |
| [`backend/Dockerfile`](backend/Dockerfile) | Uvicorn, one worker, non-root, Debian slim |
| [`backend/docker-entrypoint.sh`](backend/docker-entrypoint.sh) | Mounts the fixed-size room filesystem, then drops privileges |
| [`frontend/Dockerfile`](frontend/Dockerfile) | Node build stage → Nginx serving `dist/` |
| [`deploy/docker/nginx.conf`](deploy/docker/nginx.conf) | [`deploy/nginx.conf`](deploy/nginx.conf) adapted to the compose network |
| [`docker-compose.yml`](docker-compose.yml) | Wiring, volume, health gates, restart policies |
| [`.env.example`](.env.example) | Template for `.env` — tunnel identity and public origin |
| [`.dockerignore`](.dockerignore) | Shared by both builds — both use the repo root as context |

The response headers come from [`deploy/security-headers.conf`](deploy/security-headers.conf),
the same file the systemd deployment installs, copied into the web image at
build time. One source of truth, so the two deployments cannot drift apart on
something as easy to get wrong as a CSP.

### The tunnel container

It is a router, not a part of the application. It dials **out** to Cloudflare
and forwards what comes back to Nginx, so the host needs no inbound port, no
public IP and no certificate of its own — TLS terminates at the edge. Restarting
it drops inbound connections and nothing else; restarting `backend` is what
loses rooms.

Two deliberate choices in [`docker-compose.yml`](docker-compose.yml):

- **Only the tunnel's credentials JSON is mounted**, read-only — not the whole
  `~/.cloudflared` directory. `cert.pem` in there is an account-wide credential
  that can create and delete tunnels, and `cloudflared tunnel run` does not need
  it when the tunnel is named by UUID.
- **It points at `http://web:80`, not the backend.** Everything public goes
  through Nginx, so static assets, the SPA fallback and the security headers
  behave publicly exactly as they do locally.

Ingress is expressed as `--url http://web:80` rather than a config file,
because this tunnel serves one hostname backed by one service. That keeps the
hostname out of the repository, where nothing else hardcodes a domain either
(spec section 31); the hostname lives in the Cloudflare DNS record pointing at
the tunnel UUID. If you ever need real ingress rules — several hostnames, path
routing, per-origin timeouts — add a `config.yml` and mount it at
`/etc/cloudflared/config.yml`.

`REDIRECT_HTTP_TO_HTTPS=true` is correct behind the tunnel. Cloudflare sets
`X-Forwarded-Proto` to the scheme the visitor actually used and Nginx passes it
through untouched, so the redirect only fires for a genuine plain-HTTP visitor —
which is the case it exists for, a tunnel without **Always Use HTTPS** enabled.

### Keeping it running

All three services are `restart: unless-stopped`, so they come back after a
crash, after a Docker restart, and after a reboot. Verified by restarting the
Docker daemon: all three returned on their own, the tunnel re-established its
four edge connections, and the site answered — no command typed.

> **One manual step, and without it none of the above happens.** Restart
> policies only run when the Docker daemon is running, and Docker Desktop does
> **not** start itself by default. Turn on **Settings → General → Start Docker
> Desktop when you log in**. Note it is *login*, not boot: after a reboot the
> stack comes up once you sign in, not at the login screen.

`unless-stopped` deliberately respects a deliberate stop. If you run
`docker compose stop`, the containers stay down across reboots until you start
them again — Docker records that you meant it. `docker kill` counts as a manual
stop too, which is why a killed container does not bounce back. Swap the policy
to `always` in [`docker-compose.yml`](docker-compose.yml) if you would rather a
reboot override even a deliberate stop.

### Controlling the application

Run these from the repository root. Everything is `docker compose`, so it acts
on the whole stack unless you name a service.

**Everyday**

```bash
docker compose up -d              # start everything, in the background
docker compose ps                 # what is running, and whether it is healthy
docker compose logs -f            # follow all three; Ctrl-C detaches, stops nothing
docker compose logs -f backend    # just one service
docker compose stop               # stop, and stay stopped across reboots
docker compose start              # undo that
docker compose restart backend    # restart one service
docker compose down               # stop and remove containers, keep the volume
```

**After changing things**

| You changed | What to run |
| --- | --- |
| `.env` (origin, tunnel, ports) | `docker compose up -d` |
| `deploy/docker/nginx.conf` | `docker compose up -d --build web` |
| Backend Python under `backend/app/` | `docker compose up -d --build backend` |
| Frontend under `frontend/src/` | `docker compose up -d --build web` |
| A Dockerfile or `requirements.txt` | `docker compose up -d --build` |

There is no hot reload — these are production images. Edit on the host, rebuild,
and the affected container is replaced. Rebuilding `backend` destroys every open
room; rebuilding `web` or `tunnel` does not.

**Looking inside**

```bash
docker compose exec backend sh              # shell in the backend (as the app user)
docker compose exec backend env | sort      # what settings it actually resolved
docker compose exec backend du -sh /var/lib/ephemeral-rooms   # room files on disk
curl 127.0.0.1:2000/ready                   # tunnel: edge connections established
docker stats --no-stream                    # CPU and memory per container

# Free space the app will admit to. The header is not optional - see below.
curl -H "X-Forwarded-Proto: https" 127.0.0.1:8080/api/storage
```

**Why that header.** With `REDIRECT_HTTP_TO_HTTPS=true` a plain-HTTP request to
`/api/...` on loopback gets a `301` to `https://127.0.0.1:8080`, which nothing
is listening on — the application is doing exactly what it was told, because
from its point of view a proxy just reported a plain-HTTP visitor. Real traffic
is unaffected: Cloudflare sends that header for you. Pages still load on
loopback without it, because Nginx serves `dist/` itself and never asks the
backend. Set `REDIRECT_HTTP_TO_HTTPS=false` in `.env` if you would rather work
locally without the header.

`readyConnections` from that `/ready` endpoint is the honest answer to "is the
site actually up?" — four is normal, zero means the edge cannot reach you. The
cloudflared image is distroless and has no shell, so it carries no healthcheck
of its own and `exec` into it will not work; its logs and that endpoint are the
whole diagnostic surface.

**Starting clean**

```bash
docker compose down -v            # also deletes rooms.img, so the next start
                                  # makes a fresh one - this is how you resize it
```

Always safe: the boot sweep empties the data root on every start anyway, so
nothing in that volume was going to survive.

### Continuous deployment

Push to `main` → CI proves the source → both images are built, **run**, and only
then pushed to Docker Hub → Watchtower on the deployment machine notices the new
digest and recreates `web`.

```
push to main
   │
   ├─ backend · frontend · e2e · pinned-versions     must all pass
   │
   └─ images    build ─▶ run it ─▶ push :sha-<commit>
                                        │
                     promote ─▶ move :latest ─▶ POST /v1/update ─┐
                                        │                        │
                                        │  Docker Hub            │ tunnel
                                        ▼                        ▼
                          watchtower ──poll──▶ new digest? ◀── triggered
                                        │
                                        ▼
                            pull + recreate  backend + web
```

**Two paths to the same check, and that is deliberate.** The webhook is the
fast one: `promote` POSTs to Watchtower the moment `:latest` moves, so a release
lands in seconds. The poll is the floor, and it is what makes the fast path
safe to depend on — a machine that was asleep, offline or mid-reboot when CI
ran picks the same images up on its own later. Neither is load-bearing alone.

Shortening the poll instead of triggering it is the obvious fix and the wrong
one: every check is a manifest request against Docker Hub's pull limit, so two
images at one-minute polls is a few thousand requests a day, and hitting that
limit presents as "the deploy silently stopped working".

No port is opened and there is no SSH key anywhere in this pipeline. The poll is
outbound, and the trigger arrives through the tunnel the machine already dials
out to — so the "no inbound port" property survives even though CI can now reach
in. What the webhook token permits is narrow by construction: it makes Watchtower
check the registry and pull `:latest`, the image CI just published. It cannot
name a different image, run a command, or read anything.

**Both published services update themselves.**

| | Auto-updates | Why |
| --- | --- | --- |
| `web` | **yes** | Recreating Nginx drops inbound connections and nothing else |
| `backend` | **yes** | A release should land without a person in the loop |
| `tunnel` | no | Its version is pinned deliberately |
| `watchtower` | no | Updating the updater mid-run is a needless way to lose a deploy |

**Recreating the backend destroys every live room, document and upload, with no
warning to anyone in them.** That is accepted here rather than worked around:
the rooms are ephemeral and short-lived by design, a release is not a rare
event worth choreographing around, and a backend lagging the frontend it was
tested against is its own kind of bug. A deploy is a few seconds of downtime
and an empty slate, which is what this application is.

If you do want a particular release held back, stop the poller rather than
un-labelling the service — the label is what the status script checks:

```bash
docker compose stop watchtower          # freeze deployments
docker compose --profile watchtower up -d   # resume; lands at the next poll
```

#### The tagging strategy, and why it is both

Every build pushes `sha-<commit>` **and** `latest`.

| | `latest` | `sha-<commit>` |
| --- | --- | --- |
| Rollback | impossible from the tag alone | exact, permanent |
| "What is running?" | unanswerable | unambiguous |
| **Watchtower can act on it** | **yes** | **no** |

That last row is the reason you cannot simply pin SHAs everywhere. Watchtower
detects updates by re-resolving *a fixed tag name* and comparing digests. Pin
`sha-abc123` in `.env` and the digest behind it never changes, so Watchtower has
nothing to detect — it will sit there forever, doing nothing, looking healthy.

So the deployed reference is the moving tag, and the `sha-` tags accumulate as a
rollback ledger. Pinning a `sha-` tag deliberately is also how you *freeze* a
service — useful while investigating, and how `backend` is promoted.

#### Setting it up, once

**1. Docker Hub — two tokens, not one.** *Account Settings → Personal access
tokens.*

| Token | Scope | Goes to |
| --- | --- | --- |
| CI push token | Read & Write | GitHub Actions secret |
| Server poll token | **Read-only** | `.env` on the machine |

Separate, because the Watchtower container mounts the Docker socket — which is
root-equivalent on that machine. A read-only token means compromising it still
cannot publish a poisoned image to your account. Never a password: a token is
scoped and revocable on its own.

**2. GitHub — Settings → Secrets and variables → Actions.**

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `DOCKERHUB_USERNAME` | Your Docker Hub account name |
| Secret | `DOCKERHUB_TOKEN` | The read/write token |
| Variable | `PUBLISH_IMAGES` | `true` — the on-switch |

`PUBLISH_IMAGES` is deliberately last: until it is `true` the `images` job is
skipped, so all of this can be merged and sit inert.

**3. The machine — `.env`, which deployment never writes.**

```
DOCKERHUB_NAMESPACE=your-dockerhub-username
DOCKERHUB_USERNAME=your-dockerhub-username
DOCKERHUB_READ_TOKEN=<the read-only token>
WATCHTOWER_POLL_INTERVAL=900
```

**4. Set `WATCHTOWER_HTTP_TOKEN`** in `.env`, and the same value as a GitHub
Actions secret of that name, plus `DEPLOY_HOOK_URL` pointing at
`https://<your-hostname>/v1/update`. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`. Watchtower
refuses to start without it once the endpoint is enabled, and CI falls back to
the poll if the secrets are absent — so a fork with neither still works, just
more slowly.

**5. Start Watchtower.** It is behind a profile, so `docker compose up -d` on a
laptop never silently starts something that replaces containers from a registry.

```bash
docker compose --profile watchtower up -d
```

Once started it stays started — `unless-stopped` carries it across reboots
without naming the profile again.

#### Is the deployment current?

One command, rather than four things to remember:

```bash
./deploy/docker/status.sh
```

It reports each link in the chain separately - the image digests running versus
published, the containers, Watchtower's scope, and the bundle actually being
served - so a stalled deployment localises to a stage instead of reading as "it
didn't work". Exit status is 0 when everything watched is current and 1 when it
is not, so it also works as a check in a script.

The comparison it makes is `:latest` against `:latest`, never against a `:sha-`
tag. `promote` re-points `:latest` with `imagetools create`, which wraps the
image in a new manifest **index** - so `:latest` and `:sha-<commit>` report
different digests for byte-identical content, and comparing across the two
produces a confident, entirely wrong "STALE".

#### Testing the whole thing, end to end

Do this in order. Each step is checkable on its own, so a failure tells you
which stage broke rather than "it didn't deploy".

```bash
# 1. Locally: does compose resolve to the images you expect?
docker compose config | grep image:
#    -> your-name/ephemeral-rooms-backend:latest, .../web:latest

# 2. Push a visible frontend change to main, then watch the Actions tab.
#    The `images` job must go green. It builds, RUNS the image, then pushes.

# 3. Did the tags actually land? (nothing local involved)
docker buildx imagetools inspect your-name/ephemeral-rooms-web:latest

# 4. On the machine: is Watchtower watching both published containers?
docker compose logs -f watchtower
#    -> "Update session completed ... scanned=2"
#       2 is backend and web. tunnel and watchtower are NOT in scope.

# 5. Did CI's trigger arrive? Check the `Trigger the deployment` step in the
#    run - it prints the status and Watchtower's JSON summary. Or call it
#    yourself, which is the same request CI makes (POST, not GET):
curl -sS -X POST -H "Authorization: Bearer $WATCHTOWER_HTTP_TOKEN"   https://<your-domain>/v1/update
#    -> {"summary":{"scanned":2,"updated":1,...}}
#    401 = token mismatch. 405 = you sent a GET. 404 = the tunnel is not
#    routing that path.

# 6. Did the containers actually change?
docker compose ps
docker inspect -f '{{.Image}}' $(docker compose ps -q backend)

# 7. Is the site up, on the new build?
curl -fsS https://<your-domain>/api/version
```

**Restarting the Watchtower service does not force a check** — it only resets
the timer, logging `Next scheduled run: ... in 14 minutes 59 seconds` and doing
nothing until then. That is worth knowing before you use it as a diagnostic:
restart, see no update, and it is easy to conclude the pipeline is broken when
it is merely waiting.

To force a check now, the ordinary way is the webhook CI uses — a POST to
`/v1/update` with the token, which is one HTTPS request and needs nothing local.
Failing that (no token to hand, or the tunnel is the thing you are debugging),
run a one-off container against the same socket. It scans using the same labels
and exits:

```bash
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock   nickfedor/watchtower:1.22.1 --label-enable --run-once
```

Add `--monitor-only` to see what it *would* update without touching anything —
the safest way to confirm scope, and it should report `scanned=2`.

#### If something breaks, in stage order

| Symptom | Where to look | Usual cause |
| --- | --- | --- |
| `images` job skipped entirely | Actions → the run | `PUBLISH_IMAGES` is not `true`, or it was not a push to `main` |
| Fails at "Work out the image name" | job log | `DOCKERHUB_USERNAME` secret missing |
| Fails at **Build** | job log | A real build error — the same one you would get locally |
| Fails at **Smoke-test** | job log; container logs are dumped | The image builds but does not run. This is the check earning its keep — nothing was pushed |
| Fails at **Push** | job log | Bad or expired `DOCKERHUB_TOKEN`, or it lacks Write scope |
| Tag exists but the server never updates | `docker compose logs watchtower` | See the four rows below |
| ↳ `scanned=0` | | The label is missing — the container predates it. Run `docker compose up -d web` once to apply it |
| ↳ a `401` from the registry | | `DOCKERHUB_READ_TOKEN` wrong or expired |
| ↳ `toomanyrequests` | | Docker Hub rate limit — authenticate, and raise `WATCHTOWER_POLL_INTERVAL` |
| ↳ nothing logged at all | | Watchtower is not running: `docker compose --profile watchtower up -d` |
| Release published, nothing happened for 15 min | the `Trigger the deployment` step in the run | The webhook failed and the poll caught it. `401` = token mismatch between `.env` and the `WATCHTOWER_HTTP_TOKEN` secret; `405` = something is doing a GET, it must be a POST; `404` = the tunnel's path rule is not routing `/v1/update`; `000` = the machine was unreachable, which is what the poll is for |
| Watchtower says `updated=1`, site looks unchanged | browser | Cached bundle. Hard-reload; asset filenames are hashed, so this is nearly always the browser |
| Container updated, then restarts in a loop | `docker compose logs web` | A bad image was published. Roll back (below) |

Verifying a stage without triggering the one before it: step 3 checks the
registry with no server involved; step 4 checks the server with no push
involved. That is deliberate — you can always bisect down to a single stage.

#### Rolling back

Every commit ever published is still in the registry under its own `sha-` tag,
so rolling back is a re-tag, not a rebuild.

**From GitHub, no SSH:** Actions → **Roll back** → *Run workflow* → pick the
service, paste the commit SHA. It checks the target exists, re-points `:latest`
at it inside the registry, and Watchtower recreates the container on its next
poll — the same path a deployment takes, which is the path you have actually
tested.

**On the machine, immediately:**

```bash
WEB_IMAGE=your-name/ephemeral-rooms-web:sha-<good-commit> docker compose up -d web
```

`BACKEND_IMAGE` pins the backend the same way. Note that pinning a service to a
`sha-` tag takes it out of the flow of releases: Watchtower follows the tag it
was given, so a pinned container stops tracking `:latest` until you unpin it.

#### Gotchas worth knowing before you rely on this

**A backend deploy destroys every live room.** Not downtime — data loss, by
design. Rooms, documents, presence and uploads all live in one process's memory
on a volume wiped at start, and a release recreates that container unattended,
so it happens at whatever moment the poll finds a new digest. Anyone mid-session
is disconnected with no warning and their room does not come back. That is the
accepted trade for releases that land on their own; if a particular one needs to
wait, stop the poller rather than un-labelling the service.

**Docker Hub rate limits.** Every poll is a manifest request counting against
your quota, and the limits have been tightened more than once — check the
current numbers rather than trusting a figure written here. Two mitigations, and
you want both: authenticate the poll (`DOCKERHUB_READ_TOKEN`), and keep
`WATCHTOWER_POLL_INTERVAL` conservative. Two images at 900s is ~192 checks a day.
A limit hit here looks exactly like "deployment quietly stopped working".

**`containrrr/watchtower` does not work on modern Docker.** Its last release
defaults to Docker API v1.25; Engine 29 refuses anything below v1.40, and the
failure is a nil-pointer panic rather than a clean error. That is why this stack
pins the maintained `nickfedor` fork, which negotiates the version itself. Both
read the same label, so swapping back is a one-line change if that ever
reverses.

**The Docker socket is root-equivalent.** Watchtower can control every container
on the machine. Do not publish a port from it, and keep its registry token
read-only.

**Watchtower recreates containers outside Compose.** It copies the running
container's configuration — labels, ports, capabilities and devices all survive
(verified). But the container was not created by `docker compose up`, so a later
`docker compose up -d` may recreate it once more to reconcile. Harmless for
`web`; it is one more reason `backend` is left alone.

**On Docker Desktop, updates only land while the machine is on** and you are
logged in — Docker Desktop does not start at boot by default. See
[Keeping it running](#keeping-it-running). A machine that sleeps overnight
simply picks the update up when it wakes.

**Old images accumulate.** `WATCHTOWER_CLEANUP=true` removes the superseded
image after each successful update, and is on by default here. Without it, a
deployment a day quietly fills the disk.

### Configuration

Machine-specific values live in `.env` beside `docker-compose.yml` — see
[`.env.example`](.env.example). Anything in
[`backend/.env.example`](backend/.env.example) can also be added under
`environment:` for the `backend` service. Settings are validated at startup, so
a malformed value stops the container immediately rather than at the first
request; `docker compose logs backend` will name the offending field.

Room files go on a named volume rather than the container's writable layer, so
large uploads do not grow the overlay and free-space checks see a real
filesystem. How much that volume may consume is covered in
[The 10 GB storage budget](#the-10-gb-storage-budget) below.

### The 10 GB room storage

Room files live on a **fixed-size ext4 filesystem**, not on the volume directly.
[`backend/docker-entrypoint.sh`](backend/docker-entrypoint.sh) creates a sparse
`rooms.img` of `ROOM_DISK_SIZE` on the `room-backing` volume, mounts it at
`DATA_ROOT`, and only then hands over to Uvicorn. So the limit is the kernel's:
`statvfs` reports 10 GB, and a write past it gets `ENOSPC`.

```
room-backing volume  ──  rooms.img (sparse, 10 GB apparent / 68 MB actual)
                            │  mount -o loop, by the entrypoint, every start
                            ▼
                     /var/lib/ephemeral-rooms   ext4, 9.8 GB, dedicated
```

Nothing in the application enforces this; `MAX_TOTAL_STORAGE_BYTES` is `0`.

**Why the entrypoint and not Docker.** Docker cannot mount a loop-backed
filesystem: the `local` volume driver calls `mount(2)`, which has no `loop`
handling — `-o loop` is a `mount(8)` userspace feature — so
`--opt o=loop` fails with `data: loop: invalid argument`. Pointing a volume at a
pre-attached `/dev/loopN` does work, but leaves a Docker object pointing at a
kernel object that does not survive a restart, and the failure is unrecoverable:

```
$ docker run -d --restart unless-stopped -v rooms-data:/data alpine sleep 999
Error: failed to mount local volume: mount /dev/loop7: input/output error
$ docker ps -a
looptest: Created          ← not "Restarting". A restart policy never retries
                             a container that failed to start.
```

Mounting inside the container removes both problems. There is no ordering to get
right, because the setup and the application are the same process tree; and the
whole thing is redone from scratch on every start, which is exactly what makes
it survive a reboot with nothing typed.

**Privileges.** The container gets `SYS_ADMIN` (for `mount`) and `MKNOD` (a
container's `/dev` has no `loop*` nodes), plus `/dev/loop-control` and a
`b 7:* rmw` device-cgroup rule. Not `privileged: true`, which most examples
reach for and which would hand over the whole host device tree to cap one
directory. The entrypoint gives all of it up before the server starts:

```
$ docker compose exec backend cat /proc/1/status
Name:    uvicorn
Uid:     10001 10001 10001 10001     ← ephemeral, not root
CapEff:  0000000000000000            ← no capabilities at all
```

`setpriv --clear-groups --inh-caps=-all` is what does that, and `exec` means
Uvicorn replaces the script as PID 1, so it still receives `SIGTERM` normally.

**Verified.** Writing past the limit as the application's own user:

```
$ dd if=/dev/zero of=fill bs=1M count=12000
9963+0 records out, 10446962688 bytes (9.7 GiB) copied, 15.5 s, 673 MB/s
/dev/loop2   9.8G  9.8G  220K  100%  /var/lib/ephemeral-rooms
```

Asked for 12 GB, got 9.8 GB and `ENOSPC`. And across a full Docker Desktop
restart, with nothing typed:

```
entrypoint: creating a 10G image at /backing/rooms.img      ← first boot
entrypoint: /dev/loop2  9.8G  24K  9.8G  1%  /var/lib/...
entrypoint: /dev/loop2  9.8G  28K  9.8G  1%  /var/lib/...   ← after restart:
                                                              mounted, not recreated
```

**Two things to know.**

The image is sparse, so it claims 10 GB and occupies only what has been written
— but it never shrinks. Delete a 9 GB room and the filesystem inside has 9 GB
free again while `rooms.img` stays 9 GB on the host, because that is its
high-water mark. `docker compose down -v` discards it and the next start makes a
fresh one; that is also the only way to change `ROOM_DISK_SIZE`, since the
entrypoint creates an image only when none exists.

`DISK_HEADROOM_BYTES` still matters with a real filesystem underneath: it stops
the volume being filled to the very last byte, where the reaper and the cleanup
paths would themselves have no room to work. Usable space is the image size
minus the headroom — 9.8 GB − 512 MiB, which is the ~9.23 GiB `/api/storage`
reports.

`MAX_ROOM_TOTAL_BYTES` is `0`, so a room may use whatever the disk has left.
There is no per-room share and the free-space check is first-come-first-served,
which does mean one busy room can take most of the volume — the deliberate
trade, since a room refusing a file the server had space for is the worse
outcome. Set it to a byte count if you would rather cap each room.

**The application-level alternative.** `MAX_TOTAL_STORAGE_BYTES` still exists
and is tested — [`BudgetedDiskSpace`](backend/app/storage/fs.py) caps the figure
the ledger reads, at `min(real free, budget − in use)`. It is the right tool when
the data root is a volume you cannot size, and it needs no privileges at all.
Here the filesystem does the job better, so it is set to `0`.

### Four more things worth knowing before you change any of it

**Do not scale the backend.** `--scale backend=2` is not a performance knob
here; it is two separate, invisible copies of the application. Every room,
document, presence entry, session token and disk reservation lives in one
process's memory, so two users typing the same room code would land in
different rooms and each see an empty one. One replica, one worker — the same
constraint [`deploy/ephemeral-rooms.service`](deploy/ephemeral-rooms.service)
carries, for the same reason (spec section 20.1).

**`REDIRECT_HTTP_TO_HTTPS` depends on what is in front.** `.env` sets it to
`true`, which is right behind the tunnel: Cloudflare reports the visitor's real
scheme and Nginx passes it through, so the redirect only fires for a genuine
plain-HTTP visitor. Running the stack with nothing in front — straight at
`http://127.0.0.1:8080` with the tunnel stopped — set it to `false`, or the web
container's own `X-Forwarded-Proto: http` will make the application 301 every
request to an `https://` URL nothing is listening on.

**The backend port is deliberately not published.** Only the web container can
reach it, which is what makes `--forwarded-allow-ips '*'` safe: nothing else
can get close enough to forge a forwarding header. Publishing port 8000 to the
host would invalidate that, and would also route around every security header
Nginx adds.

**Debian slim, not Alpine.** `pycrdt` is a compiled Rust extension published
as manylinux (glibc) wheels only. On musl, pip finds no wheel and falls back to
a source build needing a full Rust toolchain. If a `pip install` in that image
ever starts invoking a compiler, a wheel is missing for the platform — fix the
pin rather than installing gcc and hiding it.

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
  grows in memory, and no size cap stands in the way by default. What bounds it
  is `MEMORY_HEADROOM_BYTES`, which reports the pressure rather than refusing
  the edit — and, in practice, rooms being short-lived.
- **Uploads require ordered chunks.** Resume works, but out-of-order chunk
  delivery does not; that is the cost of the single-part-file design.
- **Large uploads consume disk for their full duration**, and their reservation
  holds space against other uploads until they complete or are reaped.
- **`pycrdt` is a compiled extension**, so the deployment target's architecture
  must have a matching wheel.
- Capacity is bounded by one Python process on one instance: `MAX_ROOMS` 200,
  `MAX_USERS_PER_ROOM` 32. The workload is almost entirely I/O-bound and the
  CRDT merge work happens in Rust, so this is comfortable at this scale.
