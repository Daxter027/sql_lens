import { useState, useEffect } from 'react'

const STEPS = [
  { id: 'precheck',  label: 'Pre-check' },
  { id: 'running',   label: 'Running' },
  { id: 'verifying', label: 'Verifying' },
  { id: 'done',      label: 'Done' },
]

const ISSUE_LABELS = {
  transaction_log_growth:     'Transaction Log Fix',
  heap_clustering:            'Clustered Index Conversion',
  unused_indexes:             'Unused Index Disable',
  ghost_pages:                'Ghost Page Reconciliation',
  blank_string_contamination: 'Blank-String Cleanup',
  shadow_tables:              'Shadow Table Quarantine',
}

const METRIC_LABELS = {
  storage_type:       'Storage Type',
  row_count:          'Row Count',
  index_name:         'Index Name',
  is_disabled:        'Disabled',
  ghost_record_count: 'Ghost Records',
  total_rows:         'Total Rows',
  blank_or_spaces:    'Blank/Spaces',
  name:               'Name',
  quarantined_at:     'Quarantined',
}

const labelFor = (id) => ISSUE_LABELS[id] || id

const fmtMetric = (v) => {
  if (v == null) return '—'
  if (typeof v === 'boolean') return v ? 'Yes' : 'No'
  if (typeof v === 'number') return v.toLocaleString()
  return v
}

// Determine overall batch status from the array of results
function overallStatus(results) {
  if (!results || results.length === 0) return 'skipped'
  if (results.every(r => r.status === 'success')) return 'success'
  if (results.every(r => r.status === 'failed'))  return 'failed'
  if (results.some(r => r.status === 'failed'))   return 'partial'
  if (results.some(r => r.status === 'success'))  return 'success'
  return 'skipped'
}

const statusPillColor = (s) =>
  s === 'success' ? 'var(--success)' : s === 'failed' ? 'var(--error)' : 'var(--text-muted)'

const statusPillIcon = (s) =>
  s === 'success' ? '✅' : s === 'failed' ? '❌' : '⏭️'

