import { randomBytes } from 'node:crypto';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The dev server proxies API and WebSocket traffic to the Python backend so the
// browser only ever talks to one origin, exactly as Nginx does in production.
const BACKEND = process.env.VITE_BACKEND_ORIGIN ?? 'http://127.0.0.1:8000';

// Hostnames the dev server will answer to, beyond localhost.
//
// Vite rejects requests whose Host header it does not recognise. That is a DNS
// rebinding protection: without it, a page on another origin could resolve its
// own domain to 127.0.0.1 and read from your dev server. It matters here
// because this server proxies /api and /ws straight through to the backend.
//
// Tunnels present their own hostname, and trycloudflare mints a fresh random
// one on every restart, so naming a single host would break tomorrow. The
// leading dot allows a domain and all of its subdomains.
//
// Development only. `vite build` ignores the whole `server` block, and in
// production Nginx serves the built assets and Vite is not running at all.
const TUNNEL_HOSTS = [
  '.trycloudflare.com',
  '.ngrok-free.app',
  '.ngrok.io',
  '.loca.lt',
  '.vikk.space'
];

// Security headers for the dev server.
//
// Production gets these from Nginx and from the application itself
// (backend/app/security.py); this exists because the dev server is sometimes
// exposed through a tunnel, and a page reachable on the public internet should
// not be missing them just because it happens to be a dev build.
//
// Vite's React Fast Refresh injects an inline module preamble into the served
// HTML, which would ordinarily force 'unsafe-inline' into script-src. Vite can
// stamp a nonce onto what it injects instead, so dev keeps a script-src that
// actually restricts something.
//
// Generated per process start and never committed, so it is not guessable from
// the repository. This is still a development server: it is not what should be
// exposed publicly or measured. To check the real headers, build and serve the
// bundle from the backend instead - see docs/aws-setup.md.
const DEV_NONCE = randomBytes(18).toString('base64');

const DEV_SECURITY_HEADERS = {
  'Content-Security-Policy': [
    "default-src 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    `script-src 'self' 'nonce-${DEV_NONCE}'`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self' ws: wss:",
  ].join('; '),
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Resource-Policy': 'same-origin',
};

export default defineConfig({
  plugins: [react()],
  html: { cspNonce: DEV_NONCE },
  server: {
    headers: DEV_SECURITY_HEADERS,
    // Loopback only. A tunnel client runs on this machine and dials in
    // locally, so it does not need the server exposed on the LAN.
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    allowedHosts: [
      ...TUNNEL_HOSTS,
      // Escape hatch for any other host: VITE_ALLOWED_HOST=foo.example.com
      ...(process.env.VITE_ALLOWED_HOST ? [process.env.VITE_ALLOWED_HOST] : []),
    ],
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/ws': { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
  // The commit this bundle was built from, replaced at build time so nothing
  // is looked up at runtime. VITE_BUILD_ID is set by frontend/Dockerfile from a
  // build argument; a plain `npm run build` has none, and 'dev' is the honest
  // answer for a bundle that did not come from CI.
  //
  // Note this makes the image content depend on the commit, so every build
  // produces a new digest even when nothing under src/ changed. That is the
  // point - a deployed bundle should be traceable to a commit - but it does
  // mean any push to main republishes the web image.
  define: {
    __BUILD_ID__: JSON.stringify(process.env.VITE_BUILD_ID ?? 'dev'),
  },
  build: { outDir: 'dist', sourcemap: true },
});
