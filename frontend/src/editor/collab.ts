/**
 * The per-document CRDT replica and its sync plumbing.
 *
 * The load-bearing detail: the tie-breaking clientID is set on the BROWSER's
 * Y.Doc, before the editor binding is attached and before the first sync frame
 * is sent. Insertions are authored here, so this is the ID the CRDT compares
 * when breaking ties; setting it on the server's document instead produces a
 * system that appears to work while silently breaking the join-order rule
 * (spec section 7.1).
 */

import * as Y from 'yjs';
import { Awareness, applyAwarenessUpdate, encodeAwarenessUpdate, removeAwarenessStates } from 'y-protocols/awareness';
import { readSyncMessage, writeSyncStep1, writeUpdate } from 'y-protocols/sync';
import * as encoding from 'lib0/encoding';
import * as decoding from 'lib0/decoding';
import { MESSAGE_AWARENESS, MESSAGE_SYNC, decodeFrame, encodeFrame } from '../ws/protocol';

const LOCAL_ORIGIN = 'local';
const REMOTE_ORIGIN = 'remote';

export interface CollabDocument {
  documentId: string;
  doc: Y.Doc;
  text: Y.Text;
  awareness: Awareness;
  undoManager: Y.UndoManager;
  destroy: () => void;
}

export type FrameSender = (frame: Uint8Array) => void;

/**
 * Build a replica for one document.
 *
 * `clientId` is the join sequence number the server assigned in the join
 * acknowledgement. It must be applied before anything else touches the doc.
 */
export function createCollabDocument(
  documentId: string,
  clientId: number,
  send: FrameSender,
): CollabDocument {
  const doc = new Y.Doc();
  doc.clientID = clientId;

  const text = doc.getText('text');
  const awareness = new Awareness(doc);

  // Undo is scoped to this user's own operations. Ctrl+Z must never revert
  // another participant's typing (spec section 10).
  const undoManager = new Y.UndoManager(text, {
    trackedOrigins: new Set([LOCAL_ORIGIN, null]),
  });

  const onUpdate = (update: Uint8Array, origin: unknown) => {
    if (origin === REMOTE_ORIGIN) return;
    const enc = encoding.createEncoder();
    encoding.writeVarUint(enc, MESSAGE_SYNC);
    writeUpdate(enc, update);
    send(encodeFrame(documentId, encoding.toUint8Array(enc)));
  };

  const onAwareness = (
    { added, updated, removed }: { added: number[]; updated: number[]; removed: number[] },
    origin: unknown,
  ) => {
    if (origin === REMOTE_ORIGIN) return;
    const changed = added.concat(updated, removed);
    if (changed.length === 0) return;
    const enc = encoding.createEncoder();
    encoding.writeVarUint(enc, MESSAGE_AWARENESS);
    encoding.writeVarUint8Array(enc, encodeAwarenessUpdate(awareness, changed));
    send(encodeFrame(documentId, encoding.toUint8Array(enc)));
  };

  /**
   * Keep `awareness.clientID` equal to `doc.clientID`.
   *
   * yjs reassigns `doc.clientID` whenever a REMOTE transaction carries structs
   * authored under the id this replica is currently using, because it cannot
   * tell "my own earlier work coming back" from "someone else is squatting on
   * my id". For this application that is not an edge case, it is every resume:
   * a resumed session keeps its server-assigned clientID (spec section 4.1),
   * gets a brand-new Y.Doc, and the first sync hands it its own earlier
   * writing as a remote update. Reload after typing and it fires every time.
   *
   * `Awareness` caches `doc.clientID` in its constructor and never re-reads it,
   * so after the swap the local cursor stays filed under the OLD id while
   * `y-codemirror.next` compares against the NEW one. Both of its guards then
   * invert, and the editor gets much worse than merely wrong:
   *
   *   - Its re-entrancy guard is "only dispatch when some client OTHER than us
   *     changed". Our own cursor now looks like someone else's, so every
   *     cursor move dispatches a transaction from inside a ViewPlugin's
   *     update() and CodeMirror throws "Calls to EditorView.update are not
   *     allowed while an update is in progress". One throw per keystroke.
   *   - Its skip-self check stops matching, so this user is painted a second,
   *     "remote" caret widget on top of their own native one. Arrow keys then
   *     crawl, and worst to the RIGHT, because that is the direction that has
   *     to step over the widget - the caret is inserted with side: 1.
   *
   * Re-filing the state under the new id fixes both. Order matters: the old id
   * is removed first so peers drop the caret that is about to be orphaned,
   * rather than holding a ghost until it times out.
   *
   * Hooked on `afterTransactionCleanup` because that is emitted immediately
   * after the reassignment; `afterTransaction` runs before it and would still
   * see the old value.
   */
  const realignAwarenessClientId = () => {
    if (awareness.clientID === doc.clientID) return;
    const local = awareness.getLocalState();
    removeAwarenessStates(awareness, [awareness.clientID], 'clientid-reassigned');
    awareness.clientID = doc.clientID;
    if (local !== null) awareness.setLocalState(local);
  };

  doc.on('update', onUpdate);
  doc.on('afterTransactionCleanup', realignAwarenessClientId);
  awareness.on('update', onAwareness);

  return {
    documentId,
    doc,
    text,
    awareness,
    undoManager,
    destroy: () => {
      doc.off('update', onUpdate);
      doc.off('afterTransactionCleanup', realignAwarenessClientId);
      awareness.off('update', onAwareness);
      removeAwarenessStates(awareness, [doc.clientID], 'unmount');
      undoManager.destroy();
      awareness.destroy();
      doc.destroy();
    },
  };
}

/** The SYNC_STEP1 that opens a sync, carrying this replica's state vector. */
export function syncStep1Frame(collab: CollabDocument): Uint8Array {
  const enc = encoding.createEncoder();
  encoding.writeVarUint(enc, MESSAGE_SYNC);
  writeSyncStep1(enc, collab.doc);
  return encodeFrame(collab.documentId, encoding.toUint8Array(enc));
}

/**
 * Apply an inbound frame to the matching replica.
 *
 * Returns a reply frame when the peer's message requires one. Frames for
 * documents this client does not hold are ignored, which is what makes an
 * update that arrives after a delete harmless.
 */
export function applyFrame(
  frame: Uint8Array,
  documents: Map<string, CollabDocument>,
): Uint8Array | null {
  const decoded = decodeFrame(frame);
  if (decoded === null) return null;
  const collab = documents.get(decoded.documentId);
  if (collab === undefined) return null;

  const dec = decoding.createDecoder(decoded.payload);
  const messageType = decoding.readVarUint(dec);

  if (messageType === MESSAGE_SYNC) {
    const enc = encoding.createEncoder();
    encoding.writeVarUint(enc, MESSAGE_SYNC);
    readSyncMessage(dec, enc, collab.doc, REMOTE_ORIGIN);
    // length 1 means the encoder holds only the message-type byte, so there
    // is nothing to reply with.
    if (encoding.length(enc) > 1) {
      return encodeFrame(collab.documentId, encoding.toUint8Array(enc));
    }
    return null;
  }

  if (messageType === MESSAGE_AWARENESS) {
    applyAwarenessUpdate(collab.awareness, decoding.readVarUint8Array(dec), REMOTE_ORIGIN);
  }
  return null;
}

export { LOCAL_ORIGIN };
