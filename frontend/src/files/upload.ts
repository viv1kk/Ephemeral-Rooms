/**
 * The browser half of the resumable upload protocol (spec section 15).
 *
 * Chunks go over HTTP rather than the WebSocket, which keeps large binary
 * transfers off the collaboration channel and gives native progress events and
 * backpressure. On failure the client re-queries the committed offset and
 * resumes from there, with exponential backoff and a bounded retry count.
 */

export type UploadState = 'starting' | 'uploading' | 'completing' | 'done' | 'error' | 'cancelled';

export interface UploadTask {
  localId: string;
  uploadId: string | null;
  file: File;
  sent: number;
  total: number;
  state: UploadState;
  error?: string;
}

const MAX_ATTEMPTS = 5;
const BASE_BACKOFF_MS = 400;

export interface UploadCallbacks {
  onProgress: (task: UploadTask) => void;
  /** Coarse, throttled to at most one message per second (spec section 5). */
  onBroadcast: (uploadId: string, percent: number) => void;
}

async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { message?: string };
    return body.message ?? 'That upload could not be completed.';
  } catch {
    return 'That upload could not be completed.';
  }
}

/** PUT one chunk, reporting byte-level progress through XHR. */
function putChunk(
  uploadId: string,
  blob: Blob,
  start: number,
  total: number,
  onBytes: (loaded: number) => void,
  signal: AbortSignal,
): Promise<{ ok: true; committedOffset: number } | { ok: false; status: number; body: unknown }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/uploads/${uploadId}`);
    xhr.setRequestHeader('Content-Range', `bytes ${start}-${start + blob.size - 1}/${total}`);
    xhr.upload.onprogress = (e) => onBytes(e.loaded);
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText) as unknown;
      } catch {
        /* an empty or non-JSON body is handled by the status check below */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve({ ok: true, committedOffset: (body as { committedOffset: number }).committedOffset });
      } else {
        resolve({ ok: false, status: xhr.status, body });
      }
    };
    xhr.onerror = () => reject(new Error('network'));
    xhr.onabort = () => reject(new DOMException('aborted', 'AbortError'));
    signal.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.send(blob);
  });
}

export async function runUpload(
  roomCode: string,
  userId: string,
  task: UploadTask,
  chunkBytes: number,
  callbacks: UploadCallbacks,
  signal: AbortSignal,
): Promise<void> {
  const update = (patch: Partial<UploadTask>) => {
    Object.assign(task, patch);
    callbacks.onProgress(task);
  };

  update({ state: 'starting' });

  const init = await fetch(`/api/rooms/${roomCode}/uploads`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: task.file.name, size: task.file.size, userId }),
    signal,
  });
  if (!init.ok) {
    update({ state: 'error', error: await readError(init) });
    return;
  }
  const { uploadId } = (await init.json()) as { uploadId: string };
  update({ uploadId, state: 'uploading' });

  let offset = 0;
  let attempts = 0;
  let lastBroadcast = 0;

  while (offset < task.file.size) {
    if (signal.aborted) return;
    const end = Math.min(offset + chunkBytes, task.file.size);
    const blob = task.file.slice(offset, end);
    const chunkStart = offset;

    let result;
    try {
      result = await putChunk(
        uploadId,
        blob,
        chunkStart,
        task.file.size,
        (loaded) => {
          update({ sent: chunkStart + loaded });
          const now = Date.now();
          if (now - lastBroadcast >= 1000) {
            lastBroadcast = now;
            callbacks.onBroadcast(uploadId, Math.floor(((chunkStart + loaded) / task.file.size) * 100));
          }
        },
        signal,
      );
    } catch (err) {
      if (signal.aborted || (err as Error).name === 'AbortError') return;
      attempts += 1;
      if (attempts >= MAX_ATTEMPTS) {
        update({ state: 'error', error: 'The connection failed and the upload could not resume.' });
        return;
      }
      await new Promise((r) => setTimeout(r, BASE_BACKOFF_MS * 2 ** (attempts - 1)));
      // Re-query where the server actually got to, rather than guessing.
      offset = await queryOffset(uploadId, offset);
      update({ sent: offset });
      continue;
    }

    if (result.ok) {
      attempts = 0;
      offset = result.committedOffset;
      update({ sent: offset });
      continue;
    }

    // 409 carries the authoritative offset, so resynchronize instead of failing.
    if (result.status === 409) {
      const body = result.body as { committedOffset?: number };
      if (typeof body?.committedOffset === 'number') {
        offset = body.committedOffset;
        update({ sent: offset });
        continue;
      }
    }
    const message = (result.body as { message?: string })?.message;
    update({ state: 'error', error: message ?? 'That upload could not be completed.' });
    return;
  }

  update({ state: 'completing' });
  const done = await fetch(`/api/uploads/${uploadId}/complete`, { method: 'POST', signal });
  if (!done.ok) {
    update({ state: 'error', error: await readError(done) });
    return;
  }
  update({ state: 'done', sent: task.file.size });
}

async function queryOffset(uploadId: string, fallback: number): Promise<number> {
  try {
    const resp = await fetch(`/api/uploads/${uploadId}`);
    if (!resp.ok) return fallback;
    return ((await resp.json()) as { committedOffset: number }).committedOffset;
  } catch {
    return fallback;
  }
}

/** Abort so the server deletes the partial data and releases its reservation. */
export async function abortUpload(uploadId: string): Promise<void> {
  try {
    await fetch(`/api/uploads/${uploadId}`, { method: 'DELETE' });
  } catch {
    // The reaper and the boot sweep are the other two layers that cover this.
  }
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}
