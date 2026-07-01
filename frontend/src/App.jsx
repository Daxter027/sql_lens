import { useState, useEffect } from 'react'
import ConnectionScreen from './components/ConnectionScreen.jsx'
import AnalysisScreen from './components/AnalysisScreen.jsx'
import ExecutionScreen from './components/ExecutionScreen.jsx'
import ReportScreen from './components/ReportScreen.jsx'
import { clearPanelCaches } from './components/IssueCard.jsx'
import { apiListDatabases, apiSwitchDatabase } from './api.js'

/**
 * App.jsx — top-level screen router.
 * State machine: connect → analyze → execute → report
 * Screens are rendered in order; no URL routing needed.
 */
export default function App() {
  const [screen, setScreen] = useState('connect')   // connect | analyze | execute | report
  const [session, setSession] = useState(null)       // { token, server, database }
  const [analysisData, setAnalysisData] = useState(null)
  const [executionData, setExecutionData] = useState(null)
  // Issue ids remediated this session — accumulated across optimization rounds so
  // returning to the analysis screen reflects what was already fixed.
  const [executedIds, setExecutedIds] = useState(() => new Set())
  const [databases, setDatabases] = useState([])     // databases on the connected server
  const [switching, setSwitching] = useState(false)
  const [switchError, setSwitchError] = useState(null)

  const handleConnected = (sessionInfo) => {
    // New DB session — drop any cached on-demand panel results from a prior one.
    clearPanelCaches()
    setSession(sessionInfo)
    setScreen('analyze')
  }

  // Load the server's database list once per session (list is server-wide and
  // constant, so it does not refetch on a DB switch — the token stays the same).
  useEffect(() => {
    if (!session?.token) { setDatabases([]); return }
    let active = true
    apiListDatabases()
      .then(res => { if (active) setDatabases(res.databases || []) })
      .catch(() => { if (active) setDatabases([]) })
    return () => { active = false }
  }, [session?.token])

  // Switch to another DB on the same server WITHOUT re-entering credentials.
  const handleSwitchDatabase = async (database) => {
    if (!session || switching || database === session.database) return
    setSwitching(true)
    setSwitchError(null)
    try {
      await apiSwitchDatabase(database)
      clearPanelCaches()
      setAnalysisData(null)
      setExecutionData(null)
      setExecutedIds(new Set())
      setSession(s => ({ ...s, database }))
      setScreen('analyze')       // AnalysisScreen remounts (keyed on db) → re-analyses
    } catch (e) {
      setSwitchError(e.message || 'Could not switch database.')
    } finally {
      setSwitching(false)
    }
  }

  const handleAnalyzed = (data) => {
    setAnalysisData(data)
  }

  const handleExecute = (data) => {
    // Remember which issues actually ran (success/partial) so they show as
    // remediated and aren't offered again when the user comes back.
    const ran = (data || [])
      .filter(r => r && (r.status === 'success' || r.status === 'partial'))
      .map(r => r.issue_id)
    if (ran.length) {
      setExecutedIds(prev => {
        const next = new Set(prev)
        ran.forEach(id => next.add(id))
        return next
      })
    }
    setExecutionData(data)
    setScreen('execute')
  }

  const handleExecutionDone = () => {
    setScreen('report')
  }

  // Return from the report to the analysis screen WITHOUT a new session or a
  // re-analysis — cached results stay, remediated issues are marked.
  const handleBackToAnalysis = () => {
    setScreen('analyze')
  }

  const handleDisconnect = () => {
    clearPanelCaches()
    setSession(null)
    setAnalysisData(null)
    setExecutionData(null)
    setExecutedIds(new Set())
    setScreen('connect')
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-brand">
          <div className="brand-icon">🗄️</div>
          <span>SQL Storage Optimizer</span>
        </div>
        {session && (
          <div className="topbar-meta">
            <span className="db-badge" title="Connected server">🖥️ {session.server}</span>
            {/* Database switcher — change DB on the same server, no re-login. */}
            <label title="Switch database on this server"
              style={{ display: 'flex', alignItems: 'center', gap: '0.35rem' }}>
              <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>DB</span>
              <select
                className="form-input"
                value={session.database}
                disabled={switching || databases.length === 0}
                onChange={e => handleSwitchDatabase(e.target.value)}
                style={{ width: 'auto', padding: '3px 8px', fontSize: '0.78rem' }}
              >
                {/* Ensure the current DB is always selectable even if the list hasn't loaded. */}
                {!databases.includes(session.database) && (
                  <option value={session.database}>{session.database}</option>
                )}
                {databases.map(db => <option key={db} value={db}>{db}</option>)}
              </select>
              {switching && <span className="spinner" style={{ width: 12, height: 12 }} />}
            </label>
            {switchError && (
              <span style={{ fontSize: '0.72rem', color: 'var(--error)' }} title={switchError}>⚠ switch failed</span>
            )}
            <button
              className="btn btn-ghost"
              style={{ padding: '4px 12px', fontSize: '0.75rem' }}
              onClick={handleDisconnect}
            >
              Disconnect
            </button>
          </div>
        )}
      </header>

      <main className="page">
        {screen === 'connect' && (
          <ConnectionScreen onConnected={handleConnected} />
        )}
        {screen === 'analyze' && (
          <AnalysisScreen
            key={`${session?.token}:${session?.database}`}   // remount + re-analyse on DB switch
            session={session}
            analysisData={analysisData}
            executedIds={executedIds}
            onAnalyzed={handleAnalyzed}
            onExecute={handleExecute}
          />
        )}
        {screen === 'execute' && (
          <ExecutionScreen
            session={session}
            executionData={executionData}
            onDone={handleExecutionDone}
          />
        )}
        {screen === 'report' && (
          <ReportScreen
            session={session}
            onBack={handleBackToAnalysis}
            onDisconnect={handleDisconnect}
          />
        )}
      </main>
    </div>
  )
}
