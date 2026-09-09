/**
 * The room's Share control: a button that opens a small panel holding a QR
 * code for the room URL and a button to copy it.
 *
 * The QR encoder is loaded on first open rather than up front. Nobody who
 * never shares a room should pay for it in the initial bundle, which is the
 * same reason the editor's language modes are lazy-loaded.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

type Matrix = boolean[][];

export function ShareMenu({ url }: { url: string }): JSX.Element {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [matrix, setMatrix] = useState<Matrix | null>(null);
  const [qrFailed, setQrFailed] = useState(false);

  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const copyButton = useRef<HTMLButtonElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    setCopyFailed(false);
  }, []);

  // Encode on first open, and again if the room changes underneath us.
  useEffect(() => {
    setMatrix(null);
    setQrFailed(false);
  }, [url]);

  useEffect(() => {
    if (!open || matrix !== null || qrFailed) return undefined;
    let cancelled = false;
    void import('qr')
      .then(({ default: encodeQR }) => {
        if (cancelled) return;
        // A four-module quiet zone is what the QR standard requires for a
        // reliable scan; the library defaults to two.
        setMatrix(encodeQR(url, 'raw', { ecc: 'medium', border: 4 }));
      })
      .catch(() => {
        if (!cancelled) setQrFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [open, matrix, qrFailed, url]);

  // Dismiss on a click elsewhere or on Escape, and hand focus back to the
  // button so keyboard users are not dropped at the top of the document.
  useEffect(() => {
    if (!open) return undefined;

    const onPointerDown = (event: PointerEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        close();
        trigger.current?.focus();
      }
    };

    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, close]);

  // Move focus into the panel on open so the copy action is one key away.
  useEffect(() => {
    if (open) copyButton.current?.focus();
  }, [open]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setCopyFailed(false);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // The Clipboard API needs a secure context, so this fails on a plain
      // http origin that is not localhost. Say so rather than doing nothing:
      // the link is on screen and can be copied by hand.
      setCopyFailed(true);
    }
  };

  return (
    <div className="share" ref={root}>
      <button
        ref={trigger}
        onClick={() => (open ? close() : setOpen(true))}
        aria-haspopup="dialog"
        aria-expanded={open}
        data-testid="share-button"
      >
        Share
      </button>

      {open && (
        <div className="share-panel" role="dialog" aria-label="Share this room" data-testid="share-panel">
          <div className="share-qr">
            {matrix !== null ? (
              <QrCode matrix={matrix} />
            ) : qrFailed ? (
              <p className="share-qr-fallback">The QR code could not be generated.</p>
            ) : (
              <p className="share-qr-fallback">Generating...</p>
            )}
          </div>

          <p className="share-url" data-testid="share-url">
            {url}
          </p>

          <button
            ref={copyButton}
            className="primary share-copy"
            onClick={copy}
            data-testid="share-copy"
          >
            {copied ? 'Copied' : 'Copy link'}
          </button>

          {copyFailed && (
            <p className="share-copy-error">Copying is blocked here — select the link above.</p>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The matrix as an SVG.
 *
 * Drawn as one path rather than a rect per module: a version-3 symbol has over
 * a thousand modules, and one node scales better than a thousand.
 *
 * Deliberately black on white regardless of theme. Scanners expect dark
 * modules on a light ground, and an inverted code is rejected by many of them,
 * so this is one of the few places the dark palette does not apply.
 */
function QrCode({ matrix }: { matrix: Matrix }): JSX.Element {
  const size = matrix.length;
  let path = '';
  for (let row = 0; row < size; row += 1) {
    const cells = matrix[row]!;
    for (let col = 0; col < size; col += 1) {
      if (cells[col]) path += `M${col} ${row}h1v1h-1z`;
    }
  }

  return (
    <svg
      viewBox={`0 0 ${size} ${size}`}
      className="qr"
      role="img"
      aria-label="QR code linking to this room"
      shapeRendering="crispEdges"
      data-testid="share-qr"
    >
      <rect width={size} height={size} fill="#ffffff" />
      <path d={path} fill="#0f1115" />
    </svg>
  );
}
