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

export default defineConfig({
  plugins: [react()],
  server: {
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
