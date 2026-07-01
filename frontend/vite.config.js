import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `host: true` binds the dev/preview server to all interfaces (0.0.0.0) so
// other machines on the LAN can reach it at http://<this-host-ip>:5173.
// `/api` is proxied to the backend on localhost:8000, so the backend itself
// never needs to be exposed to the network.
const proxy = {
  '/api': {
    // Use 127.0.0.1, NOT localhost: Node resolves "localhost" to IPv6 (::1)
    // first on Windows, but uvicorn binds IPv4 (127.0.0.1) — the mismatch makes
    // the proxy fail with a connection-refused 500. Forcing IPv4 avoids it.
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
  },
}

export default defineConfig({
  plugins: [react()],
  server: { host: true, port: 5173, proxy },
  preview: { host: true, port: 4173, proxy },
})
