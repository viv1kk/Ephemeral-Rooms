# Ephemeral Collaborative Room Web Application

**Specification v3 (amended)**

---

## Amendment Log

### v3: Python / FastAPI backend

The backend moves from Node.js to Python and FastAPI at the user's direction. **No product behaviour, lifecycle rule, or conflict-resolution semantic changes.** The collaboration guarantees in §7 survive intact because the Python CRDT binding is binary-protocol compatible with the browser's Yjs.

| # | Area | Change |
|---|---|---|
| B1 | Stack | §0 backend replaced: Python 3.12, FastAPI, Uvicorn, `pycrdt`, `pytest`. Frontend and deployment shape unchanged. |
| B2 | CRDT | `pycrdt` (Rust `yrs` bindings) replaces `yjs` server-side. Wire compatible with the browser's Yjs, so §7 is unaffected. Requires a version-compatibility test (§0.1). |
| B3 | Client IDs | §7.1 clarified. The tie-breaking `clientID` is set on the **browser's** `Y.Doc`, not the server's. This was implicit in v2 and must be explicit now that the two sides are different languages. |
| B4 | Concurrency | New §20.1. The server must run as **exactly one Uvicorn worker**. Multiple workers means multiple processes and invisible rooms. This is the single most dangerous footgun of the Python stack. |
| B5 | Event loop | New §28.1. Blocking calls (file I/O, `rmtree`, `statvfs`) must not run on the event loop. Python has no equivalent of Node's libuv thread pool by default. |
| B6 | Reservations | §17 amended. The check-then-reserve sequence spans an `await` and therefore needs an `asyncio.Lock`. Under Node this was safe by accident; under asyncio it is not. |
| B7 | Uploads | §15 amended. Use raw `request.stream()`, never `UploadFile`, which spools the whole body to a temp file and would double disk usage. |
| B8 | Validation | §5 amended. Pydantic v2 discriminated unions replace hand-rolled schema validation. This is an improvement over v2. |
| B9 | Deployment | §30 amended for Uvicorn under systemd. Nginx and certbot configuration is unchanged. |
| B10 | Tests | §37.1 amended: `pytest`, `pytest-asyncio`, `httpx`, and Playwright for Python. |

### v2: contradictions and gaps in v1

This version resolves the contradictions and gaps in v1. Changes are listed here so they can be reviewed independently of the full text.

| # | Area | Change |
|---|---|---|
| A1 | Stack | Technology stack is now pinned (§0). v1 left it open while demanding verifiable deployment instructions. |
| A2 | Conflict resolution | §7 rewritten. "Later edit wins" does not apply to collaborative text and has been replaced with the CRDT's deterministic ordering. Last-write-wins is retained only for scalar metadata. |
| A3 | Server authority | §36 amended. The server holds the canonical document state in RAM but does not arbitrate text conflicts. |
| A4 | HTTPS | §29 rewritten. AWS Certificate Manager cannot be installed on a bare EC2 instance. Let's Encrypt via certbot is now specified. |
| A5 | Security model | §21 rewritten as an explicit threat model. Room access is an open capability; anyone with the code has full access. |
| A6 | Session identity | New §4.1. A resume token is required so that refresh restores identity without trusting client-supplied UUIDs. |
| A7 | Lifecycle | New §2.1. Explicit state machine with named states and configurable timers, replacing the conflict between "immediately destroyed" and "grace period". |
| A8 | Storage paths | §16 amended. Room directories are keyed by a room *instance* UUID, not the reusable 4-digit code. |
| A9 | Disk space | §17 amended. A reservation ledger is required; a bare pre-flight check is racy under concurrent uploads. |
| A10 | Limits | New §21.1. Configurable caps with generous defaults, to prevent trivial disk-fill denial of service. |
| A11 | Uploads | §15 rewritten as a concrete resumable protocol with a stale-upload reaper and a boot-time sweep. |
| A12 | Undo | §10 amended. Undo is scoped to the local user's own operations. |
| A13 | Concurrency | New §8.1. Semantics defined for delete-while-editing, concurrent rename, and delete-while-downloading. |
| A14 | Sanitization | §21 extended to display names and document names, not just filenames. |
| A15 | Testability | New §37.1. Clock, disk, and file storage must be injectable, or the required tests cannot be written honestly. |
| A16 | Delivery | New §42. Milestones with acceptance gates, replacing a single-pass build. |

---

## 0. Technology Stack (Fixed)

The stack is fixed so that deployment, testing, and collaboration behaviour are all verifiable. Do not substitute components.

**Backend (Python)**
* Python 3.12
* FastAPI for HTTP and WebSocket routing
* Uvicorn as the ASGI server, **single worker** (see §20.1)
* `pycrdt` for server-side CRDT state
* `pycrdt-websocket` for the Yjs sync and awareness protocol primitives
* Pydantic v2 for message and request validation
* `aiofiles` for non-blocking file I/O
* `pytest`, `pytest-asyncio`, `httpx` for tests

**Frontend**
* React 18, TypeScript, Vite
* CodeMirror 6 as the editor
* `yjs` plus `y-protocols` in the browser
* `y-codemirror.next` for the Yjs binding
* `@codemirror/lang-*` packages for syntax highlighting

Node.js is still required as a **build-time** dependency for Vite. It is not required at runtime on the server, and the production instance serves only pre-built static assets.

**End-to-end testing**
* Playwright for Python, driving two or more browser contexts against a real server

**Deployment**
* Single EC2 instance, Ubuntu 24.04 LTS
* Nginx as reverse proxy, TLS terminator, and static asset server
* certbot with Let's Encrypt
* systemd running Uvicorn

Rationale for a CRDT over Operational Transformation: it converges without a transforming server, survives out-of-order and duplicated messages by construction, and has a maintained CodeMirror 6 binding. Writing a correct OT server is substantially more work for no benefit at this scale, and in Python specifically there is no mature OT server library to lean on.

### 0.1 Cross-Language CRDT Compatibility (new, and load-bearing)

The browser runs `yjs` (JavaScript). The server runs `pycrdt`, which wraps `yrs`, the Rust port of Yjs. <cite index="2-1">The y-crdt project aims to maintain behaviour and binary protocol compatibility with Yjs, so that projects using Yjs and Yrs can interoperate</cite>. <cite index="7-1">pycrdt is the maintained Python binding to y-crdt; the older `ypy` binding is deprecated in its favour</cite> and must not be used.

This compatibility is an assumption the entire collaboration design rests on, so it must be **verified rather than trusted**:

1. Pin exact versions of `pycrdt`, `pycrdt-websocket`, `yjs`, `y-protocols`, and `y-codemirror.next`. No caret or tilde ranges on any of the five.
2. Write a compatibility test as the very first task of milestone M2: construct a document in the browser, sync it to the Python server, mutate it on both sides concurrently, and assert byte-identical converged state. If this test cannot be made to pass, stop and reconsider the stack before writing anything else.
3. Record the verified version quintet in the README. Treat upgrading any one of them as a change requiring the compatibility test to be re-run.

`pycrdt-websocket` ships the sync and awareness protocol encoding, plus a `YRoom` abstraction. **Use its protocol primitives, not its room or server classes.** Its room lifecycle has different semantics from §2.1 (it is built for persistent rooms), and bending it to fit the ephemeral model will cost more than implementing §2.1 directly against the protocol helpers. Note that recent releases install as the `pycrdt.websocket` submodule rather than a separate top-level package; confirm the import path against the pinned version.

---

