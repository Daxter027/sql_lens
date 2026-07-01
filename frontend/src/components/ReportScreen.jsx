import { useState, useEffect } from 'react'
import { apiReport } from '../api.js'

const ISSUE_NAMES = {
  transaction_log_growth:     'Transaction Log Growth',
  heap_clustering:            'Clustered Index Conversion',
  string_storage:             'String Storage Optimization',
  unused_indexes:             'Unused Index Audit',
  ghost_pages:                'Ghost Page Reconciliation',
  index_fragmentation:        'Fragmented Index Rebuild',
  blank_string_contamination: 'Blank-String Contamination',
  shadow_tables:              'Shadow / Twin Tables',
  inappropriate_datatypes:    'Inappropriate Datatypes',
  archival_candidates:        'Legacy Table Archival Candidates',
  data_file_reclaim:          'Data File Space Reclamation',
}

const nameFor = (id) => ISSUE_NAMES[id] || id

const statusColor = (s) =>
  s === 'success' ? 'var(--success)' : s === 'failed' ? 'var(--error)' : 'var(--warning)'
const statusIcon = (s) =>
  s === 'success' ? '✅' : s === 'failed' ? '❌' : '⏭️'

function Section({ title, children }) {
  return (
    <div className="glass-card" style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
      <h3 style={{ borderBottom: '1px solid var(--border-subtle)', paddingBottom: '0.75rem' }}>{title}</h3>
      {children}
    </div>
  )
}

