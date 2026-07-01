import { useState } from 'react'
import { apiConnect, setSessionToken } from '../api.js'

export default function ConnectionScreen({ onConnected }) {
  // No database here — we connect to the server (login's default DB) and pick the
  // specific database on the next screen.
  const [form, setForm] = useState({
    server: '',
    auth_type: 'windows',
    username: '',
    password: '',
    trust_server_certificate: false,
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const set = (key, val) => setForm(f => ({ ...f, [key]: val }))

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      const res = await apiConnect(form)
      setSessionToken(res.session_token)
      onConnected({ token: res.session_token, server: res.server, database: res.database })
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', paddingTop: '3rem' }}>
      {/* Hero */}
      <div style={{ textAlign: 'center', marginBottom: '2.5rem' }}>
        <div style={{ fontSize: '3rem', marginBottom: '1rem' }}>🗄️</div>
        <h1 style={{ marginBottom: '0.5rem' }}>
          <span className="text-gradient">SQL Storage Optimizer</span>
        </h1>
        <p style={{ color: 'var(--text-secondary)', fontSize: '1rem', maxWidth: '440px' }}>
          Deep read-only analysis of five storage issues, with a targeted
          transaction log fix — all in one tool.
        </p>
      </div>

      {/* Connection form */}
      <div className="glass-card" style={{ width: '100%', maxWidth: '480px', padding: '2rem' }}>
        <h2 style={{ marginBottom: '1.5rem', fontSize: '1.1rem' }}>Connect to SQL Server</h2>

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>

          <div className="form-group">
            <label className="form-label">Server / Host</label>
            <input
              id="field-server"
              className="form-input"
              placeholder="localhost or 192.168.1.10:1433"
              value={form.server}
              onChange={e => set('server', e.target.value)}
              required
            />
          </div>

          <div className="form-group">
            <label className="form-label">Authentication</label>
            <div className="auth-toggle">
              <button
                type="button"
                id="auth-windows"
                className={form.auth_type === 'windows' ? 'active' : ''}
                onClick={() => set('auth_type', 'windows')}
              >
                🪟 Windows / AD Auth
              </button>
              <button
                type="button"
                id="auth-sql"
                className={form.auth_type === 'sql' ? 'active' : ''}
                onClick={() => set('auth_type', 'sql')}
              >
                🔑 SQL Auth
              </button>
            </div>
          </div>

          {form.auth_type === 'sql' && (
            <>
              <div className="form-group">
                <label className="form-label">Username</label>
                <input
                  id="field-username"
                  name="username"
                  className="form-input"
                  placeholder="sa"
                  value={form.username}
                  onChange={e => set('username', e.target.value)}
                  autoComplete="username"
                />
              </div>
              <div className="form-group">
                <label className="form-label">Password</label>
                <input
                  id="field-password"
                  name="password"
                  className="form-input"
                  type="password"
                  placeholder="••••••••"
                  value={form.password}
                  onChange={e => set('password', e.target.value)}
                  autoComplete="current-password"
                />
              </div>
            </>
          )}

          <label className="checkbox-row">
            <input
              id="field-trust-cert"
              type="checkbox"
              checked={form.trust_server_certificate}
              onChange={e => set('trust_server_certificate', e.target.checked)}
            />
            <span>Trust Server Certificate (self-signed)</span>
          </label>

          {error && (
            <div className="toast toast-error" id="connect-error">
              <span>⚠️</span>
              <span>{error}</span>
            </div>
          )}

          <button
            id="btn-connect"
            type="submit"
            className="btn btn-primary btn-lg"
            disabled={loading}
            style={{ marginTop: '0.5rem' }}
          >
            {loading ? <><div className="spinner" style={{ width: 16, height: 16 }} /> Connecting…</> : 'Connect →'}
          </button>
        </form>
      </div>

      {/* Footer note */}
      <p style={{ marginTop: '1.5rem', fontSize: '0.75rem', color: 'var(--text-muted)', textAlign: 'center' }}>
        Credentials are held in server memory only for the session lifetime.<br />
        They are never written to disk or returned to this browser.
      </p>
    </div>
  )
}
