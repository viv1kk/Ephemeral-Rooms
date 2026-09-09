/**
 * All room state in one hook.
 *
 * The ordering that matters: the join acknowledgement carries the assigned
 * clientId, and no CRDT replica is constructed until it arrives. Everything
 * downstream depends on that, so `identity` gates document creation
 * (spec section 7.1).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { CollabDocument } from '../editor/collab';
import { applyFrame, createCollabDocument, syncStep1Frame } from '../editor/collab';
import { RoomClient, type ConnectionStatus } from '../ws/client';
import type { JoinedEvent, Limits, RoomDocument, RoomFile, RoomUser, ServerEvent } from '../ws/protocol';

export interface Toast {
  id: number;
  message: string;
  tone: 'info' | 'error';
}

export interface Identity {
  userId: string;
  clientId: number;
  displayName: string;
  color: string;
}

export interface RoomStateValue {
  status: ConnectionStatus;
  identity: Identity | null;
  users: RoomUser[];
  documents: RoomDocument[];
  files: RoomFile[];
  activeDocumentId: string | null;
  collab: CollabDocument | null;
  storageAvailable: number;
  limits: Limits | null;
  toasts: Toast[];
  roomWasCreated: boolean;
  remoteUploads: Record<string, { name: string; uploader: string; percent: number; updatedAt: number }>;
  selectDocument: (documentId: string) => void;
  createDocument: (name: string) => void;
  renameDocument: (documentId: string, name: string) => void;
  deleteDocument: (documentId: string) => void;
  deleteFile: (fileId: string) => void;
  setDisplayName: (name: string) => void;
  broadcastUploadProgress: (uploadId: string, percent: number) => void;
  dismissToast: (id: number) => void;
  leave: () => void;
  client: RoomClient | null;
}

let toastSeq = 0;

/** Remove one in-flight upload, if it is still being tracked. */
function dropUpload(
  current: RoomStateValue['remoteUploads'],
  uploadId: string,
): RoomStateValue['remoteUploads'] {
  if (!(uploadId in current)) return current;
  const next = { ...current };
  delete next[uploadId];
  return next;
}

// Progress is broadcast at most once a second while a transfer is alive, so a
// row that has said nothing for this long belongs to an uploader whose tab
// died. The server's reaper announces the ones it catches, but it only runs
// every UPLOAD_STALE_MS; this stops a stuck bar sitting there until then.
const REMOTE_UPLOAD_STALE_MS = 45_000;