## 1. Core Product Concept

Unchanged from v1.

No registration, login, or accounts. A user visits the site, creates a room, receives a short URL, and shares it. Everyone opening that URL joins the same live room.

* Each connected user has a server-generated UUID as their authoritative identity.
* Users may optionally set a cosmetic display name.
* All users are equal. No owner, admin, or moderator.
* No room password. No chat.

Room codes are 4-digit numeric:

```
https://example.com/room/4827
```

The room code is never used as a user identity.

**Code allocation.** On "Create Room" the server picks a uniformly random unused 4-digit code. If the number of active rooms has reached `MAX_ROOMS`, room creation returns a clear error rather than looping. Codes are reused after a room is destroyed.

---

## 2. Room Lifecycle

A room remains active while at least one user is connected. When the last participant leaves, the room and all its contents are destroyed and are not recoverable.

Visiting a destroyed room's URL produces a new empty room using that code.

There is no maximum room lifetime. A room left open overnight stays alive.

### 2.1 Lifecycle State Machine (new)

v1 said both "immediately destroyed" and "use a grace period". These are reconciled by making the states explicit. Every transition below is mandatory.

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

**States**

* `ACTIVE` — at least one live WebSocket connection. Normal operation.
* `EMPTY_GRACE` — zero live connections, state fully retained, timer running. A join or reconnect during this window returns the user to the *same* room with all documents and files intact. This is what makes browser refresh safe.
* `CLOSING` — irreversible. New joins are refused. Any WebSocket still attached is closed with a defined close code. Cleanup (§33) runs.
* `CLOSED` — cleanup verified, room removed from the manager, code released for reuse.

**Per-user connection states**

* `CONNECTED` — live socket, responding to heartbeats.
* `DISCONNECTED` — socket gone, user retained in the presence list (rendered dimmed, labelled "reconnecting") for `USER_RECONNECT_GRACE_MS`. Their awareness state (cursor, selection) is cleared immediately; their identity is not.
* Removed — grace elapsed with no reconnect. A "left the room" event is broadcast.

A room enters `EMPTY_GRACE` when it has zero `CONNECTED` users, regardless of how many are in `DISCONNECTED` grace.

**Timers (environment variables)**

| Variable | Default | Purpose |
|---|---|---|
| `WS_HEARTBEAT_INTERVAL_MS` | `20000` | Server sends WebSocket ping. |
| `WS_HEARTBEAT_TIMEOUT_MS` | `45000` | No pong within this window terminates the socket. |
| `USER_RECONNECT_GRACE_MS` | `30000` | Disconnected user retained in presence. |
| `ROOM_EMPTY_GRACE_MS` | `60000` | Empty room retained before `CLOSING`. |

`WS_HEARTBEAT_INTERVAL_MS` must remain comfortably below the Nginx `proxy_read_timeout` configured in §29. Do not raise one without the other.

---

## 3. Room State Preservation

Refreshing the browser must not destroy the room or its contents. The `EMPTY_GRACE` state in §2.1 guarantees this even when the refreshing user is the only participant.

The application distinguishes four cases, and the mechanism for each is now named:

| Case | Detection | Result |
|---|---|---|
| Temporary network loss | heartbeat timeout, no explicit leave | user enters `DISCONNECTED`, room retained |
| Browser refresh | socket closes, client reconnects with a valid resume token (§4.1) | same UUID rebound, room state restored |
| Explicit departure | client sends `leave` message, or clicks Leave Room | user removed immediately, no grace |
| Final participant leaving | room reaches zero connected users | `EMPTY_GRACE`, then `CLOSING` |

---

## 4. Users

No authentication. On join the server generates:

```
userId = crypto.randomUUID()
```

The UUID is the authoritative internal identity and is never accepted from the client.

A friendly display name is generated by default (`Blue Fox`, `Quiet Panda`, `Red Wolf`). The user may change it. The display name is cosmetic only, is not unique, and carries no authority.

Users are completely equal. Any participant may perform any room operation.

### 4.1 Session Resume Token (new)

v1 required refresh to restore identity while also forbidding trust in client-supplied identity. Those reconcile only with a server-issued token.

1. On first join the server generates `sessionToken`, 128 bits of CSPRNG output, hex-encoded, and returns it in the join acknowledgement alongside the assigned `userId`.
2. The client stores it in `sessionStorage` (not `localStorage`, so it dies with the tab, and separate tabs are correctly treated as separate users).
3. On reconnect the client presents `{ roomCode, sessionToken }`.
4. The server rebinds the existing `userId`, display name, and join sequence number only if the token matches a user in that room instance and that user is still within `USER_RECONNECT_GRACE_MS`.
5. Otherwise the server issues a fresh `userId` and token. It never errors out; the user simply joins as new.

The token is scoped to one room instance and is discarded when the room reaches `CLOSED`. It is a continuity mechanism, not a security boundary (see §21).

---

## 5. Real-Time Communication

A single WebSocket per client carries all real-time traffic, at `wss://<domain>/ws`.

The following update live with no manual refresh:

* users joining and leaving
* display name changes
* document create, rename, delete
* text edits (Yjs updates)
* cursor positions and selections (Yjs awareness)
* file upload started, progress, completed, deleted
* room lifecycle notices
* connection status

**Message envelope.** Every client message is JSON with a `type` field, except Yjs sync and awareness frames, which are binary and prefixed with a single-byte channel tag.

Validation uses a **Pydantic v2 discriminated union** on `type`, so every inbound control message is parsed into a typed model before it reaches any handler. Unknown type, missing field, wrong type, or a control payload over `MAX_WS_MESSAGE_BYTES` (which bounds control messages only, never document text) results in the message being dropped and a structured error event returned. A `ValidationError` must never propagate to the connection handler and must never terminate the socket unless malformed input is repeated abusively.

Binary frames are dispatched by their channel tag before any JSON parsing is attempted, and are passed to the CRDT layer as raw bytes.

**Upload progress clarification.** The uploader's own progress bar is driven locally by the HTTP request's progress events and is not round-tripped through the server. A coarse broadcast (`fileId`, `uploaderName`, `percent`), throttled to at most one message per second per upload, informs other participants that an upload is in flight.

---

## 6. Collaborative Text/Code Editor

Multiple users edit the same document simultaneously, Google Docs style.

**Implementation is the Yjs CRDT, in two languages.** Each document is a `Y.Doc` containing a single `Y.Text` in the browser, and the corresponding `pycrdt.Doc` with a `Text` on the server. The two speak the same binary update format (§0.1). The server holds the authoritative document in memory, applies every incoming update to it, and relays updates to all other clients in the room. The server's copy is what late joiners and reconnecting clients sync against, via a state-vector diff rather than a full replay.

Do not implement collaboration by broadcasting whole-document replacements. Do not diff-and-patch full text.

Required behaviour, all of which Yjs provides and all of which must be covered by tests:

* simultaneous insertion at the same position
* simultaneous deletion of overlapping ranges
* insertion into a range another user is concurrently deleting
* out-of-order, duplicated, and delayed update delivery
* a client reconnecting after missing arbitrary numbers of updates
* three or more concurrent editors

Remote cursors, selections, and display names are shown via the Yjs awareness protocol, rendered by `y-codemirror.next`. Each user is assigned a stable colour derived from their `userId`.

---

## 7. Conflict Resolution (rewritten)

**v1's rule cannot be implemented and has been replaced.** "The later edit wins" is register semantics. Applied to collaborative text it means discarding one user's keystrokes, which is exactly the behaviour a CRDT exists to prevent. The correct guarantee is convergence, not victory.

