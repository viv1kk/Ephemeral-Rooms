import type { RoomUser } from '../ws/protocol';
import type { Toast } from '../routes/useRoom';

interface PresenceProps {
  users: RoomUser[];
  selfId: string | null;
}

export function Presence({ users, selfId }: PresenceProps): JSX.Element {
  const ordered = [...users].sort((a, b) => a.joinedAt - b.joinedAt);
  return (
    <section className="panel presence">
      <h2>
        {users.length} {users.length === 1 ? 'person' : 'people'} in this room
      </h2>
      <ul data-testid="presence-list">
        {ordered.map((user) => (
          <li key={user.userId} className={user.connected ? '' : 'reconnecting'}>
            <span className="dot" style={{ background: user.color ?? '#8a8f98' }} aria-hidden />
            <span className="presence-name">{user.displayName}</span>
            {user.userId === selfId && <span className="tag">you</span>}
            {/* A disconnected user is dimmed rather than removed, so a brief
                network blip does not read as leaving (spec section 23). */}
            {!user.connected && <span className="tag muted">reconnecting</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

interface ToastsProps {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}

export function Toasts({ toasts, onDismiss }: ToastsProps): JSX.Element {
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <button
          key={toast.id}
          className={`toast ${toast.tone}`}
          onClick={() => onDismiss(toast.id)}
          title="Dismiss"
        >
          {toast.message}
        </button>
      ))}
    </div>
  );
}