/* Before/after table for the transaction-log execution (keys match tl.execute). */
function LogBeforeAfter({ before, after }) {
  const rows = [
    ['Log Size (MB)', 'log_size_mb',  v => v.toLocaleString()],
    ['Used (MB)',     'log_used_mb',  v => v.toLocaleString()],
    ['Used %',        'log_used_pct', v => `${v.toFixed(2)}%`],
    ['VLF Count',     'vlf_count',    v => v.toLocaleString()],
  ]
  return (
    <table className="data-table">
      <thead><tr><th>Metric</th><th>Before</th><th>After</th><th>Delta</th></tr></thead>
      <tbody>
        {rows.map(([label, key, fmt]) => {
          const bv = before?.[key], av = after?.[key]
          const delta = av != null && bv != null ? av - bv : null
          const better = delta != null && delta < 0
          return (
            <tr key={key}>
              <td style={{ color: 'var(--text-secondary)' }}>{label}</td>
              <td>{bv != null ? fmt(bv) : '—'}</td>
              <td>{av != null ? fmt(av) : '—'}</td>
              <td style={{ color: better ? 'var(--success)' : delta > 0 ? 'var(--error)' : 'var(--text-muted)' }}>
                {delta != null ? `${delta > 0 ? '+' : ''}${fmt(delta)}` : '—'}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

/* One executed optimization. */
function ExecutionBlock({ exec }) {
  const isTL = exec.issue_id === 'transaction_log_growth'
  const multi = Array.isArray(exec.results) && exec.results.length > 0
  return (
    <Section title={`🔧 ${nameFor(exec.issue_id)}`}>
      <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', alignItems: 'center' }}>
        <span style={{ fontWeight: 700, color: statusColor(exec.status), fontSize: '1rem' }}>
          {statusIcon(exec.status)} {exec.status?.toUpperCase()}
        </span>
        <span style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>{exec.message}</span>
      </div>

      {exec.command_executed && !multi && (
        <div>
          <div className="label" style={{ marginBottom: '0.4rem' }}>Command Executed</div>
          <code className="code-block" style={{ fontSize: '0.78rem' }}>{exec.command_executed}</code>
        </div>
      )}

      {isTL && exec.before_metrics && exec.after_metrics && (
        <div>
          <div className="label" style={{ marginBottom: '0.5rem' }}>Before / After</div>
          <LogBeforeAfter before={exec.before_metrics} after={exec.after_metrics} />
          {exec.delta_mb_freed > 0 && (
            <div style={{
              marginTop: '0.75rem', padding: '0.6rem 1rem',
              background: 'var(--success-bg)', border: '1px solid rgba(5,150,105,0.25)',
              borderRadius: 'var(--radius-sm)', color: 'var(--success)', fontWeight: 700,
            }}>
              🎉 Total freed: {exec.delta_mb_freed.toLocaleString()} MB
            </div>
          )}
        </div>
      )}

      {multi && (
        <div>
          <div className="label" style={{ marginBottom: '0.5rem' }}>
            Processed Objects ({exec.results.length})
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
            {exec.results.map((r, i) => (
              <div key={i} style={{
                display: 'flex', justifyContent: 'space-between', gap: '0.75rem',
                padding: '0.5rem 0.75rem', background: 'var(--bg-glass)',
                border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm)',
                fontSize: '0.8rem',
              }}>
                <span style={{ fontFamily: 'JetBrains Mono, monospace', color: 'var(--text-primary)' }}>
                  [{r.schema ?? r.target_schema}].[{r.table ?? r.target_table}]{r.index ? ` · ${r.index}` : ''}
                </span>
                <span style={{ color: statusColor(r.status), whiteSpace: 'nowrap' }}>
                  {statusIcon(r.status)} {r.status}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
        Executed at: {exec.executed_at ? new Date(exec.executed_at).toLocaleString() : '—'}
      </p>
    </Section>
  )
}

export default function ReportScreen({ session, onBack, onDisconnect }) {
  const [report, setReport] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    apiReport()
      .then(setReport)
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  const exportText = () => {
    if (!report) return
    const lines = []
    lines.push('='.repeat(60))
    lines.push('SQL SERVER STORAGE OPTIMIZATION REPORT')
    lines.push('='.repeat(60))
    lines.push(`Database : ${report.database}`)
    lines.push(`Server   : ${report.server}`)
    lines.push(`Generated: ${report.generated_at}`)
    lines.push('')

    const execs = report.executions || (report.execution ? [report.execution] : [])
    if (execs.length) {
      lines.push('── EXECUTION RESULTS ────────────────────────────────')
      for (const ex of execs) {
        lines.push('')
        lines.push(`Issue   : ${nameFor(ex.issue_id)}`)
        lines.push(`Status  : ${ex.status}`)
        lines.push(`Message : ${ex.message}`)
        if (ex.command_executed) lines.push(`Command : ${ex.command_executed}`)
        if (ex.delta_mb_freed)   lines.push(`Freed   : ${ex.delta_mb_freed} MB`)
        if (Array.isArray(ex.results)) {
          for (const r of ex.results) {
            lines.push(`  - [${r.schema ?? r.target_schema}].[${r.table ?? r.target_table}]` +
              `${r.index ? ` · ${r.index}` : ''}: ${r.status} — ${r.message}`)
          }
        }
      }
      lines.push('')
    }

    lines.push('── ALL 5 CHECKS ─────────────────────────────────────')
    const issues = report.analysis?.issues || []
    for (const iss of issues) {
      lines.push('')
      lines.push(`[${iss.severity?.toUpperCase()}] ${iss.issue_name}`)
      lines.push(`Recommendation: ${iss.recommended_action}`)
      lines.push(`Impact: ${iss.estimated_impact}`)
    }

    const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `sql-storage-report-${report.database}-${new Date().toISOString().slice(0, 10)}.txt`
    a.click()
  }

  if (loading) return (
    <div style={{ textAlign: 'center', paddingTop: '4rem', color: 'var(--text-muted)' }}>
      <div className="spinner" style={{ margin: '0 auto 1rem', width: 32, height: 32 }} />
      Loading report…
    </div>
  )

  if (error) return (
    <div className="toast toast-error" style={{ maxWidth: 600, margin: '2rem auto' }}>
      ⚠️ {error}
    </div>
  )

  const execs = report?.executions || (report?.execution ? [report.execution] : [])
  const issues = report?.analysis?.issues || []
  // Issue ids that were executed successfully (or partially) this session.
  const remediated = new Set(
    execs.filter(e => e.status === 'success' || e.status === 'partial').map(e => e.issue_id)
  )

  return (
    <div id="report-root" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem', maxWidth: '900px', margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '1rem' }}>
        <div>
          <h1 style={{ fontSize: '1.6rem', marginBottom: '0.25rem' }}>
            Optimization <span className="text-gradient">Report</span>
          </h1>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.82rem' }}>
            {report?.database} · {report?.server} · Generated {new Date(report?.generated_at).toLocaleString()}
          </p>
        </div>
        <div style={{ display: 'flex', gap: '0.75rem' }}>
          {onBack && (
            <button id="btn-back-to-analysis" className="btn btn-ghost" onClick={onBack}>
              ← Back to Analysis
            </button>
          )}
          <button id="btn-export-text" className="btn btn-ghost" onClick={exportText}>
            ⬇ Export .txt
          </button>
          <button id="btn-print-pdf" className="btn btn-ghost" onClick={() => window.print()}>
            🖨 Print / PDF
          </button>
          <button id="btn-new-session" className="btn btn-primary" onClick={onDisconnect}>
            New Session
          </button>
        </div>
      </div>

      {/* Execution results — one block per optimization that ran */}
      {execs.length === 0 ? (
        <div className="toast toast-warning">
          ⏭️ No optimizations were executed this session — the analysis below is read-only.
        </div>
      ) : (
        execs.map((ex, i) => <ExecutionBlock key={i} exec={ex} />)
      )}

      {/* All 5 issues */}
      <Section title="📋 Complete Analysis — All 5 Checks">
        <p style={{ fontSize: '0.82rem', color: 'var(--text-muted)', marginBottom: '0.5rem' }}>
          All identified issues are listed below regardless of execution status,
          so the full picture of database health is preserved in this report.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
          {issues.map(iss => (
            <div key={iss.issue_id} style={{
              background: 'var(--bg-glass)',
              border: '1px solid var(--border-subtle)',
              borderRadius: 'var(--radius-sm)',
              padding: '1rem',
              display: 'flex',
              flexDirection: 'column',
              gap: '0.5rem',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
                <span className={`badge badge-${iss.severity?.toLowerCase() || 'none'}`}>
                  {iss.severity}
                </span>
                <strong style={{ fontSize: '0.9rem' }}>{iss.issue_name}</strong>
                {remediated.has(iss.issue_id) ? (
                  <span className="badge badge-low">✅ Remediated this session</span>
                ) : !iss.executable ? (
                  <span className="badge badge-none">Identified — manual remediation</span>
                ) : (
                  <span className="badge badge-info">Identified — fix available</span>
                )}
              </div>
              <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                {iss.recommended_action}
              </p>
              {iss.estimated_impact && iss.estimated_impact !== 'N/A' && (
                <p style={{ fontSize: '0.75rem', color: 'var(--severity-low)' }}>
                  💡 {iss.estimated_impact}
                </p>
              )}
            </div>
          ))}
        </div>
      </Section>

      {/* Print style */}
      <style>{`
        @media print {
          .topbar, button, .btn { display: none !important; }
          body { background: white !important; }
          .glass-card { border: 1px solid #d0d7e2 !important; box-shadow: none !important; }
        }
      `}</style>
    </div>
  )
}
