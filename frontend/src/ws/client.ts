/**
 * The room WebSocket: one socket per client carrying all real-time traffic.
 *
 * Reconnection is automatic, with exponential backoff and jitter. The resume
 * token lives in sessionStorage rather than localStorage, so it dies with the
 * tab and two tabs are correctly treated as two separate users
 * (spec section 4.1).
 */

import {
  type ClientMessage,
  type JoinedEvent,
  type ServerEvent,
  websocketUrl,
} from './protocol';

export type ConnectionStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected';

const BASE_RETRY_MS = 500;
const MAX_RETRY_MS = 15_000;

function tokenKey(roomCode: string): string {
  return `room-session:${roomCode}`;
}

export function readToken(roomCode: string): string | null {
  try {
    return sessionStorage.getItem(tokenKey(roomCode));
  } catch {
    return null;
  }
}

function writeToken(roomCode: string, token: string): void {
  try {
    sessionStorage.setItem(tokenKey(roomCode), token);
  } catch {
    /* private browsing; the user simply joins as new on refresh */
  }
}

export interface RoomClientHandlers {
  onEvent: (event: ServerEvent) => void;
  onBinary: (frame: Uint8Array) => void;
  onStatus: (status: ConnectionStatus) => void;
  /** Fires on every successful join, including reconnects. */
  onJoined: (event: JoinedEvent) => void;
}

export class RoomClient {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private closedByUs = false;
  private retryTimer: number | null = null;

  constructor(
    private readonly roomCode: string,
    private readonly handlers: RoomClientHandlers,
  ) {}

  connect(): void {
    this.closedByUs = false;
    this.handlers.onStatus(this.attempt === 0 ? 'connecting' : 'reconnecting');

    const ws = new WebSocket(websocketUrl());
    ws.binaryType = 'arraybuffer';
    this.ws = ws;

    ws.onopen = () => {
      // The join must be acknowledged before any sync traffic, because the
      // browser needs its assigned clientId to build the Y.Doc (section 7.1).
      this.send({ type: 'join', roomCode: this.roomCode, sessionToken: readToken(this.roomCode) });
    };

    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        this.handlers.onBinary(new Uint8Array(ev.data));
        return;
      }
      let event: ServerEvent;
      try {
        event = JSON.parse(ev.data as string) as ServerEvent;
      } catch {
        return;
      }

      if (event.type === 'ping') {
        this.send({ type: 'pong' });
        return;
      }
      if (event.type === 'joined') {
        this.attempt = 0;
        writeToken(this.roomCode, event.sessionToken);
        this.handlers.onStatus('connected');
        this.handlers.onJoined(event);
      }
      this.handlers.onEvent(event);
    };

    ws.onclose = () => {
      this.ws = null;
      if (this.closedByUs) {
        this.handlers.onStatus('disconnected');
        return;
      }
      this.handlers.onStatus('reconnecting');
      this.scheduleRetry();
    };

    ws.onerror = () => {
      /* onclose always follows; retry logic lives there */
    };
  }

  private scheduleRetry(): void {
    this.attempt += 1;
    // Exponential backoff with jitter, so a server restart does not bring
    // every client back in the same millisecond.
    const capped = Math.min(BASE_RETRY_MS * 2 ** (this.attempt - 1), MAX_RETRY_MS);
    const delay = capped / 2 + Math.random() * (capped / 2);
    this.retryTimer = window.setTimeout(() => this.connect(), delay);
  }

  send(message: ClientMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message));
  }

  sendBinary(frame: Uint8Array): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      // Copy into a standalone buffer: a subarray view would send the whole
      // underlying buffer.
      this.ws.send(frame.slice().buffer);
    }
  }

  get isOpen(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  /** Explicit departure: no reconnect grace at all (spec section 27). */
  leave(): void {
    this.closedByUs = true;
    this.send({ type: 'leave' });
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    try {
      sessionStorage.removeItem(tokenKey(this.roomCode));
    } catch {
      /* ignore */
    }
    this.ws?.close();
    this.ws = null;
  }

  /** Tear down without discarding the resume token, e.g. on unmount. */
  dispose(): void {
    this.closedByUs = true;
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.ws?.close();
    this.ws = null;
  }
}
