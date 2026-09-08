/**
 * The file list and upload queue.
 *
 * The uploader's own progress bar is driven locally by the HTTP request's
 * progress events; only a coarse, throttled percentage is round-tripped
 * through the server for other participants (spec section 5).
 */

import { useCallback, useRef, useState } from 'react';
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

  const bump = useCallback((task: UploadTask) => {
    setTasks((current) => current.map((t) => (t.localId === task.localId ? { ...task } : t)));
  }, []);

  const start = useCallback(
    (chosen: FileList | null) => {
      if (chosen === null) return;
      const active = tasks.filter((t) => t.state === 'uploading' || t.state === 'starting').length;
      const room = Math.max(0, maxUploadsPerUser - active);
      const accepted = Array.from(chosen).slice(0, room);

      for (const file of accepted) {
        const task: UploadTask = {
          localId: `local-${(localSeq += 1)}`,
          uploadId: null,
          file,
          sent: 0,
          total: file.size,
          state: 'starting',
        };
        setTasks((current) => [...current, task]);

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
          // Completed uploads arrive back as a `file_added` event, so the
          // transient row can go.
          window.setTimeout(
            () => setTasks((c) => c.filter((t) => t.localId !== task.localId || t.state === 'error')),
            1500,
          );
        });
      }
      if (input.current !== null) input.current.value = '';
    },
    [roomCode, userId, chunkBytes, tasks, maxUploadsPerUser, bump, onBroadcastProgress],
  );

  const cancel = useCallback((task: UploadTask) => {
    controllers.current.get(task.localId)?.abort();
    // Tell the server too, so the .part file and its reservation go now
    // rather than waiting for the reaper.
    if (task.uploadId !== null) void abortUpload(task.uploadId);
    setTasks((current) => current.filter((t) => t.localId !== task.localId));
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
            {task.state !== 'error' && task.state !== 'done' && (
              <button className="link" onClick={() => cancel(task)}>
                Cancel
              </button>
            )}
          </div>
          {task.state === 'error' ? (
            <div className="upload-error">{task.error}</div>
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
