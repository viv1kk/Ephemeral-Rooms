/**
 * The file list and upload queue.
 *
 * The uploader's own progress bar is driven locally by the HTTP request's
 * progress events; only a coarse, throttled percentage is round-tripped
 * through the server for other participants (spec section 5).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { RoomFile } from '../ws/protocol';
import { abortUpload, formatBytes, runUpload, type UploadTask } from './upload';

interface FilePanelProps {
  roomCode: string;
  userId: string;
  chunkBytes: number;
  files: RoomFile[];
  remoteUploads: Record<string, { name: string; uploader: string; percent: number }>;
  maxUploadsPerUser: number;
  onDelete: (fileId: string) => void;
  onBroadcastProgress: (uploadId: string, percent: number) => void;
}

let localSeq = 0;

function nextLocalId(): string {
  localSeq += 1;
  return `local-${localSeq}`;
}

export function FilePanel({
  roomCode,
  userId,
  chunkBytes,
  files,
  remoteUploads,
  maxUploadsPerUser,
  onDelete,
  onBroadcastProgress,
}: FilePanelProps): JSX.Element {
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const controllers = useRef<Map<string, AbortController>>(new Map());
  const input = useRef<HTMLInputElement>(null);
  // Accepted but not yet begun, because this browser already has
  // maxUploadsPerUser transfers open. Drained by the pump below.
  const [queued, setQueued] = useState<UploadTask[]>([]);
  const started = useRef<Set<string>>(new Set());

  const bump = useCallback((task: UploadTask) => {
    setTasks((current) => current.map((t) => (t.localId === task.localId ? { ...task } : t)));
  }, []);

  const begin = useCallback(
    (task: UploadTask) => {
      const controller = new AbortController();
      controllers.current.set(task.localId, controller);
      void runUpload(
        roomCode,
        userId,
        task,
        chunkBytes,
        { onProgress: bump, onBroadcast: onBroadcastProgress },
        controller.signal,
      ).finally(() => {
        controllers.current.delete(task.localId);
        // It has left the queue, so nothing can start it again and the guard
        // entry is only holding a string. Dropped so a page that uploads all
        // day does not accumulate one per file.
        started.current.delete(task.localId);
        // A finished upload comes back as a `file_added` event and takes its
        // place in the list above, so the transient row retires shortly after
        // showing 100%. A failed one is kept: it carries the only explanation
        // the user gets, and is dismissed by hand.
        window.setTimeout(() => {
          setTasks((c) => c.filter((t) => t.localId !== task.localId || t.state === 'error'));
        }, 1200);
      });
    },
    [roomCode, userId, chunkBytes, bump, onBroadcastProgress],
  );

  const start = useCallback((chosen: FileList | null) => {
    if (chosen === null) return;
    // Every chosen file is accepted, however many there are and however large.
    // maxUploadsPerUser bounds how many transfers run AT ONCE - the server
    // refuses a further init past it - so the rest wait their turn here. They
    // used to be sliced off and silently discarded, with nothing in the UI to
    // say that half the selection was never going to arrive.
    const accepted = Array.from(chosen).map<UploadTask>((file) => ({
      localId: nextLocalId(),
      uploadId: null,
      file,
      sent: 0,
      total: file.size,
      state: 'queued',
    }));
    setTasks((current) => [...current, ...accepted]);
    setQueued((current) => [...current, ...accepted]);
    if (input.current !== null) input.current.value = '';
  }, []);

  // The pump: fills the concurrency window from the front of the queue, and
  // re-runs whenever a transfer finishes or more files are chosen. `started`
  // is what stops a task being launched twice when this re-runs before React
  // has applied the state change from the previous pass.
  useEffect(() => {
    if (queued.length === 0) return;
    const active = tasks.filter(
      (t) => t.state === 'starting' || t.state === 'uploading' || t.state === 'completing',
    ).length;
    const room = Math.max(0, maxUploadsPerUser - active);
    if (room === 0) return;

    const launching = queued.filter((t) => !started.current.has(t.localId)).slice(0, room);
    if (launching.length === 0) return;
    const ids = new Set(launching.map((t) => t.localId));
    for (const id of ids) started.current.add(id);
    setQueued((current) => current.filter((t) => !ids.has(t.localId)));
    for (const task of launching) {
      task.state = 'starting';
      bump(task);
      begin(task);
    }
  }, [queued, tasks, maxUploadsPerUser, begin, bump]);

  // Also serves as "dismiss" for a failed upload: aborting is still the right
  // call there, because the server may be holding a part file and a disk
  // reservation that would otherwise wait for the reaper.
  const cancel = useCallback((task: UploadTask) => {
    controllers.current.get(task.localId)?.abort();
    // Tell the server too, so the .part file and its reservation go now
    // rather than waiting for the reaper.
    if (task.uploadId !== null) void abortUpload(task.uploadId);
    setTasks((current) => current.filter((t) => t.localId !== task.localId));
    // A task cancelled while still waiting its turn has to leave the queue
    // too, or the pump would start it a moment later.
    setQueued((current) => current.filter((t) => t.localId !== task.localId));
    started.current.delete(task.localId);
  }, []);

  const ordered = [...files].sort((a, b) => b.uploadedAt - a.uploadedAt);

  return (
    <section className="panel files">
      <h2>Files</h2>

      <ul className="file-list" data-testid="file-list">
        {ordered.map((file) => (
          <li key={file.fileId}>
            <div className="file-name" title={file.name}>
              {file.name}
            </div>
            <div className="file-meta">
              {formatBytes(file.size)} · {file.uploaderName}
            </div>
            <div className="file-actions">
              <a
                className="button"
                href={`/api/rooms/${roomCode}/files/${file.fileId}`}
                download={file.name}
              >
                Download
              </a>
              <button className="danger" onClick={() => onDelete(file.fileId)}>
                Delete
              </button>
            </div>
          </li>
        ))}
        {ordered.length === 0 && tasks.length === 0 && (
          <li className="empty">No files yet.</li>
        )}
      </ul>

      {tasks.map((task) => (
        <div key={task.localId} className="upload">
          <div className="upload-head">
            <span className="file-name">{task.file.name}</span>
            {task.state === 'error' ? (
              // A failed row is never retired automatically, so without this it
              // would sit there for the life of the page.
              <button className="link" onClick={() => cancel(task)}>
                Dismiss
              </button>
            ) : task.state !== 'done' ? (
              <button className="link" onClick={() => cancel(task)}>
                Cancel
              </button>
            ) : null}
          </div>
          {task.state === 'error' ? (
            <div className="upload-error">{task.error}</div>
          ) : task.state === 'queued' ? (
            <span className="upload-percent waiting">Waiting to start</span>
          ) : (
            <>
              <progress max={task.total} value={task.sent} />
              <span className="upload-percent">
                {task.total === 0 ? 100 : Math.floor((task.sent / task.total) * 100)}%
              </span>
            </>
          )}
        </div>
      ))}

      {Object.entries(remoteUploads).map(([uploadId, info]) => (
        <div key={uploadId} className="upload remote">
          <div className="upload-head">
            <span className="file-name">{info.name}</span>
            <span className="tag muted">{info.uploader}</span>
          </div>
          <progress max={100} value={info.percent} />
          <span className="upload-percent">{info.percent}%</span>
        </div>
      ))}

      <input
        ref={input}
        type="file"
        multiple
        hidden
        data-testid="file-input"
        onChange={(e) => start(e.target.files)}
      />
      <button className="primary" onClick={() => input.current?.click()}>
        + Upload Files
      </button>
    </section>
  );
}
