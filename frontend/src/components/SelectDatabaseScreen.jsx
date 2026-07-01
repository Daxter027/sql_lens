import { useState, useEffect } from 'react'
import { apiListDatabases, apiSwitchDatabase } from '../api.js'

/**
 * SelectDatabaseScreen
 * Shown right after connecting to a server. Lists the databases the login can
 * open and lets the user pick one; picking switches the session to that database
 * (via /api/switch-database) and proceeds to analysis.
 */
export default function SelectDatabaseScreen({ session, onSelected, onDisconnect }) {
  const [databases, setDatabases] = useState(null)   // null = still loading
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')
  const [selecting, setSelecting] = useState(null)   // database currently being switched to

  useEffect(() => {
    let active = true
    apiListDatabases()
      .then(res => { if (active) setDatabases(res.databases || []) })
      .catch(e => { if (active) { setError(e.message); setDatabases([]) } })
    return () => { active = false }
  }, [])

  const choose = async (db) => {
    setSelecting(db); setError(null)
    try {
      await apiSwitchDatabase(db)
      onSelected(db)
    } catch (e) {
      setError(e.message); setSelecting(null)
    }
  }

  const q = query.trim().toLowerCase()
  const filtered = (databases || []).filter(d => !q || d.toLowerCase().includes(q))

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', paddingTop: '3rem' }}>
      <div style={{ textAlign: 'center', marginBottom: '2rem' }}>
        <h1 style={{ marginBottom: '0.5rem' }}><span className="text-gradient">Select a database</span></h1>
        <p style={{ color: 'var(--text-secondary)' }}>
          Connected to <strong>{session?.server}</strong> — choose a database to analyze.
        </p>
      </div>

      <div className="glass-card" style={{ width: '100%', maxWidth: '520px', padding: '1.5rem' }}>
        <input
          className="form-input"
          placeholder="Filter databases…"
          value={query}
          onChange={e => setQuery(e.target.value)}
          style={{ marginBottom: '1rem' }}
          autoFocus
        />

        {databases === null ? (
          <p style={{ color: 'var(--text-muted)', textAlign: 'center', padding: '1rem' }}>
            <span className="spinner" style={{ width: 16, height: 16, display: 'inline-block', verticalAlign: 'middle', marginRight: 8 }} />
            Loading databases…
          </p>
        ) : (error && databases.length === 0) ? (
          <div className="toast toast-error"><span>⚠️</span><span>{error}</span></div>
        ) : filtered.length === 0 ? (
          <p style={{ color: 'var(--text-muted)', textAlign: 'center', padding: '1rem' }}>
            {databases.length === 0 ? 'No databases are accessible to this login.' : `No databases match “${query}”.`}
          </p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', maxHeight: '46vh', overflowY: 'auto' }}>
            {filtered.map(db => (
              <button
                key={db}
                type="button"
                disabled={!!selecting}
                onClick={() => choose(db)}
                style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '0.6rem 0.9rem', borderRadius: 'var(--radius-sm)',
                  border: '1px solid var(--border-subtle)', background: 'var(--bg-glass)',
                  color: 'var(--text-primary)', cursor: selecting ? 'wait' : 'pointer',
                  textAlign: 'left', fontSize: '0.9rem',
                }}
              >
                <span>🗄️ {db}</span>
                {selecting === db
                  ? <span className="spinner" style={{ width: 14, height: 14 }} />
                  : <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>Analyze →</span>}
              </button>
            ))}
          </div>
        )}

        {error && databases && databases.length > 0 && (
          <div className="toast toast-error" style={{ marginTop: '0.75rem' }}><span>⚠️</span><span>{error}</span></div>
        )}

        <div style={{ marginTop: '1rem', textAlign: 'center' }}>
          <button className="btn btn-ghost" style={{ fontSize: '0.8rem' }} onClick={onDisconnect}>← Disconnect</button>
        </div>
      </div>
    </div>
  )
}
