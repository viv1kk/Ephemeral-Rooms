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
// The CSP here is deliberately looser than production's in exactly one place:
// Vite's React Fast Refresh injects an inline module preamble into the served
// HTML, so dev needs 'unsafe-inline' for scripts. The production build has no
// inline script at all and does not. Do not copy this policy into production -
// scanning a tunnelled dev server measures this, not what you ship.
const DEV_SECURITY_HEADERS = {
  'Content-Security-Policy': [
    "default-src 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "script-src 'self' 'unsafe-inline'",
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
  build: { outDir: 'dist', sourcemap: true },
});