/** Generic before/after table for multi-object results (heap, index, ghost). */
function GenericBeforeAfter({ before, after }) {
  if (!before && !after) return null
  const keys = [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])]
  if (keys.length === 0) return null
  return (
    <table className="data-table" style={{ width: '100%', fontSize: '0.82rem', marginBottom: '0.5rem' }}>
      <thead><tr><th>Metric</th><th>Before</th><th>After</th></tr></thead>
      <tbody>
        {keys.map(k => (
          <tr key={k}>
            <td style={{ color: 'var(--text-secondary)' }}>{METRIC_LABELS[k] || k}</td>
            <td>{fmtMetric(before?.[k])}</td>
            <td>{fmtMetric(after?.[k])}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function MultiObjectResults({ results }) {
  return (
    <div>
      <h4 style={{ marginBottom: '0.75rem' }}>Processed Objects ({results.length})</h4>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
        {results.map((res, i) => {
          const schema = res.schema ?? res.target_schema
          const table = res.table ?? res.target_table
          const title = res.index
            ? `[${schema}].[${table}] · ${res.index}`
            : res.column
              ? `[${schema}].[${table}].[${res.column}]`
              : `[${schema}].[${table}]`
          return (
            <div key={i} style={{
              background: 'var(--bg-glass)',
              border: `1px solid ${res.status === 'success' ? 'rgba(5,150,105,0.3)' : res.status === 'failed' ? 'rgba(220,38,38,0.3)' : 'var(--border-subtle)'}`,
              borderRadius: 'var(--radius-sm)', padding: '1rem',
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '0.75rem', marginBottom: '0.4rem' }}>
                <strong style={{ color: 'var(--accent)', fontFamily: 'JetBrains Mono, monospace', fontSize: '0.85rem' }}>
                  {title}
                </strong>
                <span style={{ color: statusPillColor(res.status), fontSize: '0.85rem', whiteSpace: 'nowrap' }}>
                  {statusPillIcon(res.status)} {res.status}
                </span>
              </div>
              <div style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginBottom: '0.5rem' }}>{res.message}</div>
              <GenericBeforeAfter before={res.before_metrics} after={res.after_metrics} />
              {res.command_executed && (
                <code className="code-block" style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                  {res.command_executed}
                </code>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function IssueResultCard({ result }) {
  const isTL  = result.issue_id === 'transaction_log_growth'
  const bm    = result.before_metrics
  const am    = result.after_metrics
  const hasMultiResults = Array.isArray(result.results) && result.results.length > 0

  return (
    <div className="glass-card" style={{ padding: '1.5rem' }}>
      {/* Card header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
        <h3 style={{ margin: 0 }}>{labelFor(result.issue_id)}</h3>
        <span style={{ color: statusPillColor(result.status), fontWeight: 700 }}>
          {statusPillIcon(result.status)} {result.status}
        </span>
      </div>

      {/* Status message */}
      <div style={{ fontSize: '0.875rem', color: statusPillColor(result.status), marginBottom: '1rem' }}>
        {result.message}
      </div>

      {/* Single command (e.g. transaction log) */}
      {result.command_executed && !hasMultiResults && (
        <div style={{ marginBottom: '1rem' }}>
          <div className="label" style={{ marginBottom: '0.4rem' }}>Command Executed</div>
          <code className="code-block">{result.command_executed}</code>
        </div>
      )}

      {/* Transaction log: dedicated before/after table */}
      {isTL && bm && am && (
        <div>
          <h4 style={{ marginBottom: '0.5rem' }}>Before vs After</h4>
          <table className="data-table" style={{ width: '100%' }}>
            <thead>
              <tr><th>Metric</th><th>Before</th><th>After</th><th>Delta</th></tr>
            </thead>
            <tbody>
              {[
                ['Log Size (MB)', 'log_size_mb',  v => `${v.toLocaleString()} MB`],
                ['Used (MB)',     'log_used_mb',  v => `${v.toLocaleString()} MB`],
                ['Used %',        'log_used_pct', v => `${v.toFixed(2)}%`],
                ['VLF Count',     'vlf_count',    v => v],
              ].map(([label, key, fmt]) => {
                const bv = bm[key], av = am[key]
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
          {result.delta_mb_freed > 0 && (
            <div style={{
              marginTop: '1rem', padding: '0.75rem 1rem',
              background: 'var(--success-bg)', border: '1px solid rgba(5,150,105,0.25)',
              borderRadius: 'var(--radius-sm)', color: 'var(--success)', fontWeight: 700,
            }}>
              🎉 {result.delta_mb_freed.toLocaleString()} MB freed
            </div>
          )}
        </div>
      )}

      {/* Multi-object results (heap clustering, unused indexes, ghost pages) */}
      {hasMultiResults && <MultiObjectResults results={result.results} />}
    </div>
  )
}

export default function ExecutionScreen({ session, executionData, onDone }) {
  // executionData is an ARRAY of ExecuteResponse objects (one per issue run)
  const results = Array.isArray(executionData) ? executionData : (executionData ? [executionData] : [])
  const status  = overallStatus(results)

  const [step, setStep] = useState('precheck')

  useEffect(() => {
    const seq = ['precheck', 'running', 'verifying', 'done']
    let i = 0
    const iv = setInterval(() => {
      i++
      if (i < seq.length) setStep(seq[i])
      else clearInterval(iv)
    }, 400)
    return () => clearInterval(iv)
  }, [])

  const stepStatus = (s) => {
    const idx = STEPS.findIndex(x => x.id === s)
    const cur = STEPS.findIndex(x => x.id === step)
    if (idx < cur) return 'done'
    if (idx === cur) return step === 'done'
      ? (status === 'success' || status === 'partial' ? 'done' : 'failed')
      : 'active'
    return 'pending'
  }

  const headline = results.length === 1 ? labelFor(results[0].issue_id) : `${results.length} Optimizations`

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '2rem', maxWidth: '820px', margin: '0 auto' }}>

      {/* Header */}
      <div>
        <h1 style={{ fontSize: '1.6rem', marginBottom: '0.25rem' }}>
          Executing <span className="text-gradient">{headline}</span>
        </h1>
        <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>
          Running pre-execution safety checks → optimizations → post-verification
        </p>
      </div>

      {/* Pipeline */}
      <div className="glass-card" style={{ padding: '2rem' }}>
        <div className="status-pipeline">
          {STEPS.map(s => (
            <div key={s.id} className={`status-step ${stepStatus(s.id)}`}>
              <div className={`step-dot ${stepStatus(s.id)}`}>
                {stepStatus(s.id) === 'done'   ? '✓'
                  : stepStatus(s.id) === 'failed' ? '✗'
                  : stepStatus(s.id) === 'active' ? '…'
                  : '○'}
              </div>
              <span className="step-label">{s.label}</span>
            </div>
          ))}
        </div>

        {/* Overall status message */}
        <div style={{ textAlign: 'center', marginTop: '1.5rem' }}>
          {status === 'success' && (
            <div style={{ color: 'var(--success)', fontSize: '1.1rem', fontWeight: 700 }}>
              ✅ {results.length === 1 ? results[0].message : `All ${results.length} optimizations completed successfully.`}
            </div>
          )}
          {status === 'partial' && (
            <div className="toast toast-warning" style={{ justifyContent: 'center' }}>
              ⚠️ Some optimizations completed with errors — see details below.
            </div>
          )}
          {status === 'failed' && (
            <div className="toast toast-error" style={{ justifyContent: 'center' }}>
              ❌ {results.length === 1 ? results[0].message : 'All optimizations failed — see details below.'}
            </div>
          )}
          {status === 'skipped' && (
            <div className="toast toast-warning" style={{ justifyContent: 'center' }}>
              ⏭️ {results.length === 1 ? results[0].message : 'Optimizations were skipped.'}
            </div>
          )}
        </div>
      </div>

      {/* Per-issue result cards */}
      {results.map((result, i) => (
        <IssueResultCard key={i} result={result} />
      ))}

      {/* Actions */}
      <div style={{ display: 'flex', gap: '1rem', justifyContent: 'flex-end' }}>
        <button id="btn-view-report" className="btn btn-primary btn-lg" onClick={onDone}>
          View Full Report →
        </button>
      </div>
    </div>
  )
}
