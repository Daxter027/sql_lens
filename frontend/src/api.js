/**
 * api.js
 * ------
 * All API calls to the FastAPI backend.
 * The session token is held in module-level state — NEVER stored in
 * localStorage or cookies (would persist across sessions).
 */

// Relative by default: requests go to the same host/port that served the page
// (e.g. http://<lan-ip>:5173/api), which Vite proxies to the backend on
// localhost:8000. This makes the app work over LAN with no CORS and only port
// 5173 exposed. Override with VITE_API_BASE to point at a separate backend host.
const BASE = import.meta.env.VITE_API_BASE || '/api'

let _sessionToken = null

export const setSessionToken = (token) => { _sessionToken = token }
export const getSessionToken = () => _sessionToken
export const clearSessionToken = () => { _sessionToken = null }

const headers = (extra = {}) => ({
  'Content-Type': 'application/json',
  ...((_sessionToken) ? { 'X-Session-Token': _sessionToken } : {}),
  ...extra,
})

async function handleResponse(res) {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch {
      // Non-JSON body (e.g. nginx 502, plain-text error)
      try {
        const text = await res.text()
        detail = text.trim() || detail
      } catch { /* ignore */ }
    }
    throw new Error(detail)
  }
  return res.json()
}

/** POST /api/connect — returns { session_token, server, database } */
export async function apiConnect({ server, database, auth_type, username, password, trust_server_certificate }) {
  let res
  try {
    res = await fetch(`${BASE}/connect`, {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ server, database, auth_type, username, password, trust_server_certificate }),
    })
  } catch (networkErr) {
    // fetch() itself threw — server not reachable, CORS rejected, etc.
    throw new Error(
      `Cannot reach the backend API (${networkErr.message}). ` +
      'Make sure the backend is running on port 8000.'
    )
  }
  return handleResponse(res)
}

/** GET /api/databases — list databases on the connected server (reuses the
 *  server-side session credentials; client sends nothing sensitive). */
export async function apiListDatabases() {
  const res = await fetch(`${BASE}/databases`, { headers: headers() })
  return handleResponse(res)   // { server, current, databases: [...] }
}

/** POST /api/switch-database?database=X — re-point the session at another DB on
 *  the same server without re-entering credentials. */
export async function apiSwitchDatabase(database) {
  const res = await fetch(`${BASE}/switch-database?database=${encodeURIComponent(database)}`, {
    method: 'POST',
    headers: headers(),
  })
  return handleResponse(res)   // { server, database }
}

/** DELETE /api/disconnect */
export async function apiDisconnect() {
  if (!_sessionToken) return
  await fetch(`${BASE}/disconnect?session_token=${_sessionToken}`, {
    method: 'DELETE',
    headers: headers(),
  }).catch(() => {})
  clearSessionToken()
}

/** POST /api/analyze — returns AnalyzeResponse. Pass a list of issue ids to
 *  run a subset (sent as ?checks=a,b,c); omit to run all checks. */
export async function apiAnalyze(checks = null) {
  const qs = Array.isArray(checks) && checks.length
    ? `?checks=${encodeURIComponent(checks.join(','))}`
    : ''
  const res = await fetch(`${BASE}/analyze${qs}`, {
    method: 'POST',
    headers: headers(),
  })
  return handleResponse(res)
}

/** POST /api/execute — returns ExecuteResponse */
export async function apiExecute(issue_id, recovery_choice = null, targetParams = {}) {
  const res = await fetch(`${BASE}/execute`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({
      session_token: _sessionToken,
      issue_id,
      recovery_choice,
      ...targetParams,   // target_schema, target_table, target_column for heap_clustering
    }),
  })
  return handleResponse(res)
}

/** POST /api/storage-redundancy — runs the single backend analysis (SQL + local
 *  Ollama model) and returns the combined result. Long-running on CPU-only
 *  hardware (minutes); fetch has no client timeout so it waits for the server. */
export async function apiStorageRedundancy(model = null) {
  const qs = model ? `?model=${encodeURIComponent(model)}` : ''
  const res = await fetch(`${BASE}/storage-redundancy${qs}`, {
    method: 'POST',
    headers: headers(),
  })
  return handleResponse(res)
}

/** POST /api/table-intelligence — per-table profile for every user table
 *  (native metadata + optional SSRS report-usage). Read-only. */
export async function apiTableIntelligence(includeSsrs = true) {
  const qs = includeSsrs ? '' : '?include_ssrs=false'
  const res = await fetch(`${BASE}/table-intelligence${qs}`, {
    method: 'POST',
    headers: headers(),
  })
  return handleResponse(res)
}

/** POST /api/data-compression — estimate ROW/PAGE compression savings for the
 *  top-N largest tables (heavy: samples data into tempdb). Read-only. */
export async function apiDataCompression(topN = 25, mode = 'PAGE') {
  const res = await fetch(`${BASE}/data-compression?top_n=${topN}&mode=${encodeURIComponent(mode)}`, {
    method: 'POST',
    headers: headers(),
  })
  return handleResponse(res)
}

/** GET /api/reclaim-progress — live telemetry for an in-flight data-file reclaim */
export async function apiReclaimProgress() {
  const res = await fetch(`${BASE}/reclaim-progress`, { headers: headers() })
  return handleResponse(res)
}

/** GET /api/report */
export async function apiReport() {
  const res = await fetch(`${BASE}/report`, {
    method: 'GET',
    headers: headers(),
  })
  return handleResponse(res)
}