export function useRoom(roomCode: string): RoomStateValue {
  const [status, setStatus] = useState<ConnectionStatus>('connecting');
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [users, setUsers] = useState<RoomUser[]>([]);
  const [documents, setDocuments] = useState<RoomDocument[]>([]);
  const [files, setFiles] = useState<RoomFile[]>([]);
  const [activeDocumentId, setActiveDocumentId] = useState<string | null>(null);
  const [collab, setCollab] = useState<CollabDocument | null>(null);
  const [storageAvailable, setStorageAvailable] = useState(0);
  const [limits, setLimits] = useState<Limits | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [roomWasCreated, setRoomWasCreated] = useState(false);
  const [remoteUploads, setRemoteUploads] = useState<RoomStateValue['remoteUploads']>({});

  const clientRef = useRef<RoomClient | null>(null);
  const docsRef = useRef<Map<string, CollabDocument>>(new Map());
  const identityRef = useRef<Identity | null>(null);
  const activeRef = useRef<string | null>(null);

  const pushToast = useCallback((message: string, tone: Toast['tone'] = 'info') => {
    const id = (toastSeq += 1);
    setToasts((current) => [...current, { id, message, tone }]);
    window.setTimeout(() => setToasts((c) => c.filter((t) => t.id !== id)), 6000);
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  /** Build (or reuse) the replica for a document and start its sync. */
  const ensureDocument = useCallback((documentId: string): CollabDocument | null => {
    const id = identityRef.current;
    const client = clientRef.current;
    if (id === null || client === null) return null;

    const existing = docsRef.current.get(documentId);
    if (existing !== undefined) return existing;

    const created = createCollabDocument(documentId, id.clientId, (frame) =>
      client.sendBinary(frame),
    );
    docsRef.current.set(documentId, created);
    // Ask the server for a state-vector diff, and offer ours.
    client.send({ type: 'subscribe_document', documentId });
    client.sendBinary(syncStep1Frame(created));
    return created;
  }, []);

  const selectDocument = useCallback(
    (documentId: string) => {
      activeRef.current = documentId;
      setActiveDocumentId(documentId);
      setCollab(ensureDocument(documentId));
    },
    [ensureDocument],
  );

  const handleJoined = useCallback(
    (event: JoinedEvent) => {
      const id: Identity = {
        userId: event.userId,
        clientId: event.clientId,
        displayName: event.displayName,
        color: event.color,
      };
      identityRef.current = id;
      setIdentity(id);
      setUsers(event.room.users);
      setDocuments(event.room.documents);
      setFiles(event.room.files);
      setStorageAvailable(event.storageAvailable);
      setLimits(event.limits);
      if (event.roomWasCreated) setRoomWasCreated(true);

      // On a reconnect the replicas already exist and only need re-syncing;
      // Yjs flushes anything typed while offline as part of that exchange.
      const client = clientRef.current;
      if (client !== null) {
        for (const [documentId, doc] of docsRef.current) {
          client.send({ type: 'subscribe_document', documentId });
          client.sendBinary(syncStep1Frame(doc));
        }
      }

      const stillThere =
        activeRef.current !== null &&
        event.room.documents.some((d) => d.documentId === activeRef.current);
      if (!stillThere) {
        const first = event.room.documents[0];
        if (first !== undefined) selectDocument(first.documentId);
        else {
          activeRef.current = null;
          setActiveDocumentId(null);
          setCollab(null);
        }
      }
    },
    [selectDocument],
  );

  const handleEvent = useCallback(
    (event: ServerEvent) => {
      switch (event.type) {
        case 'user_joined':
          setUsers((c) => [...c.filter((u) => u.userId !== event.user.userId), { ...event.user, color: event.color }]);
          pushToast(`${event.user.displayName} joined`);
          break;
        case 'user_left':
          setUsers((c) => c.filter((u) => u.userId !== event.userId));
          pushToast(`${event.displayName} left`);
          break;
        case 'user_updated':
          setUsers((c) => c.map((u) => (u.userId === event.user.userId ? { ...u, ...event.user } : u)));
          if (event.user.userId === identityRef.current?.userId) {
            setIdentity((cur) => (cur === null ? cur : { ...cur, displayName: event.user.displayName }));
          }
          break;
        case 'presence':
          setUsers(event.users);
          break;
        case 'document_created':
          setDocuments((c) => [...c, event.document]);
          break;
        case 'document_renamed':
          setDocuments((c) =>
            c.map((d) => (d.documentId === event.documentId ? { ...d, name: event.name } : d)),
          );
          break;
        case 'document_deleted': {
          setDocuments((c) => c.filter((d) => d.documentId !== event.documentId));
          const doc = docsRef.current.get(event.documentId);
          if (doc !== undefined) {
            doc.destroy();
            docsRef.current.delete(event.documentId);
          }
          if (activeRef.current === event.documentId) {
            // Non-blocking notice, then close the editor and move on
            // (spec section 8.1).
            pushToast(`"${event.name}" was deleted by ${event.deletedBy}`);
            activeRef.current = null;
            setActiveDocumentId(null);
            setCollab(null);
          }
          break;
        }
        case 'file_added':
          setFiles((c) => [...c.filter((f) => f.fileId !== event.file.fileId), event.file]);
          setRemoteUploads((c) => dropUpload(c, event.uploadId));
          break;
        case 'upload_ended':
          // Cancelled or reaped: no file is coming, so retire the row.
          setRemoteUploads((c) => dropUpload(c, event.uploadId));
          break;
        case 'file_deleted':
          setFiles((c) => c.filter((f) => f.fileId !== event.fileId));
          break;
        case 'upload_progress':
          setRemoteUploads((c) => ({
            ...c,
            [event.uploadId]: {
              name: event.filename,
              uploader: event.uploaderName,
              percent: event.percent,
              updatedAt: Date.now(),
            },
          }));
          break;
        case 'storage':
          setStorageAvailable(event.available);
          break;
        case 'room_closing':
          pushToast(event.reason, 'error');
          break;
        case 'error':
          pushToast(event.message, 'error');
          break;
        default:
          break;
      }
    },
    [pushToast],
  );

  const handleBinary = useCallback((frame: Uint8Array) => {
    const reply = applyFrame(frame, docsRef.current);
    if (reply !== null) clientRef.current?.sendBinary(reply);
  }, []);

  useEffect(() => {
    const client = new RoomClient(roomCode, {
      onEvent: handleEvent,
      onBinary: handleBinary,
      onStatus: setStatus,
      onJoined: handleJoined,
    });
    clientRef.current = client;
    client.connect();

    // Closing the tab is detected by the heartbeat timeout, not by an unload
    // handler (spec section 27). The spec permits a sendBeacon hint purely to
    // shorten that delay, but it is deliberately not sent here: an unsigned
    // beacon naming a user is a way for anyone to drop anyone, and the
    // heartbeat already gives the correct result within a few seconds.

    const docs = docsRef.current;
    return () => {
      client.dispose();
      clientRef.current = null;
      for (const doc of docs.values()) doc.destroy();
      docs.clear();
    };
  }, [roomCode, handleEvent, handleBinary, handleJoined]);

  /**
   * A read-only diagnostic handle.
   *
   * Spec section 7.1 requires an explicit test that the server-assigned
   * clientID really is set on the BROWSER's Y.Doc, because getting that
   * backwards produces a system that appears to work while silently breaking
   * the join-order tie-break. The value lives inside the Y.Doc and cannot be
   * observed from the DOM, so it is surfaced here. Nothing writes through it.
   */
  useEffect(() => {
    (window as unknown as { __room: unknown }).__room = {
      userId: identity?.userId ?? null,
      clientId: identity?.clientId ?? null,
      docClientId: collab?.doc.clientID ?? null,
      documentId: collab?.documentId ?? null,
      text: () => collab?.text.toString() ?? null,
      // Awareness is the hardest part of this to debug from the outside: a
      // missing caret can mean the state expired, the cursor was published as
      // null, or the editor simply lost focus, and the DOM looks identical in
      // all three cases.
      awareness: () => {
        if (collab === null) return null;
        const states: Record<string, unknown> = {};
        collab.awareness.getStates().forEach((state, clientId) => {
          states[String(clientId)] = {
            name: (state as { user?: { name?: string } }).user?.name ?? null,
            hasCursor: (state as { cursor?: unknown }).cursor != null,
          };
        });
        return { self: collab.doc.clientID, states };
      },
    };
  }, [identity, collab]);

  // Backstop for an uploader whose tab died mid-transfer: no file_added and no
  // upload_ended will ever arrive for them, so nothing else would clear the row.
  useEffect(() => {
    const timer = window.setInterval(() => {
      setRemoteUploads((current) => {
        const cutoff = Date.now() - REMOTE_UPLOAD_STALE_MS;
        const live = Object.entries(current).filter(([, u]) => u.updatedAt >= cutoff);
        if (live.length === Object.keys(current).length) return current;
        return Object.fromEntries(live);
      });
    }, 10_000);
    return () => window.clearInterval(timer);
  }, []);

  const send = useCallback((message: Parameters<RoomClient['send']>[0]) => {
    clientRef.current?.send(message);
  }, []);

  return {
    status,
    identity,
    users,
    documents,
    files,
    activeDocumentId,
    collab,
    storageAvailable,
    limits,
    toasts,
    roomWasCreated,
    remoteUploads,
    selectDocument,
    createDocument: useCallback((name: string) => send({ type: 'create_document', name }), [send]),
    renameDocument: useCallback(
      (documentId: string, name: string) => send({ type: 'rename_document', documentId, name }),
      [send],
    ),
    deleteDocument: useCallback(
      (documentId: string) => send({ type: 'delete_document', documentId }),
      [send],
    ),
    deleteFile: useCallback((fileId: string) => send({ type: 'delete_file', fileId }), [send]),
    setDisplayName: useCallback((name: string) => send({ type: 'set_name', displayName: name }), [send]),
    broadcastUploadProgress: useCallback(
      (uploadId: string, percent: number) => send({ type: 'upload_progress', uploadId, percent }),
      [send],
    ),
    dismissToast,
    leave: useCallback(() => clientRef.current?.leave(), []),
    client: clientRef.current,
  };
}
