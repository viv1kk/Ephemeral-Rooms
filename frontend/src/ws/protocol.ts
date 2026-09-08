/**
 * The wire vocabulary, mirroring backend/app/ws/events.py and messages.py.
 *
 * Binary frames carry a leading channel tag and the 16 raw bytes of the target
 * document's UUID, then a standard y-protocol message:
 *
 *     [tag:1][documentId:16][y-protocol message ...]
 */

export const CHANNEL_CRDT = 0x01;
export const MESSAGE_SYNC = 0;
export const MESSAGE_AWARENESS = 1;

export interface RoomUser {
  userId: string;
  displayName: string;
  clientId: number;
  joinedAt: number;
  connected: boolean;
  color?: string;
}

export interface RoomDocument {
  documentId: string;
  name: string;
  createdAt: number;
  createSeq: number;
}

export interface RoomFile {
  fileId: string;
  name: string;
  size: number;
  uploaderId: string;
  uploaderName: string;
  uploadedAt: number;
}

export interface Limits {
  maxDocs: number;
  maxDocBytes: number;
  maxFiles: number;
  maxFileBytes: number;
  maxDisplayNameChars: number;
  maxDocNameChars: number;
  maxFilenameChars: number;
  uploadChunkBytes: number;
  maxUploadsPerUser: number;
  heartbeatIntervalMs: number;
}

export interface JoinedEvent {
  type: 'joined';
  userId: string;
  clientId: number;
  sessionToken: string;
  displayName: string;
  color: string;
  roomCode: string;
  resumed: boolean;
  roomWasCreated: boolean;
  room: { roomCode: string; users: RoomUser[]; documents: RoomDocument[]; files: RoomFile[] };
  storageAvailable: number;
  limits: Limits;
}

export type ServerEvent =
  | JoinedEvent
  | { type: 'user_joined'; user: RoomUser; color: string }
  | { type: 'user_left'; userId: string; displayName: string }
  | { type: 'user_updated'; user: RoomUser; opSeq: number }
  | { type: 'presence'; users: RoomUser[] }
  | { type: 'document_created'; document: RoomDocument; opSeq: number }
  | { type: 'document_renamed'; documentId: string; name: string; opSeq: number }
  | { type: 'document_deleted'; documentId: string; name: string; deletedBy: string; opSeq: number }
  | { type: 'file_added'; file: RoomFile; uploadId: string }
  | { type: 'upload_ended'; uploadId: string; reason: string }
  | { type: 'file_deleted'; fileId: string; name: string; deletedBy: string }
  | { type: 'upload_progress'; uploadId: string; uploaderName: string; filename: string; percent: number }
  | { type: 'storage'; available: number }
  | { type: 'ping'; t: number }
  | { type: 'room_closing'; reason: string }
  | { type: 'error'; code: string; message: string; reference?: string };

export type ClientMessage =
  | { type: 'join'; roomCode: string; sessionToken: string | null }
  | { type: 'leave' }
  | { type: 'pong' }
  | { type: 'set_name'; displayName: string }
  | { type: 'create_document'; name: string }
  | { type: 'rename_document'; documentId: string; name: string }
  | { type: 'delete_document'; documentId: string }
  | { type: 'subscribe_document'; documentId: string }
  | { type: 'delete_file'; fileId: string }
  | { type: 'upload_progress'; uploadId: string; percent: number };

const HEX = Array.from({ length: 256 }, (_, i) => i.toString(16).padStart(2, '0'));

/** Pack a UUID string into its 16 raw bytes. */
export function uuidToBytes(uuid: string): Uint8Array {
  const hex = uuid.replace(/-/g, '');
  const out = new Uint8Array(16);
  for (let i = 0; i < 16; i += 1) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

export function bytesToUuid(bytes: Uint8Array): string {
  let hex = '';
  for (let i = 0; i < 16; i += 1) hex += HEX[bytes[i]!];
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function encodeFrame(documentId: string, payload: Uint8Array): Uint8Array {
  const frame = new Uint8Array(1 + 16 + payload.length);
  frame[0] = CHANNEL_CRDT;
  frame.set(uuidToBytes(documentId), 1);
  frame.set(payload, 17);
  return frame;
}

export function decodeFrame(frame: Uint8Array): { documentId: string; payload: Uint8Array } | null {
  if (frame.length < 18 || frame[0] !== CHANNEL_CRDT) return null;
  return {
    documentId: bytesToUuid(frame.subarray(1, 17)),
    payload: frame.subarray(17),
  };
}

/**
 * Derived from window.location so https maps to wss and a mixed-content error
 * is impossible. Never hardcode this (spec section 22).
 */
export function websocketUrl(): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/ws`;
}
