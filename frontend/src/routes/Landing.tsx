import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

export function Landing(): JSX.Element {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState('');

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const resp = await fetch('/api/rooms', { method: 'POST' });
      if (!resp.ok) {
        const body = (await resp.json()) as { message?: string };
        setError(body.message ?? 'The room could not be created.');
        return;
      }
      const { roomCode } = (await resp.json()) as { roomCode: string };
      navigate(`/room/${roomCode}`);
    } catch {
      setError('The server could not be reached.');
    } finally {
      setBusy(false);
    }
  };

  const join = (e: React.FormEvent) => {
    e.preventDefault();
    if (/^\d{4}$/.test(code)) navigate(`/room/${code}`);
  };

  return (
    <main className="landing">
      <div className="landing-card">
        <h1>Temporary Workspace</h1>
        <p className="landing-tagline">Collaborate. Share files. Nothing is saved.</p>

        <button className="primary large" onClick={create} disabled={busy} data-testid="create-room">
          {busy ? 'Creating...' : 'Create Room'}
        </button>

        {error !== null && <p className="landing-error">{error}</p>}

        <form className="landing-join" onSubmit={join}>
          <label htmlFor="code">or join with a code</label>
          <div className="landing-join-row">
            <input
              id="code"
              inputMode="numeric"
              pattern="\d{4}"
              maxLength={4}
              placeholder="4827"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
            />
            <button type="submit" disabled={!/^\d{4}$/.test(code)}>
              Join
            </button>
          </div>
        </form>
      </div>

      <footer className="landing-footer">
        Rooms are temporary and may be lost if the server restarts.
      </footer>
    </main>
  );
}
