import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

export function Landing(): JSX.Element {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState('');

  // The backend's build, fetched once. `web` updates itself when a new image
  // is published; this service is promoted by hand, because restarting it
  // destroys every live room. So the two drifting apart is normal rather than
  // exceptional, and worth being able to see rather than infer.
  const [apiBuild, setApiBuild] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/version')
      .then((r) => (r.ok ? r.json() : null))
      .then((body: { build?: string } | null) => {
        if (!cancelled && body?.build) setApiBuild(body.build);
      })
      // A version stamp is not worth an error state on the landing page. If
      // the request fails the footer simply shows the web build alone.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  // Compare in full, show the first seven. CI stamps the whole commit sha, so
  // /api/version stays precise enough to name the exact :sha- image tag, while
  // the footer stays readable.
  const short = (b: string): string => (b.length > 12 ? b.slice(0, 7) : b);
  const drifted = apiBuild !== null && apiBuild !== __BUILD_ID__;

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
        <span className="landing-build" data-testid="build-stamp">
          web <code>{short(__BUILD_ID__)}</code>
          {apiBuild !== null && (
            <>
              {' · '}api <code>{short(apiBuild)}</code>
              {drifted && (
                <span className="landing-build-drift" title="The frontend and backend are from different builds. The backend does not update itself; promote it deliberately.">
                  {' '}⚠
                </span>
              )}
            </>
          )}
        </span>
      </footer>
    </main>
  );
}