The requirement is now split by data kind.

### 7.1 Document text: CRDT ordering, no edit is discarded

Concurrent insertions are **both preserved**. Every replica converges on the same total order, and no user's typing is lost.

Ordering rules, in the order applied:

1. **Causal position.** Each inserted character records its left and right neighbours at insertion time. Insertions at unambiguously different positions are ordered by position; there is no conflict to resolve.
2. **Tie-break by join order.** When two insertions have identical neighbours (genuinely concurrent, at the same logical point), the tie is broken by comparing the two clients' CRDT client identifiers. **The server assigns each user a `clientID` equal to a per-room monotonically increasing join sequence number**, starting at 1, and returns it in the join acknowledgement. Because the CRDT breaks ties by comparing client IDs, join order therefore determines the outcome, which preserves v1's stated intent.

   **Where the ID is applied (clarified for the split-language stack).** The tie-break compares the IDs of the clients that *authored* the insertions. Insertions are authored in the browser, so the assigned number must be set on the **browser's** `Y.Doc` before the editor binding is attached and before the first sync frame is sent. The server's own `pycrdt` document does not author edits and its client ID is irrelevant to ordering. Getting this backwards, by setting the ID on the server document, produces a system that appears to work and silently breaks the tie-break rule, so it needs an explicit test.

   Consequently the join acknowledgement must arrive **before** the WebSocket begins carrying sync traffic. Sequence: open socket → send join → receive `{userId, clientId, sessionToken}` → construct `Y.Doc` with the assigned client ID → begin sync.
3. **Deletion is idempotent and commutative.** A character deleted concurrently by two users is deleted once. A character inserted concurrently with the deletion of its surrounding range survives; it is not swept up by a deletion that did not causally know about it.

**Implementation obligation.** The direction of the tie-break (whether the earlier joiner's text lands to the left or the right of the later joiner's) is determined by the CRDT implementation in use. The implementer must write a test that asserts the actual observed ordering, pin the versions of both `yjs` and `pycrdt`, confirm the two agree on that ordering, and document the observed rule in the README with a worked two-user example. Do not assert a direction from memory, and do not assume the two implementations agree without checking; that agreement is exactly what §0.1 exists to verify.

The join sequence counter belongs to the room instance and is never reset while the instance lives. A user reconnecting with a valid resume token keeps their original `clientID`, which is why §4.1 is a prerequisite for this section rather than an optional convenience.

### 7.2 Scalar metadata: last-write-wins by server sequence

Document names, display names, and similar single-value fields are not CRDT text and *do* use last-write-wins. Ordering does not come from client clocks.

* The server maintains a single monotonically increasing `opSeq` per room.
* Every metadata mutation is stamped with the next `opSeq` on arrival.
* Higher `opSeq` wins.
* `opSeq` is assigned in the order the server processes messages, so it is total and deterministic. Ties are impossible by construction, which makes the join-order tie-break unnecessary here.

Client clocks are not consulted for any ordering decision anywhere in the system. Client-supplied timestamps are accepted only as display metadata (§13) and are overwritten by the server's own clock.

---

## 8. Documents

Users can create, rename, delete, switch between, edit, and download multiple documents within a room.

The **document list** is server-authoritative and is not itself a CRDT. Create, rename, and delete are WebSocket RPCs; the server validates, applies, stamps with `opSeq`, and broadcasts. Only the *contents* of each document are CRDT-managed.

Documents exist only while the room instance exists.

### 8.1 Concurrent Document Operations (new)

v1 listed "concurrent file operations" under error handling without defining semantics. These are now defined.

| Situation | Defined behaviour |
|---|---|
| User A edits a document while user B deletes it | Delete wins. The server broadcasts `document_deleted`. Clients viewing it show a non-blocking notice ("This document was deleted by Quiet Panda"), close the editor, and switch to another document or an empty state. In-flight edits for that document are discarded on arrival. |
| Two users rename the same document concurrently | Last write wins by `opSeq` (§7.2). Both clients converge on the higher-sequence name. |
| Two users create documents with the same name | Both are created. Names are not unique; `documentId` is. The UI disambiguates by showing creation order. |
| A user deletes the last remaining document | Allowed. The room shows an empty state with a "New Document" action. |
| A file is deleted while another user is downloading it | The download completes. The server unlinks the path but the open file descriptor keeps the data alive until the stream ends. This is intentional; document it. |
| A file is deleted while it is still uploading | The upload is aborted, its reservation released, and its partial data removed (§15). |

---

## 9. Syntax Highlighting

CodeMirror 6 with language support for at minimum: Python, JavaScript, TypeScript, C, C++, Java, HTML, CSS, JSON, YAML, Markdown, Shell/Bash, SQL, plus a plain-text fallback.

Language is selected from the document name's extension, with a manual override in the UI. Language modes are lazy-loaded so the initial bundle stays small.

---

## 10. Editor Features

Standard editing: undo, redo, copy, paste, select all, keyboard navigation, search, line numbers, indentation, multi-document.

**Undo scope (amended).** Undo must use `Y.UndoManager` configured to track only operations originating from the local user. A user pressing Ctrl+Z must never revert another participant's typing. This is a correctness requirement, not a nicety, and requires an explicit test: two users type, one undoes, the other's text is asserted intact.

---

## 11. File Upload System

Arbitrary file types. No extension allow-list or block-list.

There is no per-file or per-room quota by default; the effective limit is available disk space, subject to the safety caps in §21.1 which are set high enough to be inert in normal use.

On insufficient space the server must: reject the upload, delete all partial data for that upload, release the reservation, leave existing room files untouched, and return a clear user-facing message ("Not enough space on the server for this file").

A failed upload must never leave orphaned temporary data. This is enforced by three independent mechanisms (§15): explicit abort handling, a periodic stale-upload reaper, and a boot-time sweep.

---

## 12. File Upload UX

While uploading:

```
Uploading...
project.zip
██████████████████░░░░ 72%   [Cancel]
```

For each completed file: filename, size, uploader display name, upload timestamp, download button, delete button.

Multiple users can upload simultaneously. Duplicate filenames are permitted and never overwrite one another; storage is keyed by `fileId`.

---

## 13. File Ownership

No privileged users. Any participant can delete any file.

Retained metadata: `uploaderId` (UUID), `uploaderName` (the display name **as it was at upload time**, snapshotted, not a live reference), and `uploadedAt` (from the server clock, never the client's).

---

## 14. File Download

```
GET /api/rooms/:roomCode/files/:fileId
```

The server resolves `fileId` within the room instance currently bound to `:roomCode`. If the file does not belong to that instance, respond 404 (never 403, which would confirm existence elsewhere).

The stored path is `/<dataRoot>/rooms/<roomInstanceId>/files/<fileId>` where `fileId` is a UUID. The user-supplied filename is **never** used to construct a path.

`Content-Disposition` uses RFC 5987 encoding so that spaces, Unicode, and special characters are handled correctly:

```
Content-Disposition: attachment; filename="fallback.bin"; filename*=UTF-8''<percent-encoded-original>
```

---

## 15. Resumable Uploads (rewritten)

Uploads go over HTTP, not the WebSocket. This keeps large binary transfers off the collaboration channel and gives native progress events and backpressure.

**Protocol**

| Step | Endpoint | Behaviour |
|---|---|---|
| Init | `POST /api/rooms/:code/uploads` with `{filename, size}` | Validates, checks and **reserves** space (§17), creates `uploadId`, returns it. |
| Send | `PUT /api/uploads/:uploadId` with `Content-Range` | Appends to a single `.part` file. Rejects a range that does not start exactly at the current committed offset. |
| Query | `GET /api/uploads/:uploadId` | Returns `{ committedOffset }` so a client can resume. |
| Complete | `POST /api/uploads/:uploadId/complete` | Verifies the committed offset equals the declared size, atomically renames the `.part` file into `files/<fileId>`, releases the reservation, broadcasts `file_added`. |
| Abort | `DELETE /api/uploads/:uploadId` | Deletes the `.part` file, releases the reservation. |

The single-part-file approach with a byte offset is deliberately simpler than N discrete chunk files: there is no reassembly step and therefore no window in which a half-assembled file can be observed. Resume requires ordered transmission, which is acceptable here.

Client chunk size defaults to 8 MiB (`UPLOAD_CHUNK_BYTES`). On network failure the client re-queries the offset and resumes from there, with exponential backoff and a bounded retry count before surfacing a failure.

**FastAPI implementation constraints (new).**

* **Do not use `UploadFile` or `File(...)`.** FastAPI spools multipart bodies into a `SpooledTemporaryFile`, which writes the entire chunk to disk a second time before your handler sees it. That doubles transient disk usage and puts bytes on disk outside the reservation ledger's accounting, defeating §17.
* Read the body with `async for chunk in request.stream()` and append incrementally. The chunked protocol above uses a raw `PUT` body with a `Content-Range` header specifically so that no multipart parsing is involved.
* Write with `aiofiles`, or a bounded thread executor. A synchronous `f.write()` of an 8 MiB chunk blocks the event loop and stalls every WebSocket in every room for the duration (§28.1).
* Call `os.fsync` only at `complete`, not per chunk. Per-chunk fsync will make large uploads unusably slow.
* Validate that the `Content-Range` start offset equals the stored `committedOffset` exactly. Reject anything else with 409 and the current offset in the body, so the client can resynchronize rather than guess.
* Handle client disconnect mid-stream: `request.stream()` raises, and the handler must leave `committedOffset` at the last fully written position rather than a partial one. Update the offset only after the write for that range has completed.

**Three-layer orphan prevention**

1. **Explicit** — abort, error, and room-cleanup paths all delete `.part` files and release reservations.
2. **Reaper** — a periodic sweep deletes any upload with no activity for `UPLOAD_STALE_MS` (default 600000) and releases its reservation.
3. **Boot sweep** — on startup the server deletes the entire contents of `<dataRoot>/rooms`. Since no room survives a restart (§19), everything there is by definition garbage. This closes the crash-leaves-orphans gap that v1's §15 and §19 opened between them.

---

## 16. Storage Architecture

Local filesystem only. No object storage.

```
<DATA_ROOT>/
  rooms/
    <roomInstanceId>/          # UUID, NOT the 4-digit code
      files/
        <fileId>               # UUID, no extension
      tmp/
        <uploadId>.part
```

**Why the instance UUID (amended).** v1 used `/data/rooms/4827`. Room codes are reused, and §34 explicitly permits a new room with the same code to be created while the old one is still being cleaned up. Under v1's layout the new room would write files into a directory that another task is recursively deleting. Keying by instance UUID makes the two directories disjoint, so the collision is structurally impossible rather than merely unlikely.

The room manager holds `Map<roomCode, RoomInstance>`. Rooms in `CLOSING` are removed from that map immediately, so they are unreachable by code even before their files are gone.

All collaboration state lives in memory. No database of any kind.

---

## 17. Storage Availability (amended)

The UI displays approximate available server storage in the room information panel.

**The backend check must account for in-flight uploads.** A naive pre-flight check passes two concurrent 40 GB uploads against 50 GB of free space and then fails partway through both, which is precisely the "leave existing room files untouched" failure v1 forbids.

Required mechanism, a **reservation ledger**:

```
availableForNewUpload = statvfs(DATA_ROOT).bavail
                      - sum(activeReservations)
                      - DISK_HEADROOM_BYTES
```

* On upload init, if the declared size exceeds `availableForNewUpload`, reject before writing a single byte.
* Otherwise record a reservation of the declared size.
* Release the reservation on complete, abort, reap, or room cleanup.
* **The check and the reservation must be atomic under a single process-wide `asyncio.Lock`.** `statvfs` runs in a thread executor (§28.1), which means the sequence contains an `await`, which means the event loop can interleave another upload's check between your check and your reservation. Both then pass, and both then fail partway through. This is the exact race the ledger exists to prevent, so do not treat "Python is single-threaded" as making it safe. Hold the lock across read-free-space, subtract-reservations, compare, and record. Release it before any actual byte is written; the lock guards the ledger, not the transfer.
* Re-check actual free space every `DISK_RECHECK_INTERVAL_BYTES` (default 64 MiB) written, to catch space consumed outside the application. If free space falls below headroom mid-upload, abort that upload cleanly.

`DISK_HEADROOM_BYTES` defaults to 2 GiB so the instance never fills to the point where the OS and logs are affected.

The displayed figure is `availableForNewUpload`, refreshed on a timer and pushed over the WebSocket. The browser figure is advisory only; the server enforces independently and never trusts a client-declared size beyond using it as the reservation amount, which is itself validated against bytes actually received.

---

## 18. No Persistence

No PostgreSQL, MySQL, MongoDB, DynamoDB, S3, Redis, or any persistent store. Permitted: RAM, temporary filesystem, WebSocket connection state.

Room data must not intentionally survive room destruction. Note the interaction with §19: because a restart destroys everything, the boot sweep in §15 is what makes this true in practice and not merely in intent.

---

## 19. Crash Behaviour

Server restart loses all active rooms and all room files. This is accepted and explicitly documented in the README and shown in the landing page footer ("Rooms are temporary and may be lost if the server restarts").

Simplicity and ephemerality take priority over durability.

---

## 20. Room Capacity

Single EC2 instance. No horizontal scaling, no multi-region, no distributed state. The architecture must stay simple enough to be read end to end in an afternoon.

Note the structural consequence: because rooms live in one process's memory, this application cannot be run as more than one instance. Do not write code that implies otherwise.

### 20.1 Single Worker (new, mandatory)

**Uvicorn must run with exactly one worker process.** The `RoomManager`, all CRDT documents, the presence table, the session-token table, and the disk reservation ledger are plain Python objects in process memory. A second worker gets a second, empty copy of all of them.

The failure mode is silent and confusing rather than loud: two users open the same room code, get load-balanced to different workers, and each sees an empty room containing only themselves. Uploads land in one worker's ledger and are invisible to the other. Cleanup in one worker deletes a directory the other still believes is live.

Therefore:

* Run `uvicorn app.main:app --host 127.0.0.1 --port 8000` with **no** `--workers` flag, or `--workers 1` explicitly.
* Pass `--ws-max-size` (see §21.3). One paste into the editor is one Yjs update is one WebSocket frame, so Uvicorn's 16 MiB default is a ceiling on how much text a person may paste — imposed a layer below anything the application can express, and silent: the socket is closed with 1009 before any handler runs. Every launch path must carry it — the container entrypoint, the systemd unit, and the local development command alike.
* Do **not** deploy under `gunicorn -k uvicorn.workers.UvicornWorker -w N`. This is the most commonly recommended FastAPI production pattern and it is wrong for this application.
* Do not add `--reload` in production; it spawns a reloader process.
* Add a startup assertion that logs a prominent warning if more than one worker is somehow detected, and state the constraint in a comment at the top of the ASGI entry point and in the systemd unit file.

Capacity is bounded by one Python process on one instance. Given `MAX_ROOMS` of 200 and `MAX_USERS_PER_ROOM` of 32, that is comfortably within reach for a personal project, since the workload is almost entirely I/O-bound and the CRDT merge work happens in Rust rather than in Python bytecode.

---

## 21. Security Model (rewritten)

v1 asked for room isolation while forbidding any authentication. Those cannot both hold, so the model is now stated explicitly.

### 21.1 Threat model

**Accepted, by design:**
* Room codes are 4 digits and enumerable. Anyone can scan the space and find active rooms.
* Anyone with a room code has full access: read all documents, edit them, download every file, delete every file, delete every document.
* There is no confidentiality guarantee between rooms beyond the obscurity of the code.
* No malware scanning. Uploaded files are served as-is.

**This must be stated in the UI**, once, in the room panel: "Anyone with this room code can view, edit, and delete everything here."

### 21.2 Still required

These are hygiene requirements, not access control, and are mandatory regardless of the open model:

* Validate every WebSocket message against a schema; reject oversized frames.
* Validate all upload metadata (filename length, declared size, content-range arithmetic).
* Never construct a filesystem path from user input. Paths are built only from server-generated UUIDs.
* Reject any `fileId` or `documentId` that is not a well-formed UUID before it reaches the filesystem layer.
* Scope file lookups to the room instance bound to the requested code, so a stale or mistyped `fileId` returns 404 rather than another room's data. This prevents accidental leakage across rooms; it is not a defence against a determined party, who can simply join the other room.
* Sanitize **filenames, display names, and document names** alike: Unicode NFC normalization, strip C0/C1 control characters and directional-override characters, trim, enforce length caps. Render as text; never as HTML.
* Serve downloads with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff` so an uploaded HTML or SVG file cannot execute on the site's origin.
* No IP logging, no analytics, no personal information persisted.

### 21.3 Resource limits (new)

**No fixed limit governs what a room may hold.** A number chosen in advance is either smaller than the server can carry - in which case it refuses work the machine had room for - or larger, in which case it protects nothing. So the size and count of a room's contents are bounded by what the server still has, and by nothing else:

* **Files** by free disk minus outstanding reservations minus `DISK_HEADROOM_BYTES`, enforced atomically at reservation time (§17).
* **Document text** by free memory minus `MEMORY_HEADROOM_BYTES`. Documents never reach the filesystem - every CRDT document lives in the backend process - so memory is what actually bounds them, and it gets the same treatment the disk already had.

Each cap below is still available for an operator who needs a ceiling smaller than the machine, and `0` - the default on every one that describes room contents - means "no application limit".

| Variable | Default | Purpose |
|---|---|---|
| `MAX_ROOMS` | `200` | Concurrent room instances. Bounded by the 4-digit code space, not by capacity. |
| `MAX_USERS_PER_ROOM` | `32` | Connections per room. |
| `MAX_DOCS_PER_ROOM` | `0` | Documents per room; `0` means unlimited. |
| `MAX_DOC_BYTES` | `0` | Per-document text size; `0` means unlimited. |
| `MAX_FILES_PER_ROOM` | `0` | Files per room; `0` means unlimited. |
| `MAX_FILE_BYTES` | `0` | Per-file cap; `0` means unlimited. |
| `MAX_ROOM_TOTAL_BYTES` | `0` | Per-room total; `0` means unlimited. |
| `DISK_HEADROOM_BYTES` | `2147483648` | Disk the application will not spend, so cleanup itself always can. |
| `MEMORY_HEADROOM_BYTES` | `536870912` | The same, for the memory documents live in. |
| `MEMORY_POLL_INTERVAL_MS` | `2000` | How long a memory reading is reused; it is consulted per applied update. |
| `MAX_WS_MESSAGE_BYTES` | `1048576` | **Control frames only.** Document text travels as binary CRDT frames and is not bounded by this. |
| `WS_MAX_FRAME_BYTES` | `268435456` | Transport ceiling, passed to Uvicorn as `--ws-max-size`. One paste is one frame. |
| `MAX_DISPLAY_NAME_CHARS` | `32` | |
| `MAX_DOC_NAME_CHARS` | `128` | |
| `MAX_FILENAME_CHARS` | `255` | |
| `MAX_UPLOADS_PER_USER` | `5` | Concurrent transfers per connection. Not a limit on how many files may be uploaded: the client queues the rest and every chosen file still arrives. |

Two consequences worth stating outright, because they are easy to get wrong:

* **The memory guard never refuses an edit.** An update that has arrived is always applied. Rejecting it would leave the sending browser holding text the server does not have, and a silently diverged CRDT is a far worse outcome than a room that is merely large. The room is told instead - once, then at most every 30 seconds - so a participant can delete a document or a file. A platform that exposes no memory reading is treated as "not under pressure", never as "full".
* **`WS_MAX_FRAME_BYTES` is a real ceiling and it is not the application's.** One paste into the editor is one Yjs update is one WebSocket frame, and Uvicorn's 16 MiB default closes the socket with 1009 before the application sees the frame at all. It is raised in `backend/docker-entrypoint.sh`; a deployment that starts Uvicorn some other way must pass `--ws-max-size` itself or it silently caps how much text a person can paste.

Exceeding a limit produces a clear user-facing message, never a stack trace.

---

## 22. Network and Connection Handling

The UI shows connection state: `● Connected`, `● Reconnecting...`, `● Disconnected`.

On connection loss the client preserves local editor state (Yjs holds unsent updates and flushes them on reconnect), reconnects automatically with exponential backoff and jitter, resynchronizes via the Yjs sync protocol, and restores identity via the resume token (§4.1).

A browser refresh reconnects automatically.

**The WebSocket URL must be derived from `window.location`**, never hardcoded, so that `https:` maps to `wss:` and no mixed-content error is possible:

```js
const wsUrl = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`;
```

---

## 23. Room Presence

Show the participant list with live join and leave updates:

```
3 people in this room
● Blue Fox
● Quiet Panda
● Red Wolf (reconnecting)
```

Join and leave are subtle toasts. A user in `DISCONNECTED` grace is shown dimmed rather than removed, so a brief network blip does not produce a spurious "left the room / joined the room" pair.

---

## 24. Main UI

Minimal, dark-mode first, clean, desktop-oriented, calm. Not a SaaS dashboard. Spiritually close to a plain collaborative workspace.

```
┌───────────────────────────────────────────────────────────────┐
│  Room 4827                     [Copy Link]   [Leave Room]     │
├───────────────────────────────────┬───────────────────────────┤
│ Documents                         │ Files                     │
│ 📄 main.py                        │ 📦 project.zip            │
│ 📄 config.json                    │    42 MB · Blue Fox       │
│ [+ New Document]                  │    [Download] [Delete]    │
│                                   │ [+ Upload Files]          │
├───────────────────────────────────┴───────────────────────────┤
│                        CODE EDITOR                            │
├───────────────────────────────────────────────────────────────┤
│ ● Connected   3 people   Available storage: 71.4 GB           │
└───────────────────────────────────────────────────────────────┘
```

The open-access notice from §21.1 sits in the room information panel.

---

## 25. Landing Page

```
Temporary Workspace
Collaborate. Share files. Nothing is saved.

[ Create Room ]
```

Creating a room generates the code, creates the in-memory instance, connects the creator, and shows the workspace with a copyable URL.

---

## 26. Joining a Room

Opening a valid active room URL enters the room immediately. No signup, login, or password.

If no room exists for that code, a new empty room is created with that code.

**UI consequence to handle explicitly:** because unknown codes silently create rooms, a user who mistypes a code cannot tell that from an expired room. When a room is created by URL visit rather than by the Create Room button, show a one-time notice: "This room was empty, so a new one was created." This makes the mistype visible without adding an error state.

---

## 27. Leave Behaviour

"Leave Room" disconnects the user immediately with no reconnect grace. If others remain, the room continues. If they were the last, the room enters `EMPTY_GRACE` and then `CLOSING`.

Closing the tab must also result in leaving. Detection is by heartbeat timeout (§2.1), not by `beforeunload`. A `navigator.sendBeacon` hint on unload may be sent as an optimization to shorten the delay, but correctness must not depend on it.

---

## 28. Backend Architecture

```
Browser  (yjs, CodeMirror 6)
   │ HTTPS / WSS
   ▼
Nginx  (TLS termination, static assets, reverse proxy)
   │
   ▼
Uvicorn, single worker, systemd  (Python 3.12)
   └── FastAPI app
        ├── HTTP routes: upload API, download, storage info
        ├── WebSocket route: /ws
        ├── RoomManager: dict[room_code, RoomInstance], asyncio timers
        ├── pycrdt Docs in memory, one per document
        ├── UploadManager: reservation ledger, .part files, reaper
        └── Filesystem: <DATA_ROOT>/rooms/<instance_id>/
```

Suggested module layout:

```
backend/app/
  main.py            ASGI entry point, lifespan hooks (boot sweep, shutdown)
  config.py          pydantic-settings, fail fast on bad config
  rooms/
    manager.py       RoomManager, code allocation
    instance.py      RoomInstance, lifecycle state machine, timers
  ws/
    routes.py        WebSocket endpoint
    messages.py      Pydantic discriminated-union message models
    heartbeat.py     ping/pong, timeout detection
  collab/
    registry.py      per-document pycrdt Doc registry
    sync.py          sync + awareness protocol relay
  files/
    uploads.py       chunked upload endpoints, offsets
    reservations.py  disk reservation ledger + asyncio.Lock
    download.py      streaming download, RFC 5987 headers
  storage/
    protocols.py     FileStore, DiskSpaceProvider, Clock (typing.Protocol)
    fs.py            real filesystem implementations
  cleanup/
    room.py          room cleanup routine
    reaper.py        stale-upload sweep
    boot.py          data-root sweep on startup
  util/
    ids.py  names.py  sanitize.py
frontend/src/
  routes/            landing, room
  editor/            CodeMirror + Yjs binding, clientID assignment
  files/             upload queue, file list
  presence/          user list, toasts
  ws/                client, reconnect, resume token
  ui/                shared components
```

No module over roughly 300 lines. Full type annotations, checked with `mypy --strict` or `pyright`.

### 28.1 Event Loop Discipline (new)

The whole server is one thread running one event loop. Any blocking call stalls every room simultaneously. Node's runtime hides much of this behind libuv; Python does not, so it has to be handled deliberately.

Route through `asyncio.to_thread` or a bounded `ThreadPoolExecutor`:

* All file reads and writes, or use `aiofiles`.
* `shutil.rmtree` during room cleanup. Deleting a directory holding tens of gigabytes can take seconds, and doing it inline would freeze every other room.
* `os.statvfs` for the disk check.
* `os.fsync` at upload completion.

Safe to call inline: `pycrdt` operations (they execute in Rust and are fast), dict and set manipulation, UUID generation.

Use `asyncio.create_task` for grace-period timers, keep a reference to every task so it is not garbage collected mid-flight, and cancel the task explicitly on any transition that invalidates it (a user reconnecting cancels their removal timer; a join during `EMPTY_GRACE` cancels the room's closing timer). Every `CancelledError` must be caught and handled rather than allowed to surface as an unhandled task exception.

Use the FastAPI `lifespan` context manager for the boot sweep (§15) on startup and for orderly shutdown: close sockets with a defined close code, cancel timers, and do **not** attempt to preserve room state.

---

## 29. HTTPS (rewritten)

**Correction to v1.** AWS Certificate Manager public certificates cannot be installed on an EC2 instance. They terminate only at an ALB, CloudFront, or API Gateway. Using ACM would require an Application Load Balancer, which contradicts §30's instruction not to add infrastructure and would additionally require raising the ALB's default 60-second idle timeout to keep WebSockets alive.

**Therefore: Let's Encrypt via certbot, terminating at Nginx on the instance.** This is free, automatic, and correct for a single-instance deployment.

```bash
sudo apt update && sudo apt install -y nginx certbot python3-certbot-nginx
sudo certbot --nginx -d example.com -d www.example.com
sudo systemctl status certbot.timer   # renewal is automatic
```

Nginx configuration essentials:

```nginx
server {
    listen 80;
    server_name example.com www.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name example.com www.example.com;

    # certbot manages these lines
    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;

    client_max_body_size 0;          # large uploads; app enforces limits
    proxy_request_buffering off;     # stream upload chunks straight through

    location /ws {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;    # must exceed WS_HEARTBEAT_INTERVAL_MS
        proxy_send_timeout 3600s;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`proxy_request_buffering off` matters: without it Nginx buffers the entire upload body to disk before forwarding, which doubles disk usage during large uploads and breaks the reservation accounting in §17.

Renewal must be verified before the deployment is considered done: `sudo certbot renew --dry-run`.

---

## 30. AWS Deployment

Single EC2 instance. No S3, RDS, Redis, ECS, Kubernetes, or ALB.

Provide, as runnable documented steps:

* Instance provisioning (suggested: t3.small or larger, Ubuntu 24.04, root volume sized for expected uploads).
* Security group: inbound 22 (your IP only), 80 (0.0.0.0/0, for ACME and redirect), 443 (0.0.0.0/0). Nothing else. The Node process binds `127.0.0.1` only.
* Python 3.12 installation, a virtual environment at `/opt/ephemeral-rooms/.venv`, and dependency installation from a locked requirements file. `pycrdt` ships as a compiled Rust extension, so confirm a manylinux wheel exists for the instance's Python version and CPU architecture (x86_64 or arm64 if using Graviton). If no wheel matches, the install will attempt a source build and require a Rust toolchain, which is worth discovering locally rather than on the instance.
* Frontend build on a developer machine or in CI (Node is needed here only), with the resulting `dist/` copied to the instance and served directly by Nginx rather than through Uvicorn.
* `DATA_ROOT` on the root volume or a dedicated EBS volume, owned by the service user, mode 0700.
* systemd unit with `Restart=always`, `EnvironmentFile`, a dedicated non-root user, and an `ExecStart` that pins a single worker:

```ini
[Service]
User=ephemeral
WorkingDirectory=/opt/ephemeral-rooms
EnvironmentFile=/etc/ephemeral-rooms.env
# Exactly one worker. See spec section 20.1. Do not add --workers.
# --ws-max-size is load-bearing, not tuning; see §21.3.
ExecStart=/opt/ephemeral-rooms/.venv/bin/uvicorn app.main:app \
          --host 127.0.0.1 --port 8000 --workers 1 \
          --ws-max-size 268435456
Restart=always
RestartSec=2
```
* Startup, restart, and log inspection commands (`journalctl -u ephemeral-rooms -f`).
* An `.env.example` documenting every variable from §2.1, §17, §21.3, and §31.

---

## 31. Domain

Assume the domain is owned. Document:

* An `A` record for the apex pointing at the instance's Elastic IP.
* A `CNAME` or second `A` record for `www`.
* Allocation and association of an Elastic IP, and why: without it the public IP changes on stop/start and both DNS and the certificate break.
* Propagation verification (`dig`) before running certbot, since certbot will fail if DNS has not resolved yet.

`PUBLIC_ORIGIN` is an environment variable. No domain is hardcoded anywhere in the source.

---

## 32. Error Handling

Handle gracefully: room creation failure, capacity limits, WebSocket disconnect, reconnect failure, invalid room requests, malformed messages, failed uploads, insufficient disk space, interrupted uploads, deleted files, concurrent operations (§8.1), and unexpected server errors.

User-facing errors are plain language. No stack traces, no filesystem paths, no internal IDs beyond the room code and file names the user already sees. Internal detail goes to the server log with a correlation ID that can optionally be surfaced as an opaque reference.

---

## 33. Cleanup

On entering `CLOSING`:

1. Remove the room from the room manager, so the code no longer resolves to it.
2. Reject all new operations for the instance.
3. Abort all in-flight uploads for the instance and release their reservations.
4. Close any remaining WebSocket connections with a defined close code and reason.
5. Drop all references to the room's `pycrdt` documents so both the Python objects and the underlying Rust allocations are released.
6. Delete `<DATA_ROOT>/rooms/<roomInstanceId>` recursively, via `asyncio.to_thread` so a large deletion does not stall the event loop (§28.1).
7. Verify the directory no longer exists.
8. Clear presence state, timers, and the session-token table.
9. Mark `CLOSED`.

Cleanup is idempotent and safe to run repeatedly. On failure it retries with backoff up to a bounded count, logs the operational error, and never leaves room contents in place on the grounds that cleanup errored.

---

## 34. The Race Condition

```
A is the last user → A disconnects → EMPTY_GRACE elapses → CLOSING begins
                                        ↓
                          before cleanup finishes, B opens /room/4827
```

**Defined behaviour:** step 1 of §33 removes the room from the manager *before* any deletion begins. B's lookup therefore misses, and B receives a brand-new instance with a fresh `roomInstanceId` and a fresh, empty directory. The old instance finishes cleanup independently against its own directory, which the new instance never touches because the paths are keyed by instance UUID (§16).

A room's contents can never survive because someone arrived during its destruction, and a new room can never have its files deleted by an old room's cleanup.

Joins arriving during `EMPTY_GRACE` (before `CLOSING`) are a different case and *do* resurrect the room with its state intact. That is the refresh-safety mechanism, and the boundary between the two is the single point where `CLOSING` is entered.

---

## 35. Room State Model

```
RoomInstance
 ├── roomCode              4-digit, reusable
 ├── roomInstanceId        UUID, unique forever, keys the filesystem
 ├── createdAt
 ├── state                 ACTIVE | EMPTY_GRACE | CLOSING | CLOSED
 ├── opSeq                 monotonic counter for metadata LWW
 ├── joinSeq               monotonic counter, assigns Yjs clientIDs
 ├── users: Map<userId, {
 │      userId, displayName, sessionToken, clientId,
 │      joinedAt, connection, connectionState, disconnectTimer }>
 ├── documents: dict[document_id, { document_id, name, doc: pycrdt.Doc, created_at }]
 ├── files: Map<fileId, {
 │      fileId, originalName, sanitizedName, size,
 │      uploaderId, uploaderName, uploadedAt }>
 ├── uploads: Map<uploadId, {
 │      uploadId, fileId, declaredSize, committedOffset,
 │      reservationBytes, uploaderId, lastActivityAt }>
 └── timers                emptyGraceTimer
```

---

## 36. Do Not Trust the Client (amended)

The server is authoritative for: room membership, room existence and lifecycle state, user UUIDs, `clientID` assignment, display-name storage, document list and names, file IDs and ownership metadata, upload state and offsets, available storage, and all sequencing (`opSeq`).

**Amendment.** For document *text*, the server is the custodian of state, not the arbiter of conflicts. It holds the canonical `pycrdt.Doc`, applies every update to it, and serves late joiners and reconnecting clients from it, but it does not transform or reorder operations; convergence comes from the CRDT itself (§7.1). This is the correct reading of "authoritative" for a CRDT architecture, and v1's stronger wording is superseded.

The server holding a real CRDT document, rather than blindly relaying opaque bytes, is what makes the rest of the design work: it can answer a reconnecting client with a state-vector diff instead of replaying every update since the room opened, and it can say how long a document actually is - which is what an operator-set `MAX_DOC_BYTES` needs, and what lets the room be told what it is holding.

Clients cannot assign their own UUID, claim another user's identity without their session token, set their own `clientID`, or alter uploader metadata. Given §21.1, clients *can* freely access any room they know the code for; that is the accepted model, not a bug.

---

## 37. Testing Requirements

### Rooms
create; join; multiple users join; user leaves; a non-final user leaving does not destroy the room; final user leaves; room cleanup; reopening a destroyed room yields a fresh empty room; refresh reconnects to the existing room with state intact; join during `EMPTY_GRACE` resurrects the same instance; join during `CLOSING` yields a new instance; `MAX_ROOMS` rejection.

### Collaboration
single-user editing; two-user editing; simultaneous insertion at the same position with **both insertions preserved**; concurrent overlapping deletion; insertion into a concurrently deleted range; the join-order tie-break with its observed direction asserted (§7.1); out-of-order update delivery; duplicate update delivery; reconnect after missing updates; convergence of three clients from divergent states; undo does not revert another user's text; metadata LWW by `opSeq`; document deleted while being edited.

### Files
upload; multiple uploads; duplicate filenames producing distinct files; download by ID; download with a Unicode filename; delete; simultaneous uploads by different users; insufficient disk space rejected before any bytes are written; two concurrent uploads that individually fit but jointly do not (the reservation-ledger test); interrupted upload resumed from the committed offset; abort leaves no `.part` file; stale-upload reaper; boot sweep clears the data root; all files removed on room destruction; a `fileId` from room A returns 404 under room B's code.

### Presence
join event; leave event; reconnect within grace keeps identity and does not emit leave/join; reconnect after grace produces a new identity; heartbeat timeout removes a silent connection; temporary network failure does not destroy a single-user room.

### 37.1 Testability Requirements (new)

Several of the tests above are impossible to write honestly against hard-coded system calls. The following must be `typing.Protocol` interfaces injected at construction, never module-level imports called directly:

* `Clock` — `now()` and timer scheduling, so grace periods and reaper intervals can be advanced instantly in tests rather than waited out.
* `DiskSpaceProvider` — so "insufficient disk space" is testable without filling a real volume.
* `FileStore` — so upload interruption and orphan cleanup are testable with a fake backend.

Production wires the real filesystem implementations via FastAPI dependency injection. If these are not injectable, the disk-space and timeout tests will end up either absent or fabricated, and the acceptance checklist will pass while the behaviour is unverified.

**Tooling.** `pytest` with `pytest-asyncio` in strict mode. WebSocket tests use Starlette's `TestClient` for synchronous cases and `httpx.AsyncClient` with an ASGI transport for concurrent multi-client cases. Every collaboration test involving two users must drive two genuinely independent connections; a single client sending two messages does not exercise concurrency and will pass while the real behaviour is broken.

Do not use `time.sleep` or `asyncio.sleep` to wait out a grace period in a test. Advance the injected `Clock`. A suite that sleeps through §2.1's timers will take minutes and will be quietly deleted by whoever maintains it next.

For the cross-language check in §0.1, the browser side must be exercised through Playwright against a real server process, since the whole point is testing the JavaScript-to-Python wire format. A pure-Python test of `pycrdt` against itself proves nothing about compatibility.

---

## 38. Project Quality

Clean separation of concerns per §28. Environment variables for all configuration, validated at startup with fail-fast on anything missing or malformed. No hardcoded domains, ports, paths, or secrets.

Comment the non-obvious: the lifecycle state machine, the `CLOSING` versus `EMPTY_GRACE` boundary, `clientID` assignment and its relationship to tie-breaking, the reservation ledger, resume-token rebinding, and the §34 race.

---

## 39. Documentation

README covering: overview; architecture with diagram; local setup; running frontend and backend; every environment variable with its default and effect; production build; EC2 deployment; DNS; HTTPS with certbot; Nginx WebSocket configuration; storage configuration; the room lifecycle state machine; cleanup; the security model from §21.1 stated plainly; known limitations; testing.

**Two sections are mandatory and must not be reduced to a sentence:**

1. **How conflict resolution actually works**, with a worked two-user example showing concurrent insertion at the same position, the resulting text, and why. Include the observed tie-break direction and the pinned Yjs version.
2. **How ephemeral cleanup is guaranteed**, tracing a room from last-user-leaves through `EMPTY_GRACE`, `CLOSING`, directory deletion, and verification, and explaining why the §34 race cannot resurrect data.

**Known limitations to state explicitly:** server restart loses all rooms; **single Uvicorn worker only, so the application cannot be scaled horizontally or even vertically by adding workers** (§20.1); room codes are enumerable and confer full access; CRDT documents accumulate tombstones, so a long-lived heavily-edited room grows in memory (bounded by `MEMORY_HEADROOM_BYTES` reporting the pressure rather than by any size cap, and in practice by rooms being short-lived); uploads require ordered chunks; large uploads consume disk for their full duration; `pycrdt` is a compiled extension, so the deployment target's architecture must have a matching wheel.

The README must also record the **verified version quintet** from §0.1 and state that upgrading any of the five requires re-running the compatibility test.

---

## 40. Acceptance Criteria

### Room
- [ ] No signup. One-click room creation. Short shareable URL.
- [ ] Multiple users join; each receives a unique server-generated UUID.
- [ ] Users can set display names; all users are equal; no password.
- [ ] Room survives while any participant remains.
- [ ] Refresh does not destroy the room, including for a sole participant.
- [ ] A non-final user leaving does not destroy the room.
- [ ] Final participant leaving destroys the room after the grace period.
- [ ] Joining during `EMPTY_GRACE` restores the same room.
- [ ] Joining during or after `CLOSING` produces a new empty room.
- [ ] No room data intentionally persisted.

### Collaboration
- [ ] Multiple users edit simultaneously; changes appear live.
- [ ] Cursors and selections are synchronized and labelled.
- [ ] Concurrent editing converges deterministically on every client.
- [ ] **Concurrent insertions are both preserved; no edit is discarded.**
- [ ] Join order breaks exact ties, with the direction asserted by a test.
- [ ] Metadata conflicts resolve by server `opSeq`, last write winning.
- [ ] Undo/redo affects only the local user's operations.
- [ ] Multiple documents and syntax highlighting work.

### Files
- [ ] Arbitrary types; multiple files; duplicate filenames never overwrite.
- [ ] Upload progress shown; uploader and timestamp recorded server-side.
- [ ] Every participant can download and delete every file.
- [ ] Insufficient space rejects cleanly, including the concurrent-upload case.
- [ ] Failed, aborted, and abandoned uploads leave no orphaned data.
- [ ] Resumable uploads work across a simulated network interruption.
- [ ] All room files deleted on room close, with the directory verified gone.
- [ ] Boot sweep clears the data root on restart.

### Infrastructure
- [ ] Runs on a single EC2 instance using local filesystem storage.
- [ ] **Uvicorn runs with exactly one worker, asserted at startup.**
- [ ] Browser `yjs` and server `pycrdt` converge, proven by the §0.1 test at the pinned versions.
- [ ] No blocking call on the event loop: a large `rmtree` during cleanup does not stall other rooms.
- [ ] `pycrdt` installs from a wheel on the target architecture.
- [ ] No database, no S3, no ALB.
- [ ] HTTPS works with a valid Let's Encrypt certificate; `certbot renew --dry-run` passes.
- [ ] WSS works through Nginx; a connection stays alive past 60 seconds idle.
- [ ] No mixed-content or insecure-context errors in the browser console.
- [ ] Domain configurable via `PUBLIC_ORIGIN`; nothing hardcoded.
- [ ] Available storage displayed and enforced server-side.
- [ ] Restartable via systemd; room loss on restart documented and accepted.

---

## 41. Deliverables

Frontend source; backend source; collaborative editing implementation; upload/download implementation; room lifecycle management; automated tests (unit, integration, and Playwright multi-client); production configuration including systemd unit and Nginx site file; EC2 deployment instructions; HTTPS and DNS instructions; README; explanation of architectural decisions; explicit explanation of the ephemeral cleanup guarantee.

Priority order: **correctness > simplicity > maintainability > scalability.**

Add nothing that was not requested.

---

## 42. Delivery Milestones (new)

This is a multi-thousand-line project across two codebases plus tests plus deployment. Built in one pass it will look complete and be hollow in the middle, most likely in the collaboration engine and the tests. Build it in four gated stages; do not begin a stage until the previous stage's gate passes.

**M0 — Compatibility spike.** Before anything else, a throwaway proof that a browser `yjs` document synchronizes correctly with a `pycrdt` document over a FastAPI WebSocket: connect, sync, edit concurrently on both sides, converge, assert identical state. Perhaps a hundred lines, thrown away afterwards.
*Gate:* convergence demonstrated, versions pinned and recorded. This is the one assumption that would invalidate the whole architecture if false, so it is cheapest to test on day one rather than during M2.

**M1 — Skeleton, rooms, presence.** Landing page, room creation and joining, WebSocket transport with heartbeats, the full lifecycle state machine, resume tokens, presence UI.
*Gate:* every Room and Presence test in §37 passes. Refresh as a sole participant provably does not destroy the room.

**M2 — Collaborative editor.** `pycrdt` integration, server-held docs, CodeMirror binding, browser-side client ID assignment, awareness, multi-document management, syntax highlighting, scoped undo.
*Gate:* every Collaboration test passes, including the two-user simultaneous-insertion test, the client-ID-assignment test, and the scoped-undo test, run under Playwright with two real browser contexts. The README's worked conflict example is written and matches observed behaviour.

**M3 — Files.** Reservation ledger, resumable upload protocol, download by ID, deletion, the reaper, the boot sweep, storage display.
*Gate:* every Files test passes, including the concurrent-reservation test and the interrupted-resume test. After a full test run the data root is empty.

**M4 — Deployment.** EC2 provisioning, Nginx, certbot, systemd, DNS, README completion.
*Gate:* the Infrastructure checklist passes against a live instance on a real domain, including `certbot renew --dry-run` and a WebSocket held open past 60 seconds.

Before declaring completion, walk §40 line by line against the running system and fix anything unchecked.
