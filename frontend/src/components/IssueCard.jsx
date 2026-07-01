/**
 * IssueCard.jsx
 * Individual diagnostic card for one of the storage/data-quality checks.
 */
import { useState, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import { apiExecute, apiReclaimProgress, apiStorageRedundancy, apiTableIntelligence, apiDataCompression } from '../api.js'
import TableExport, { CopyButton } from './TableExport.jsx'

const SEVERITY_META = {
  High:   { cls: 'badge-high',   icon: '🔴', desc: 'High' },
  Medium: { cls: 'badge-medium', icon: '🟠', desc: 'Medium' },
  Low:    { cls: 'badge-low',    icon: '🟢', desc: 'Low' },
  None:   { cls: 'badge-none',   icon: '⬜', desc: 'None' },
}

const fmtNum = (v) => (typeof v === 'number' ? v.toLocaleString() : v)

function MetricPill({ label, value, onClick, active }) {
  const clickable = typeof onClick === 'function'
  return (
    <div
      className="metric-tile"
      onClick={onClick}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={clickable ? (e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }) : undefined}
      style={clickable ? {
        cursor: 'pointer', userSelect: 'none',
        outline: active ? '1px solid var(--accent)' : undefined,
      } : undefined}
    >
      <div className="metric-value" style={{ fontSize: '1rem' }}>{value ?? '—'}</div>
      <div className="metric-label">{label}{clickable ? (active ? ' ▲' : ' ▼') : ''}</div>
    </div>
  )
}

/* Problem 20 — per-table reversible QUARANTINE RENAME (never DROP). */
function ShadowQuarantinePanel({ objects }) {
  const [results, setResults] = useState({})
  const [busy, setBusy] = useState(null)

  const quarantine = async (o) => {
    const key = `${o.schema}.${o.table}`
    if (!window.confirm(
      `Quarantine [${o.schema}].[${o.table}] by renaming it (dated suffix)?\n\n` +
      `This is REVERSIBLE (rename back to restore) and does NOT delete any data. ` +
      `It surfaces any hidden dependency immediately.`)) return
    setBusy(key)
    try {
      const res = await apiExecute('shadow_tables', null, { target_schema: o.schema, target_table: o.table })
      setResults(p => ({ ...p, [key]: res }))
    } catch (e) {
      setResults(p => ({ ...p, [key]: { status: 'failed', message: e.message } }))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
      <div className="toast toast-warning" style={{ fontSize: '0.78rem' }}>
        <span>⚠️</span>
        <span>This tool never drops tables. Quarantine is a reversible rename — a real-world
          usage test. Larger size means more important to <em>investigate</em>, not safer to remove.
          A clean dependency count does not prove safety (it can't see app code, jobs, or BI tools).</span>
      </div>
      <TableExport filename="shadow-tables" count={objects.length}
        headers={['Schema', 'Table', 'Size (MB)', 'Rows', 'Modified', 'Counterpart', 'Counterpart Exists', 'Dependencies']}
        rows={objects.map(o => [o.schema, o.table, o.size_mb ?? '', o.row_count ?? '',
          o.modify_date ? String(o.modify_date).slice(0, 10) : '', o.counterpart_guess || '',
          o.counterpart_exists ? 'Yes' : 'No', o.dependency_count < 0 ? '' : o.dependency_count])} />
      <table className="data-table" style={{ fontSize: '0.78rem' }}>
        <thead>
          <tr><th>Table</th><th>Size</th><th>Rows</th><th>Modified</th><th>Counterpart</th><th>Deps</th><th></th></tr>
        </thead>
        <tbody>
          {objects.map((o, i) => {
            const key = `${o.schema}.${o.table}`
            const r = results[key]
            return (
              <tr key={i}>
                <td style={{ color: 'var(--text-primary)' }}>{o.table}</td>
                <td>{o.size_mb != null ? `${o.size_mb.toLocaleString()} MB` : '—'}</td>
                <td>{fmtNum(o.row_count)}</td>
                <td>{o.modify_date ? String(o.modify_date).slice(0, 10) : '—'}</td>
                <td style={{ color: o.counterpart_exists ? 'var(--success)' : 'var(--text-muted)' }}>
                  {o.counterpart_guess ? `${o.counterpart_guess}${o.counterpart_exists ? ' ✓' : ' ?'}` : '—'}
                </td>
                <td style={{ color: o.dependency_count > 0 ? 'var(--severity-medium)' : 'var(--text-muted)' }}>
                  {o.dependency_count < 0 ? '?' : o.dependency_count}
                </td>
                <td>
                  {r ? (
                    <span style={{ fontSize: '0.72rem', color: r.status === 'success' ? 'var(--success)' : 'var(--error)' }}>
                      {r.status === 'success' ? '✅ Quarantined' : `❌ ${r.status}`}
                    </span>
                  ) : (
                    <button
                      className="btn btn-ghost"
                      style={{ padding: '3px 10px', fontSize: '0.72rem' }}
                      disabled={busy === key}
                      onClick={() => quarantine(o)}
                    >
                      {busy === key ? 'Working…' : 'Quarantine'}
                    </button>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {Object.values(results).some(r => r?.status === 'success') && (
        <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
          Renamed tables remain fully intact under their new name. Restore by renaming back.
          Actual removal, if ever, is a separate manual DBA decision outside this tool.
        </div>
      )}
    </div>
  )
}

/* Generic small detail table for blank-string / datatype findings. */
function SimpleObjectTable({ objects, columns, filename }) {
  // Export the FULL set (the on-screen table is capped at 12 rows). Prefer the
  // raw field; fall back to the rendered text, but never a JSX object.
  const exportRows = objects.map(o => columns.map(c => {
    const v = c.render ? c.render(o) : o[c.key]
    return (v && typeof v === 'object') ? (o[c.key] ?? '') : (v ?? '')
  }))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
      {filename && <TableExport filename={filename} count={objects.length}
        headers={columns.map(c => c.label)} rows={exportRows} />}
      <table className="data-table" style={{ fontSize: '0.78rem' }}>
        <thead><tr>{columns.map(c => <th key={c.key}>{c.label}</th>)}</tr></thead>
        <tbody>
          {objects.slice(0, 12).map((o, i) => (
            <tr key={i}>{columns.map(c => <td key={c.key}>{c.render ? c.render(o) : (o[c.key] ?? '—')}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// Archival-candidate bucket → badge style (visual gradient, strongest first).
const ARCHIVAL_BUCKET_BADGE = {
  'Very High Confidence Archive Candidate': 'badge-high',
  'High Confidence Archive Candidate':      'badge-medium',
  'Requires Business Validation':           'badge-info',
  'Probably Active':                        'badge-low',
  'Ignore':                                 'badge-none',
  'Could Not Analyze':                      'badge-none',
}
const RISK_BADGE = { High: 'badge-high', Medium: 'badge-medium', Low: 'badge-low' }

/* AI-Powered Legacy Table Archival Candidate Detection — read-only report. */
function ArchivalCandidatesPanel({ issue }) {
  const m = issue.current_metrics || {}
  const objs = issue.affected_objects || []
  const failed = m.failed_tables || []
  const disclaimer = m.disclaimer

  const PAGE = 20
  const [showAll, setShowAll] = useState(false)
  const [sortBy, setSortBy] = useState('score')   // 'score' | 'risk'

  const RISK_RANK = { High: 3, Medium: 2, Low: 1 }
  const scoreOf = (o) => (o.confidence_score == null ? -1 : o.confidence_score)
  const sortedObjs = [...objs].sort((a, b) => {
    if (sortBy === 'risk') {
      const d = (RISK_RANK[b.risk_level] || 0) - (RISK_RANK[a.risk_level] || 0)
      if (d) return d
    }
    return scoreOf(b) - scoreOf(a)   // score desc; nulls last; risk tiebreak
  })
  const shown = showAll ? sortedObjs : sortedObjs.slice(0, PAGE)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
      {/* Mandatory disclaimer — rendered verbatim. */}
      {disclaimer && (
        <div className="toast toast-warning" style={{ fontSize: '0.78rem' }}>
          <span>⚠️</span>
          <span>{disclaimer}</span>
        </div>
      )}

      {objs.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.74rem' }}>
          <span style={{ color: 'var(--text-muted)' }}>Sort by:</span>
          <button
            className={`btn ${sortBy === 'score' ? 'btn-primary' : 'btn-ghost'}`}
            style={{ padding: '3px 10px', fontSize: '0.72rem' }}
            onClick={() => setSortBy('score')}
          >Confidence score</button>
          <button
            className={`btn ${sortBy === 'risk' ? 'btn-primary' : 'btn-ghost'}`}
            style={{ padding: '3px 10px', fontSize: '0.72rem' }}
            onClick={() => setSortBy('risk')}
          >Risk level</button>
        </div>
      )}

      {objs.length > 0 && (
        <TableExport filename="archival-candidates" count={sortedObjs.length}
          headers={['Schema', 'Table', 'Confidence Bucket', 'Score (/70)', 'Risk', 'Years Idle', 'Reserved (MB)', 'Suggested Action', 'Reason', 'Data Quality Flag']}
          rows={sortedObjs.map(o => [o.schema_name, o.table_name, o.confidence_bucket || '',
            o.confidence_score == null ? '' : o.confidence_score, o.risk_level || '',
            o.years_since_latest_activity == null ? '' : o.years_since_latest_activity,
            o.storage?.reserved_mb != null ? Math.round(o.storage.reserved_mb) : '',
            o.suggested_action || '', o.reason || '', o.data_quality_flag ? 'Yes' : 'No'])} />
      )}
      {objs.length > 0 && (
        <div className="archival-table-scroll">
        <table className="data-table" style={{ fontSize: '0.76rem' }}>
          <thead>
            <tr>
              <th>Table</th><th>Confidence</th><th>Score</th><th>Risk</th>
              <th>Years Idle</th><th>Reserved</th><th>Suggested</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((o, i) => (
              <tr key={i} title={o.reason}>
                <td style={{ color: 'var(--text-primary)' }}>
                  {o.schema_name}.{o.table_name}
                  {o.data_quality_flag && <span title={o.data_quality_notes || 'data-quality flag'}> ⚠</span>}
                </td>
                <td><span className={`badge ${ARCHIVAL_BUCKET_BADGE[o.confidence_bucket] || 'badge-none'}`}>
                  {o.confidence_bucket}
                </span></td>
                <td>{o.confidence_score == null ? '—' : `${o.confidence_score}/70`}</td>
                <td><span className={`badge ${RISK_BADGE[o.risk_level] || 'badge-none'}`}>{o.risk_level}</span></td>
                <td>{o.years_since_latest_activity == null ? '—' : o.years_since_latest_activity}</td>
                <td>{o.storage?.reserved_mb != null ? `${fmtNum(Math.round(o.storage.reserved_mb))} MB` : '—'}</td>
                <td>{o.suggested_action}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      {objs.length > PAGE && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          <button className="btn btn-ghost" style={{ padding: '4px 12px', fontSize: '0.75rem' }}
                  onClick={() => setShowAll(v => !v)}>
            {showAll ? `Show top ${PAGE}` : `Show all ${objs.length}`}
          </button>
          <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
            Showing {shown.length} of {objs.length} candidate(s)
          </span>
        </div>
      )}

      {failed.length > 0 && (
        <div>
          <div className="label" style={{ marginBottom: '0.3rem' }}>Could Not Analyze ({failed.length})</div>
          <TableExport filename="archival-could-not-analyze" count={failed.length}
            headers={['Schema', 'Table', 'Error']}
            rows={failed.map(f => [f.schema_name, f.table_name, f.error_message || ''])} />
          <table className="data-table" style={{ fontSize: '0.74rem', marginTop: '0.3rem' }}>
            <thead><tr><th>Table</th><th>Error</th></tr></thead>
            <tbody>
              {failed.map((f, i) => (
                <tr key={i}>
                  <td>{f.schema_name}.{f.table_name}</td>
                  <td style={{ color: 'var(--text-muted)' }}>{f.error_message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p style={{ fontSize: '0.7rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
        Hover a row to see the full scoring rationale. Confidence is a 0–70 score (see methodology),
        not a probability. "Archive Candidate" is a label — this module performs no archival action.
      </p>
    </div>
  )
}

/* Data File Space Reclamation — explicit two-phase panel (safe → deep). */
function DataFileReclaimPanel({ issue }) {
  const files = issue.affected_objects || []
  const actionable = files.some(f => f.shrink_required)
  const [result, setResult] = useState(null)   // result of the most recent run
  const [busy, setBusy] = useState(null)        // 'truncate_only' | 'deep_compaction' | 'minimize_file'
  const [progress, setProgress] = useState(null)

  // While a run is in flight, poll live telemetry (percent_complete, blocker).
  useEffect(() => {
    if (!busy) { setProgress(null); return }
    let active = true
    const id = setInterval(async () => {
      try {
        const p = await apiReclaimProgress()
        if (active) setProgress(p && p.phase ? p : null)
      } catch { /* transient — keep polling */ }
    }, 1000)
    return () => { active = false; clearInterval(id) }
  }, [busy])

  const CONFIRM = {
    deep_compaction:
      'Deep Compaction moves data pages to shrink the file — this FRAGMENTS indexes, ' +
      'so it immediately rebuilds them afterward (heavy I/O + log). Run only in a ' +
      'low-traffic window. Continue?',
    minimize_file:
      'Minimize File Size rebuilds indexes FIRST (to their minimal size), then shrinks ' +
      'the file as far as possible. It maximizes disk reclaimed, but the FINAL shrink ' +
      're-introduces layout fragmentation and indexes are NOT rebuilt again afterward. ' +
      'Heavy I/O + log. Continue?',
  }

  const run = async (choice) => {
    if (CONFIRM[choice] && !window.confirm(CONFIRM[choice])) return
    setBusy(choice)
    setResult(null)
    try {
      const res = await apiExecute('data_file_reclaim', choice)
      setResult(res)
    } catch (e) {
      setResult({ status: 'failed', message: e.message, results: [] })
    } finally {
      setBusy(null)
    }
  }

  const statusColor = (s) =>
    s === 'success' ? 'var(--success)' : s === 'blocked' ? 'var(--warning)' : 'var(--error)'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
      <div className="toast toast-warning" style={{ fontSize: '0.78rem' }}>
        <span>⚠️</span>
        <span><strong>Safe Reclaim</strong> (TRUNCATEONLY) drops trailing free space with zero
          fragmentation. <strong>Deep Compaction</strong> shrinks then rebuilds, keeping indexes
          clean (the file regrows into the buffer). <strong>Minimize File Size</strong> rebuilds
          first then shrinks — smallest file, but the final shrink re-introduces fragmentation.
          Shrinks back off if blocked — no session is ever killed.</span>
      </div>

      {/* Per-file snapshot */}
      {files.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
        <TableExport filename="data-file-reclaim" count={files.length}
          headers={['Data File', 'Size (MB)', 'Used (MB)', 'Free (MB)', 'Target (MB)', 'Reclaimable (MB)', 'Shrink Required']}
          rows={files.map(f => [f.logical_name, f.size_mb, f.used_mb, f.free_mb, f.target_mb, f.shrink_required ? f.reclaimable_mb : 0, f.shrink_required ? 'Yes' : 'No'])} />
        <table className="data-table" style={{ fontSize: '0.76rem' }}>
          <thead><tr>
            <th>Data File</th><th>Size</th><th>Used</th><th>Free</th><th>Target</th><th>Reclaimable</th>
          </tr></thead>
          <tbody>
            {files.map((f, i) => (
              <tr key={i}>
                <td style={{ color: 'var(--text-primary)' }}>{f.logical_name}</td>
                <td>{fmtNum(f.size_mb)} MB</td>
                <td>{fmtNum(f.used_mb)} MB</td>
                <td>{fmtNum(f.free_mb)} MB</td>
                <td>{fmtNum(f.target_mb)} MB</td>
                <td style={{ color: f.shrink_required ? 'var(--text-accent)' : 'var(--text-muted)' }}>
                  {f.shrink_required ? `${fmtNum(f.reclaimable_mb)} MB` : '— optimal'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}

      {/* Phase buttons (only when something is actually reclaimable) */}
      {!actionable ? (
        <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
          ✅ All data files are within the 16% buffer — nothing to reclaim.
        </p>
      ) : (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
        <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>
          <button className="btn btn-primary" disabled={busy} onClick={() => run('truncate_only')}>
            {busy === 'truncate_only'
              ? <><div className="spinner" style={{ width: 14, height: 14, borderTopColor: 'white' }} /> Reclaiming…</>
              : '🧹 Safe Reclaim (TRUNCATEONLY)'}
          </button>
          {result?.deep_compaction_available && (
            <button className="btn btn-danger" disabled={busy} onClick={() => run('deep_compaction')}>
              {busy === 'deep_compaction'
                ? <><div className="spinner" style={{ width: 14, height: 14, borderTopColor: 'white' }} /> Compacting…</>
                : '⚠ Deep Compaction (clean indexes)'}
            </button>
          )}
          <button className="btn btn-ghost" disabled={busy} onClick={() => run('minimize_file')}
                  style={{ borderColor: 'var(--severity-high)', color: 'var(--severity-high)' }}>
            {busy === 'minimize_file'
              ? <><div className="spinner" style={{ width: 14, height: 14 }} /> Minimizing…</>
              : '⤓ Minimize File Size'}
          </button>
        </div>
        <div className="toast toast-warning" style={{ fontSize: '0.72rem' }}>
          <span>⚠️</span>
          <span><strong>Minimize File Size</strong> maximizes disk reclamation, but the final
            shrink step will re-introduce layout fragmentation (indexes are rebuilt before the
            shrink, not after).</span>
        </div>
      </div>
      )}

      {/* Live progress (page-moving shrink / rebuild phases) */}
      {busy && progress && (
        <div style={{
          background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)',
          borderRadius: 'var(--radius-sm)', padding: '0.6rem 0.85rem', fontSize: '0.76rem',
        }}>
          {progress.phase === 'rebuilding' ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: 'var(--text-secondary)' }}>
              <div className="spinner" style={{ width: 12, height: 12 }} />
              {progress.message || 'Rebuilding indexes…'}
            </div>
          ) : progress.phase === 'shrinking' ? (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.3rem', color: 'var(--text-secondary)' }}>
                <span>Shrinking {progress.file ? `[${progress.file}]` : ''}{progress.command ? ` · ${progress.command}` : ''}</span>
                <span>{progress.percent_complete != null ? `${progress.percent_complete}%` : '…'}</span>
              </div>
              <div style={{ height: 6, background: 'var(--bg-base)', borderRadius: 3, overflow: 'hidden' }}>
                <div style={{ height: '100%', width: `${progress.percent_complete || 0}%`,
                              background: 'var(--accent)', transition: 'width 0.4s' }} />
              </div>
              {progress.blocking_spid != null && (
                <div style={{ marginTop: '0.3rem', color: 'var(--warning)', fontSize: '0.72rem' }}>
                  ⏳ Waiting on a lock held by SPID {progress.blocking_spid} (will back off, not kill).
                </div>
              )}
            </>
          ) : (
            <span style={{ color: 'var(--text-muted)' }}>Working…</span>
          )}
        </div>
      )}

      {/* Result */}
      {result && (
        <div style={{
          background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)',
          borderRadius: 'var(--radius-sm)', padding: '0.75rem 1rem', fontSize: '0.8rem',
        }}>
          <div style={{ color: statusColor(result.status), fontWeight: 600, marginBottom: '0.4rem' }}>
            {result.status?.toUpperCase()} — {result.message}
          </div>
          {(result.results || []).map((r, i) => (
            <div key={i} style={{ fontSize: '0.76rem', color: 'var(--text-secondary)' }}>
              <span style={{ color: statusColor(r.status) }}>●</span>{' '}
              [{r.logical_name}] {r.message}
              {r.blocking_spid != null && <span style={{ color: 'var(--warning)' }}> (SPID {r.blocking_spid})</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* AI Storage & Redundancy Analysis — on-demand local-model report. */
// Claude models selectable for this feature. "" = let the backend use its
// configured default (ANTHROPIC_MODEL). "custom" reveals a free-text field so a
// newer model id can be used without a code change.
const MODEL_OPTIONS = [
  { value: '',                  label: 'Server default' },
  { value: 'claude-sonnet-4-6', label: 'Sonnet 4.6  (default, balanced)' },
  { value: 'claude-haiku-4-5',  label: 'Haiku 4.5  (fast / cheapest)' },
  { value: 'claude-opus-4-8',   label: 'Opus 4.8  (max quality)' },
  { value: 'custom',            label: 'Custom…' },
]

// Cache the last report + model choice across modal open/close. The panel
// unmounts when the modal closes, so without this the (billed) report would be
// lost and would have to be re-run every time the tile is reopened.
const _storageRedundancyCache = { data: null, error: null, modelSel: '', modelCustom: '' }

function StorageRedundancyPanel() {
  const [busy, setBusy] = useState(false)
  const [data, setData] = useState(_storageRedundancyCache.data)     // success payload
  const [error, setError] = useState(_storageRedundancyCache.error)  // { kind, message }
  const [modelSel, setModelSel] = useState(_storageRedundancyCache.modelSel)
  const [modelCustom, setModelCustom] = useState(_storageRedundancyCache.modelCustom)

  // Resolve the dropdown/custom field into the value sent to the API.
  const resolvedModel = modelSel === 'custom' ? modelCustom.trim() : modelSel

  // Persist the model selection so it reopens as it was left.
  useEffect(() => {
    Object.assign(_storageRedundancyCache, { modelSel, modelCustom })
  }, [modelSel, modelCustom])

  const run = async () => {
    setBusy(true); setError(null); setData(null)
    try {
      const res = await apiStorageRedundancy(resolvedModel || null)
      if (res.status === 'ok') { setData(res); _storageRedundancyCache.data = res; _storageRedundancyCache.error = null }
      else if (res.status === 'empty') { const e = { kind: 'empty', message: res.message || 'No user tables found.' }; setError(e); _storageRedundancyCache.error = e; _storageRedundancyCache.data = null }
      else { const e = { kind: res.error_kind || 'error', message: res.error || res.message || 'Analysis failed.' }; setError(e); _storageRedundancyCache.error = e; _storageRedundancyCache.data = null }
    } catch (e) {
      const er = { kind: 'network', message: e.message }; setError(er); _storageRedundancyCache.error = er; _storageRedundancyCache.data = null
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
      <div className="toast toast-warning" style={{ fontSize: '0.78rem' }}>
        <span>🤖</span>
        <span>Finds the largest ~20% of tables by storage and asks the <strong>Claude API</strong>
          {' '}to flag naming patterns, similar row counts, and fragmentation. Sends table
          names + sizes (never row data) to Anthropic's cloud — requires an
          <strong> API key configured on the backend</strong>. Usually completes in a few seconds.</span>
      </div>

      {/* Model picker — overrides the backend's configured default per run via ?model= */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
        <label htmlFor="ai-model-select" style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
          Model
        </label>
        <select
          id="ai-model-select"
          className="form-input"
          value={modelSel}
          disabled={busy}
          onChange={(e) => setModelSel(e.target.value)}
          style={{ width: 'auto', minWidth: 200, padding: '0.35rem 0.5rem', fontSize: '0.8rem' }}
        >
          {MODEL_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        {modelSel === 'custom' && (
          <input
            className="form-input"
            type="text"
            placeholder="e.g. claude-3-5-haiku-latest"
            value={modelCustom}
            disabled={busy}
            onChange={(e) => setModelCustom(e.target.value)}
            style={{ width: 'auto', minWidth: 180, padding: '0.35rem 0.5rem', fontSize: '0.8rem' }}
          />
        )}
        <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
          runs via the Claude API (key set on the backend)
        </span>
      </div>

      <div>
        <button
          className="btn btn-primary"
          disabled={busy || (modelSel === 'custom' && !modelCustom.trim())}
          onClick={run}
        >
          {busy
            ? <><div className="spinner" style={{ width: 14, height: 14, borderTopColor: 'white' }} /> Analyzing…</>
            : '🤖 Run AI Storage Analysis'}
        </button>
      </div>

      {busy && (
        <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
          Asking Claude to analyze storage patterns — usually a few seconds. Keep this open.
        </p>
      )}

      {error && (
        <div className="toast toast-error" style={{ fontSize: '0.8rem' }}>
          <span>⚠️</span>
          <span><strong>{({
            auth_error: 'API key problem',
            api_unreachable: 'Anthropic API unreachable',
            model_not_found: 'Model not found',
            rate_limited: 'Rate limited',
            timeout: 'Timed out',
            api_error: 'API error',
            db_error: 'Database error',
            empty: 'Empty database',
            network: 'Request failed',
          })[error.kind] || 'Error'}:</strong> {error.message}</span>
        </div>
      )}

      {data && (
        <>
          {/* Summary header */}
          <div className="metric-grid">
            <MetricPill label="User Tables"   value={fmtNum(data.total_user_table_count)} />
            <MetricPill label="Analyzed (20%)" value={fmtNum(data.analyzed_table_count)} />
            <MetricPill label="Coverage"      value={`${data.analyzed_percentage}%`} />
            <MetricPill label="Model"         value={data.model_used} />
          </div>
          {data.was_truncated && (
            <div style={{ fontSize: '0.74rem', color: 'var(--warning)' }}>
              ⚠ Table list truncated for the model — analysis covers the largest tables only.
            </div>
          )}

          {/* Model's markdown report */}
          <div className="markdown-body" style={{
            background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)',
            borderRadius: 'var(--radius-sm)', padding: '0.5rem 1rem', fontSize: '0.84rem',
          }}>
            <ReactMarkdown>{data.analysis_markdown || '_No analysis returned._'}</ReactMarkdown>
          </div>

          {/* Raw data table */}
          {data.table_data?.length > 0 && (
            <details>
              <summary style={{ cursor: 'pointer', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                Raw data — {data.table_data.length} table(s)
              </summary>
              <div style={{ marginTop: '0.4rem' }}>
                <TableExport filename="storage-redundancy-tables" count={data.table_data.length}
                  headers={['Schema', 'Table', 'Rows', 'Total MB', 'Used MB', 'Unused MB']}
                  rows={data.table_data.map(r => [r.SchemaName, r.TableName, r.RowCount, r.TotalSpaceMB, r.UsedSpaceMB, r.UnusedSpaceMB])} />
              </div>
              <table className="data-table" style={{ fontSize: '0.74rem', marginTop: '0.4rem' }}>
                <thead><tr>
                  <th>Table</th><th>Rows</th><th>Total MB</th><th>Used MB</th><th>Unused MB</th>
                </tr></thead>
                <tbody>
                  {data.table_data.map((r, i) => (
                    <tr key={i}>
                      <td style={{ color: 'var(--text-primary)' }}>{r.SchemaName}.{r.TableName}</td>
                      <td>{fmtNum(r.RowCount)}</td>
                      <td>{fmtNum(r.TotalSpaceMB)}</td>
                      <td>{fmtNum(r.UsedSpaceMB)}</td>
                      <td>{fmtNum(r.UnusedSpaceMB)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
        </>
      )}
    </div>
  )
}

// ── Table Intelligence — per-table profile for every user table ───────────────
const TI_COLUMNS = [
  { key: 'name',         label: 'Table',      align: 'left', get: t => `${t.SchemaName}.${t.TableName}`, title: 'Schema.Table name — click to sort' },
  { key: 'RowCount',     label: 'Rows',       num: true, title: 'Row count — click to sort' },
  { key: 'TotalMB',      label: 'MB',         num: true, title: 'Total space used by the table, in MB — click to sort' },
  { key: 'Created',      label: 'Created',    date: true, title: 'Date the table was created — click to sort' },
  { key: 'LastWrite',    label: 'Last write', date: true, title: 'Most recent insert/update/delete — since the last SQL Server restart only ("cold" = none since then). Click to sort' },
  { key: 'RefTotal',     label: 'Refs',       num: true, title: 'How many SQL modules (procs/views/functions/triggers) reference this table — the "blast radius" if you change or drop it. Click to sort' },
  { key: 'IndexCount',   label: 'Idx',        num: true, title: 'Number of indexes on the table (clustered + non-clustered; 0 = heap). Click to sort' },
  { key: 'TriggerCount', label: 'Trg',        num: true, title: 'Number of DML triggers (INSERT/UPDATE/DELETE) on the table. Click to sort' },
  { key: 'ReportCount',  label: 'Reports',    num: true, title: 'SSRS reports whose query mentions this table. Click to sort' },
]

const TI_FILTERS = [
  { key: 'all',       label: 'All',            test: () => true },
  { key: 'cold',      label: 'Cold*',          test: t => t.ColdSinceRestart },
  { key: 'heap',      label: 'Heaps',          test: t => t.IsHeap },
  { key: 'nopk',      label: 'No PK',          test: t => !t.HasPK },
  { key: 'noref',     label: 'Zero refs',      test: t => t.RefTotal === 0 },
  { key: 'inreports', label: 'In reports',     test: t => t.ReportCount > 0 },
]

const TI_TOPK = [10, 50, 100, 500, 'all']   // "top K" row limit (applied after sort/filter)

// Cache the result + view controls across modal open/close. The panel unmounts
// when the modal closes, so without this module-level cache the profile is lost
// and re-fetched every time the tile is reopened.
const _tableIntelCache = {
  data: null, error: null, ssrs: true, query: '',
  filter: 'all', sortKey: 'TotalMB', sortDir: 'desc', topK: 100,
}

function TableIntelligencePanel() {
  const [busy, setBusy] = useState(false)
  const [data, setData] = useState(_tableIntelCache.data)
  const [error, setError] = useState(_tableIntelCache.error)
  const [ssrs, setSsrs] = useState(_tableIntelCache.ssrs)
  const [query, setQuery] = useState(_tableIntelCache.query)
  const [filter, setFilter] = useState(_tableIntelCache.filter)
  const [sortKey, setSortKey] = useState(_tableIntelCache.sortKey)
  const [sortDir, setSortDir] = useState(_tableIntelCache.sortDir)
  const [topK, setTopK] = useState(_tableIntelCache.topK)

  const run = async (includeSsrs = ssrs) => {
    setBusy(true); setError(null)
    try {
      const res = await apiTableIntelligence(includeSsrs)
      if (res.status === 'ok') { setData(res); _tableIntelCache.data = res; _tableIntelCache.error = null }
      else if (res.status === 'empty') { const e = { message: res.message || 'No user tables found.' }; setError(e); _tableIntelCache.error = e; _tableIntelCache.data = null }
      else { const e = { message: res.error || res.message || 'Analysis failed.' }; setError(e); _tableIntelCache.error = e; _tableIntelCache.data = null }
    } catch (e) {
      const er = { message: e.message }; setError(er); _tableIntelCache.error = er
    } finally {
      setBusy(false)
    }
  }

  // Auto-run ONLY when there is no cached result. Reopening the tile shows the
  // previously fetched data instead of re-querying the database.
  useEffect(() => { if (!_tableIntelCache.data) run() }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  // Persist the view controls so the tile reopens exactly as it was left.
  useEffect(() => {
    Object.assign(_tableIntelCache, { ssrs, query, filter, sortKey, sortDir, topK })
  }, [ssrs, query, filter, sortKey, sortDir, topK])

  const clickSort = (col) => {
    const key = col.key
    if (sortKey === key) setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortKey(key); setSortDir(col.num || col.date ? 'desc' : 'asc') }
  }

  const rows = data?.tables || []
  const q = query.trim().toLowerCase()
  const filterFn = (TI_FILTERS.find(f => f.key === filter) || TI_FILTERS[0]).test
  const filtered = rows.filter(t =>
    filterFn(t) && (!q || `${t.SchemaName}.${t.TableName}`.toLowerCase().includes(q)))

  const col = TI_COLUMNS.find(c => c.key === sortKey)
  const sorted = [...filtered].sort((a, b) => {
    let av, bv
    if (col?.get) { av = col.get(a).toLowerCase(); bv = col.get(b).toLowerCase() }
    else { av = a[sortKey]; bv = b[sortKey] }
    // nulls (e.g. LastWrite) always sort to the bottom
    if (av == null && bv == null) return 0
    if (av == null) return 1
    if (bv == null) return -1
    if (av < bv) return sortDir === 'asc' ? -1 : 1
    if (av > bv) return sortDir === 'asc' ? 1 : -1
    return 0
  })
  const shown = topK === 'all' ? sorted : sorted.slice(0, topK)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
      <div className="toast toast-warning" style={{ fontSize: '0.78rem' }}>
        <span>📊</span>
        <span>A read-only profile of <strong>every table</strong>: size, age, dependency
          blast-radius (how many procs/views/functions reference it), indexes/triggers/FKs,
          activity, and which SSRS reports use it — to judge what's safe to archive or drop.</span>
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
        <input
          className="form-input"
          type="text"
          placeholder="Filter by name…"
          value={query}
          onChange={e => setQuery(e.target.value)}
          style={{ width: 'auto', minWidth: 180, padding: '0.35rem 0.5rem', fontSize: '0.8rem' }}
        />
        {TI_FILTERS.map(f => (
          <button
            key={f.key}
            type="button"
            onClick={() => setFilter(f.key)}
            className="tile-pill"
            style={{
              cursor: 'pointer', border: 'none',
              background: filter === f.key ? 'var(--accent)' : 'var(--bg-glass)',
              color: filter === f.key ? '#fff' : 'var(--text-secondary)',
            }}
          >{f.label}</button>
        ))}
        <label style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 4 }}>
          Show
          <select
            className="form-input"
            value={String(topK)}
            onChange={e => setTopK(e.target.value === 'all' ? 'all' : Number(e.target.value))}
            style={{ width: 'auto', padding: '0.3rem 0.4rem', fontSize: '0.78rem' }}
          >
            {TI_TOPK.map(k => (
              <option key={k} value={String(k)}>{k === 'all' ? 'All' : `Top ${k}`}</option>
            ))}
          </select>
        </label>
        <label style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 4 }}>
          <input type="checkbox" checked={ssrs} disabled={busy}
            onChange={e => { setSsrs(e.target.checked); run(e.target.checked) }} />
          SSRS
        </label>
        <button className="btn btn-ghost" disabled={busy} onClick={() => run()}
          style={{ padding: '0.3rem 0.6rem', fontSize: '0.78rem' }}>
          {busy ? '…' : '↻ Refresh'}
        </button>
      </div>

      {busy && !data && (
        <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
          Profiling all tables{ssrs ? ' and scanning SSRS reports' : ''}…
        </p>
      )}

      {error && (
        <div className="toast toast-error" style={{ fontSize: '0.8rem' }}>
          <span>⚠️</span><span>{error.message}</span>
        </div>
      )}

      {data && (
        <>
          <div className="metric-grid">
            <MetricPill label="Tables" value={fmtNum(data.total_tables)} />
            <MetricPill label="Matching filter" value={fmtNum(filtered.length)} />
            <MetricPill label="SSRS reports" value={data.ssrs_available ? fmtNum(data.ssrs_report_count) : '—'} />
          </div>

          <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', margin: 0 }}>
            *Activity (Last write, “Cold”) is only tracked since the last SQL restart
            {data.server_start_time ? ` (${data.server_start_time})` : ''} — it resets on restart, so “Cold” means
            “no reads/writes since then”, not “never used”. {data.ssrs_note || ''}
          </p>

          {/* Export the full filtered + sorted set (not just the visible top-K). */}
          <TableExport filename="table-intelligence" count={sorted.length}
            headers={['Schema', 'Table', 'Rows', 'TotalMB', 'Created', 'SchemaModified', 'LastWrite', 'LastRead',
              'Reads', 'Writes', 'ColdSinceRestart', 'Refs', 'RefProcs', 'RefViews', 'RefFuncs', 'RefTriggers',
              'Indexes', 'Triggers', 'FKIn', 'FKOut', 'Columns', 'IsHeap', 'HasPK', 'Reports']}
            rows={sorted.map(t => [t.SchemaName, t.TableName, t.RowCount, t.TotalMB, t.Created, t.SchemaModified,
              t.LastWrite || '', t.LastRead || '', t.Reads, t.Writes, t.ColdSinceRestart, t.RefTotal, t.RefProcs,
              t.RefViews, t.RefFuncs, t.RefTriggers, t.IndexCount, t.TriggerCount, t.FkIn, t.FkOut, t.ColumnCount,
              t.IsHeap, t.HasPK, t.ReportCount])} />

          <div style={{ maxHeight: '52vh', overflow: 'auto', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm)' }}>
            <table className="data-table" style={{ fontSize: '0.74rem', margin: 0 }}>
              <thead>
                <tr>
                  {TI_COLUMNS.map(c => (
                    <th
                      key={c.key}
                      onClick={() => clickSort(c)}
                      title={c.title || 'Click to sort'}
                      style={{
                        position: 'sticky', top: 0, cursor: 'pointer', whiteSpace: 'nowrap',
                        textAlign: c.align === 'left' ? 'left' : 'right', background: 'var(--bg-surface)',
                      }}
                    >
                      {c.label}{sortKey === c.key ? (sortDir === 'asc' ? ' ▲' : ' ▼') : ''}
                    </th>
                  ))}
                  <th title="🧱 = heap (no clustered index); ⚠PK = no primary key"
                      style={{ position: 'sticky', top: 0, background: 'var(--bg-surface)' }}>Flags</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((t, i) => (
                  <tr key={`${t.SchemaName}.${t.TableName}-${i}`}>
                    <td style={{ color: 'var(--text-primary)', whiteSpace: 'nowrap' }}>{t.SchemaName}.{t.TableName}</td>
                    <td style={{ textAlign: 'right' }}>{fmtNum(t.RowCount)}</td>
                    <td style={{ textAlign: 'right' }}>{fmtNum(t.TotalMB)}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>{(t.Created || '').slice(0, 10)}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap', color: t.LastWrite ? 'inherit' : 'var(--text-muted)' }}>
                      {t.LastWrite ? t.LastWrite.slice(0, 10) : 'cold'}
                    </td>
                    <td style={{ textAlign: 'right' }}
                        title={`${t.RefProcs} procs · ${t.RefViews} views · ${t.RefFuncs} funcs · ${t.RefTriggers} triggers`}>
                      {fmtNum(t.RefTotal)}
                    </td>
                    <td style={{ textAlign: 'right' }}>{fmtNum(t.IndexCount)}</td>
                    <td style={{ textAlign: 'right' }}>{fmtNum(t.TriggerCount)}</td>
                    <td style={{ textAlign: 'right' }}
                        title={t.ReportSamples?.length ? t.ReportSamples.slice(0, 10).join('\n') : ''}>
                      {t.ReportCount ? fmtNum(t.ReportCount) : '—'}
                    </td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      {t.IsHeap && <span title="Heap (no clustered index)" style={{ marginRight: 4 }}>🧱</span>}
                      {!t.HasPK && <span title="No primary key" style={{ color: 'var(--warning)' }}>⚠PK</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {shown.length < sorted.length && (
            <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', margin: 0 }}>
              Showing top {fmtNum(shown.length)} of {fmtNum(sorted.length)} matching — raise “Show” or refine the filter.
            </p>
          )}
        </>
      )}
    </div>
  )
}

// ── Data Compression Savings — on-demand estimate (heavy) ─────────────────────
const _compressionCache = { data: null, error: null, topN: 25, mode: 'PAGE' }

function DataCompressionPanel() {
  const [busy, setBusy] = useState(false)
  const [data, setData] = useState(_compressionCache.data)
  const [error, setError] = useState(_compressionCache.error)
  const [topN, setTopN] = useState(_compressionCache.topN)
  const [mode, setMode] = useState(_compressionCache.mode)

  useEffect(() => { Object.assign(_compressionCache, { topN, mode }) }, [topN, mode])

  const run = async () => {
    setBusy(true); setError(null)
    try {
      const res = await apiDataCompression(topN, mode)
      if (res.status === 'ok') { setData(res); _compressionCache.data = res; _compressionCache.error = null }
      else if (res.status === 'empty') { const e = { message: res.message || 'No tables with data.' }; setError(e); _compressionCache.error = e; _compressionCache.data = null }
      else { const e = { message: res.error || res.message || 'Estimate failed.' }; setError(e); _compressionCache.error = e; _compressionCache.data = null }
    } catch (e) {
      const er = { message: e.message }; setError(er); _compressionCache.error = er
    } finally {
      setBusy(false)
    }
  }

  const rows = data?.tables || []
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
      <div className="toast toast-warning" style={{ fontSize: '0.78rem' }}>
        <span>🗜️</span>
        <span>Estimates <strong>ROW/PAGE compression savings</strong> for the largest tables using
          SQL Server's own estimator (it samples ~5% of each table into tempdb, so it can take a
          while on multi-GB tables). Read-only — each row includes an <strong>ALTER … REBUILD</strong>
          script to apply later (a heavy, locking op — run in a maintenance window).</span>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
        <label style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 4 }}>
          Largest
          <select className="form-input" value={String(topN)} disabled={busy}
            onChange={e => setTopN(Number(e.target.value))}
            style={{ width: 'auto', padding: '0.3rem 0.4rem', fontSize: '0.78rem' }}>
            {[10, 25, 50, 100].map(n => <option key={n} value={String(n)}>Top {n}</option>)}
          </select>
        </label>
        <label style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 4 }}>
          Mode
          <select className="form-input" value={mode} disabled={busy}
            onChange={e => setMode(e.target.value)}
            style={{ width: 'auto', padding: '0.3rem 0.4rem', fontSize: '0.78rem' }}>
            <option value="PAGE">PAGE (max)</option>
            <option value="ROW">ROW (lighter)</option>
          </select>
        </label>
        <button className="btn btn-primary" disabled={busy} onClick={run}>
          {busy
            ? <><div className="spinner" style={{ width: 14, height: 14, borderTopColor: 'white' }} /> Estimating…</>
            : '🗜️ Estimate savings'}
        </button>
      </div>

      {busy && (
        <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
          Sampling the top {topN} tables — this can take a minute or more on large tables. Keep this open.
        </p>
      )}

      {error && (
        <div className="toast toast-error" style={{ fontSize: '0.8rem' }}>
          <span>⚠️</span><span>{error.message}</span>
        </div>
      )}

      {data && (
        <>
          <div className="metric-grid">
            <MetricPill label="Tables" value={fmtNum(data.analyzed_table_count)} />
            <MetricPill label="Current" value={`${fmtNum(data.total_current_mb)} MB`} />
            <MetricPill label={`${data.mode} Saving`} value={`${fmtNum(data.total_savings_mb)} MB`} />
            <MetricPill label="Reduction" value={`${data.total_savings_pct}%`} />
          </div>
          {rows.length > 0 && (
            <SimpleObjectTable objects={rows} filename="data-compression" columns={[
              { key: 'tbl', label: 'Table', render: o => `${o.schema}.${o.table}` },
              { key: 'current_mb', label: 'Current MB', render: o => fmtNum(o.current_mb) },
              { key: 'compressed_mb', label: `${data.mode} MB`, render: o => fmtNum(o.compressed_mb) },
              { key: 'savings_mb', label: 'Saved MB', render: o => fmtNum(o.savings_mb) },
              { key: 'savings_pct', label: 'Saved %', render: o => `${o.savings_pct}%` },
              { key: 'apply_script', label: 'Apply script' },
            ]} />
          )}
        </>
      )}
    </div>
  )
}

// Read-only code-viewer for generated remediation scripts. Non-executing —
// each script (and a copy-all) can be copied to the clipboard / an editor.
function ScriptViewer({ scripts }) {
  if (!scripts || scripts.length === 0) return null
  const combined = scripts.map(s => s.script).join('\n\n')
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <span className="label">Remediation scripts (copy — never auto-executed)</span>
        <CopyButton text={combined} label="⧉ Copy all" />
      </div>
      {scripts.map((s, i) => (
        <div key={i} style={{ border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm)', overflow: 'hidden' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '0.5rem', padding: '0.4rem 0.6rem', background: 'var(--bg-glass)' }}>
            <span style={{ fontSize: '0.76rem', fontWeight: 600, color: 'var(--text-primary)' }}>{s.title}</span>
            <CopyButton text={s.script} />
          </div>
          <pre style={{
            margin: 0, padding: '0.6rem 0.8rem', background: 'var(--bg-input)',
            color: 'var(--text-secondary)', fontFamily: "'JetBrains Mono', monospace",
            fontSize: '0.72rem', lineHeight: 1.5, overflowX: 'auto', whiteSpace: 'pre',
          }}>{s.script}</pre>
        </div>
      ))}
    </div>
  )
}

// Reset the on-demand panel caches. MUST be called when the DB session changes
// (connect/disconnect) so a new database never shows the previous one's report.
export function clearPanelCaches() {
  Object.assign(_storageRedundancyCache, { data: null, error: null, modelSel: '', modelCustom: '' })
  Object.assign(_tableIntelCache, {
    data: null, error: null, ssrs: true, query: '',
    filter: 'all', sortKey: 'TotalMB', sortDir: 'desc', topK: 100,
  })
  Object.assign(_compressionCache, { data: null, error: null, topN: 25, mode: 'PAGE' })
}

export default function IssueCard({ issue, checked, remediated, onToggle, recoveryChoice, onChoiceChange }) {
  const isStorageAI = issue.issue_id === 'storage_redundancy'    // on-demand AI feature
  const isTableIntel = issue.issue_id === 'table_intelligence'   // on-demand per-table profiler
  const isCompression = issue.issue_id === 'data_compression'    // on-demand compression estimate
  const isInfoTile = isStorageAI || isTableIntel || isCompression
  const isNoIssue = !isInfoTile && !issue.affected_objects?.length && issue.severity === 'Low'
  const meta = SEVERITY_META[issue.severity] || SEVERITY_META.None

  const isShadow = issue.issue_id === 'shadow_tables'
  const isDataFileReclaim = issue.issue_id === 'data_file_reclaim'
  // These are "executable" but ONLY via their own per-object panel buttons —
  // they must NOT show the issue-level batch checkbox.
  const isPanelExec = isShadow || isDataFileReclaim || isInfoTile
  const canCheck = issue.executable && !isPanelExec && !remediated
    && (issue.eligible_for_fix || issue.recovery_decision_required) && !issue.error
  const comingSoon = !issue.executable
  const blockedMsg = issue.executable && !isPanelExec && !issue.eligible_for_fix
    && !issue.recovery_decision_required ? issue.blocking_reason : null

  const m = issue.current_metrics || {}
  const objs = issue.affected_objects || []

  // Toggle for the click-to-expand heap-table list (heap_clustering card).
  const [showHeaps, setShowHeaps] = useState(false)

  return (
    <div
      className="glass-card"
      id={`issue-card-${issue.issue_id}`}
      style={{
        padding: '1.5rem',
        display: 'flex',
        flexDirection: 'column',
        gap: '1rem',
        opacity: isNoIssue ? 0.55 : 1,
        transition: 'opacity 0.2s',
        borderColor: (checked || issue.recovery_decision_required)
          ? 'var(--accent)'
          : 'var(--border-subtle)',
        boxShadow: (checked || issue.recovery_decision_required)
          ? '0 0 0 2px var(--accent-glow)'
          : 'none',
      }}
    >
      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '1rem' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            <span className={`badge ${meta.cls}`}>{meta.icon} {meta.desc}</span>
            {issue.error && <span className="badge badge-high">⚠ Check failed</span>}
            {comingSoon && <span className="badge badge-info">📋 Analysis only</span>}
          </div>
          <h3 style={{ fontSize: '1rem' }}>{issue.issue_name}</h3>
        </div>

        {/* Checkbox / coming-soon / per-table-action note */}
        <div style={{ flexShrink: 0, paddingTop: '2px' }}>
          {remediated ? (
            <span className="badge badge-low">✅ Remediated this session</span>
          ) : comingSoon ? (
            <span style={{
              fontSize: '0.7rem', color: 'var(--text-muted)', background: 'var(--bg-glass)',
              border: '1px solid var(--border-subtle)', borderRadius: '6px',
              padding: '4px 10px', whiteSpace: 'nowrap',
            }}>
              Analysis only
            </span>
          ) : isPanelExec ? (
            <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
              {isShadow ? 'Per-table actions below' : isStorageAI ? '🤖 Run AI analysis below' : 'File actions below'}
            </span>
          ) : issue.recovery_decision_required ? (
            <span style={{ fontSize: '0.8rem', color: 'var(--text-accent)', fontWeight: 600 }}>Action Required</span>
          ) : (
            <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: canCheck ? 'pointer' : 'not-allowed' }}>
              <input
                id={`chk-${issue.issue_id}`}
                type="checkbox"
                checked={checked}
                disabled={!canCheck}
                onChange={canCheck ? onToggle : undefined}
                style={{ width: 18, height: 18, accentColor: 'var(--accent)', cursor: canCheck ? 'pointer' : 'not-allowed' }}
              />
              <span style={{ fontSize: '0.8rem', color: canCheck ? 'var(--text-secondary)' : 'var(--text-muted)' }}>
                Select
              </span>
            </label>
          )}
        </div>
      </div>

      {/* No-issue state */}
      {isNoIssue && !issue.error ? (
        <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
          ✅ No action needed — {issue.recommended_action}
        </p>
      ) : issue.error ? (
        <div className="toast toast-error">
          <span>⚠️</span>
          <span>{issue.error}</span>
        </div>
      ) : (
        <>
          {/* Key metrics */}
          <div className="metric-grid">
            {issue.issue_id === 'transaction_log_growth' && (
              <>
                <MetricPill label="Log Size"      value={m.log_size_mb != null ? `${m.log_size_mb.toLocaleString()} MB` : null} />
                <MetricPill label="Used"          value={m.log_used_pct != null ? `${m.log_used_pct.toFixed(1)}%` : null} />
                <MetricPill label="Reclaimable"   value={m.reclaimable_mb != null ? `${m.reclaimable_mb.toLocaleString()} MB` : null} />
                <MetricPill label="VLF Count"     value={m.vlf_count} />
                <MetricPill label="Recovery"      value={m.recovery_model} />
                <MetricPill label="Last Log Bkp"  value={m.last_log_backup} />
              </>
            )}
            {issue.issue_id === 'heap_clustering' && (
              <>
                <MetricPill
                  label="Heap Tables"
                  value={m.heap_count}
                  onClick={objs.length ? () => setShowHeaps(v => !v) : undefined}
                  active={showHeaps}
                />
                <MetricPill label="Total Size"   value={m.total_size_mb != null ? `${m.total_size_mb.toLocaleString()} MB` : null} />
              </>
            )}
            {issue.issue_id === 'string_storage' && (
              <MetricPill label="Flagged Cols" value={m.flagged_columns} />
            )}
            {issue.issue_id === 'unused_indexes' && (
              <>
                <MetricPill label="Candidates"   value={m.candidate_count} />
                <MetricPill label="Wasted Space" value={m.wasted_space_mb != null ? `${m.wasted_space_mb.toLocaleString()} MB` : null} />
                <MetricPill label="Confidence"   value={m.confidence} />
                <MetricPill label="Uptime (days)" value={m.days_since_restart} />
              </>
            )}
            {issue.issue_id === 'ghost_pages' && (
              <>
                <MetricPill label="Indexes"       value={m.affected_indexes} />
                <MetricPill label="Ghost Records"  value={m.total_ghost_records?.toLocaleString()} />
              </>
            )}
            {issue.issue_id === 'index_fragmentation' && (
              <>
                <MetricPill label="Fragmented"  value={m.fragmented_indexes} />
                <MetricPill label="Reorganize"  value={m.reorganize_count} />
                <MetricPill label="Rebuild"     value={m.rebuild_count} />
                <MetricPill label="Worst Frag." value={m.max_fragmentation_pct != null ? `${m.max_fragmentation_pct}%` : null} />
                <MetricPill label="Total Size"  value={m.total_size_mb != null ? `${m.total_size_mb.toLocaleString()} MB` : null} />
              </>
            )}
            {issue.issue_id === 'data_file_reclaim' && (
              <>
                <MetricPill label="Data Files"   value={m.data_files} />
                <MetricPill label="Actionable"   value={m.actionable_files} />
                <MetricPill label="Reclaimable"  value={m.reclaimable_mb != null ? `${m.reclaimable_mb.toLocaleString()} MB` : null} />
              </>
            )}
            {issue.issue_id === 'blank_string_contamination' && (
              <>
                <MetricPill label="Flagged Cols"  value={m.flagged_columns} />
                <MetricPill label="Blank Values"  value={m.total_blank_values?.toLocaleString()} />
                <MetricPill label="Auto-fixable"  value={m.fixable_columns} />
              </>
            )}
            {issue.issue_id === 'shadow_tables' && (
              <>
                <MetricPill label="Candidates"  value={m.candidate_count} />
                <MetricPill label="Total Size"  value={m.total_size_mb != null ? `${m.total_size_mb.toLocaleString()} MB` : null} />
              </>
            )}
            {issue.issue_id === 'inappropriate_datatypes' && (
              <>
                <MetricPill label="FLOAT/REAL Cols" value={m.float_columns} />
                <MetricPill label="Identifier-like" value={m.identifier_like} />
              </>
            )}
            {issue.issue_id === 'archival_candidates' && (
              <>
                <MetricPill label="Candidates"    value={m.total_candidates} />
                <MetricPill label="Very High"     value={m.very_high} />
                <MetricPill label="High"          value={m.high} />
                <MetricPill label="Validate"      value={m.requires_validation} />
                <MetricPill label="Could Not Analyze" value={m.could_not_analyze} />
              </>
            )}
            {issue.issue_id === 'missing_indexes' && (
              <>
                <MetricPill label="Suggestions" value={m.suggestion_count} />
                <MetricPill label="Top Impact"  value={m.top_impact_score?.toLocaleString()} />
                <MetricPill label="Confidence"  value={m.confidence} />
              </>
            )}
            {issue.issue_id === 'stale_statistics' && (
              <>
                <MetricPill label="Stale Stats" value={m.stale_count?.toLocaleString()} />
                <MetricPill label="Tables"      value={m.affected_tables} />
              </>
            )}
            {issue.issue_id === 'duplicate_indexes' && (
              <>
                <MetricPill label="Redundant"    value={m.redundant_count} />
                <MetricPill label="Exact Dupes"  value={m.exact_duplicates} />
                <MetricPill label="Wasted Space" value={m.wasted_space_mb != null ? `${m.wasted_space_mb.toLocaleString()} MB` : null} />
              </>
            )}
            {issue.issue_id === 'security_audit' && (
              <>
                <MetricPill label="Findings"    value={m.finding_count} />
                <MetricPill label="High Risk"   value={m.high_risk} />
                <MetricPill label="Medium Risk" value={m.medium_risk} />
              </>
            )}
            {issue.issue_id === 'adhoc_plan_cache' && (
              <>
                <MetricPill label="Single-Use Plans" value={m.total_single_use_plans?.toLocaleString()} />
                <MetricPill label="Wasted Cache" value={m.wasted_cache_mb != null ? `${m.wasted_cache_mb.toLocaleString()} MB` : null} />
              </>
            )}
          </div>

          {/* Recommendation / Decision */}
          {issue.recovery_decision_required ? (
            <div style={{
              background: 'var(--accent-subtle)', border: '1px solid rgba(59,130,246,0.3)',
              borderRadius: 'var(--radius-sm)', padding: '1rem',
              display: 'flex', flexDirection: 'column', gap: '1rem'
            }}>
              <div>
                <div className="label" style={{ marginBottom: '0.4rem', color: 'var(--text-accent)' }}>Decision Required</div>
                <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                  {issue.explanation}
                </p>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                {issue.options.map(opt => (
                  <label key={opt.id} style={{ display: 'flex', alignItems: 'flex-start', gap: '0.75rem', cursor: 'pointer', padding: '0.5rem', background: recoveryChoice === opt.id ? 'var(--accent-subtle)' : 'transparent', borderRadius: '4px', border: recoveryChoice === opt.id ? '1px solid var(--accent)' : '1px solid transparent' }}>
                    <input
                      type="radio"
                      name={`recovery_choice_${issue.issue_id}`}
                      value={opt.id}
                      checked={recoveryChoice === opt.id}
                      onChange={() => onChoiceChange(opt.id)}
                      style={{ marginTop: '3px', accentColor: 'var(--accent)' }}
                    />
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                        <span style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-primary)' }}>{opt.label}</span>
                        {opt.id === 'switch_simple' && (
                          <span className="badge badge-high" style={{ fontSize: '0.65rem' }}>Permanent change</span>
                        )}
                      </div>
                      <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)', lineHeight: 1.4 }}>{opt.consequence}</span>
                    </div>
                  </label>
                ))}
              </div>
            </div>
          ) : (
            <div style={{
              background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)',
              borderRadius: 'var(--radius-sm)', padding: '0.75rem 1rem',
            }}>
              <div className="label" style={{ marginBottom: '0.4rem' }}>Recommended Action</div>
              <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                {issue.recommended_action}
              </p>
            </div>
          )}

          {/* Per-issue detail panels */}
          {issue.issue_id === 'heap_clustering' && showHeaps && objs.length > 0 && (
            <SimpleObjectTable objects={objs} filename="heap-tables" columns={[
              { key: 'tc', label: 'Table', render: o => `${o.schema}.${o.table}` },
              { key: 'row_count', label: 'Rows', render: o => fmtNum(o.row_count) },
              { key: 'size_mb', label: 'Size', render: o => o.size_mb != null ? `${fmtNum(o.size_mb)} MB` : '—' },
              { key: 'candidate_key', label: 'Clustering Key', render: o => o.candidate_key },
            ]} />
          )}
          {issue.issue_id === 'shadow_tables' && objs.length > 0 && (
            <ShadowQuarantinePanel objects={objs} />
          )}
          {issue.issue_id === 'blank_string_contamination' && objs.length > 0 && (
            <SimpleObjectTable objects={objs} filename="blank-string-columns" columns={[
              { key: 'tc', label: 'Table.Column', render: o => `${o.table}.${o.column}` },
              { key: 'blank_or_spaces', label: 'Blank/Spaces', render: o => fmtNum(o.blank_or_spaces) },
              { key: 'total_rows', label: 'Total Rows', render: o => fmtNum(o.total_rows) },
              { key: 'fix', label: 'Auto-fix', render: o => o.eligible ? '✅ nullable' : '⛔ NOT NULL' },
            ]} />
          )}
          {issue.issue_id === 'inappropriate_datatypes' && objs.length > 0 && (
            <SimpleObjectTable objects={objs} filename="inappropriate-datatypes" columns={[
              { key: 'tc', label: 'Table.Column', render: o => `${o.table}.${o.column}` },
              { key: 'data_type', label: 'Type' },
              { key: 'nw', label: 'Decimals in sample', render: o => o.non_whole_count == null ? '?' : fmtNum(o.non_whole_count) },
              { key: 'id', label: 'Looks like ID', render: o => o.looks_like_identifier ? 'Yes' : 'No' },
            ]} />
          )}
          {issue.issue_id === 'index_fragmentation' && objs.length > 0 && (
            <SimpleObjectTable objects={objs} filename="fragmented-indexes" columns={[
              { key: 'idx', label: 'Index', render: o => `${o.schema}.${o.table} · ${o.index}` },
              { key: 'frag', label: 'Frag %', render: o => `${o.fragmentation_pct}%` },
              { key: 'size_mb', label: 'Size', render: o => o.size_mb != null ? `${fmtNum(o.size_mb)} MB` : '—' },
              { key: 'recommended_op', label: 'Action', render: o => o.recommended_op },
            ]} />
          )}
          {issue.issue_id === 'archival_candidates' && (
            <ArchivalCandidatesPanel issue={issue} />
          )}
          {issue.issue_id === 'data_file_reclaim' && (
            <DataFileReclaimPanel issue={issue} />
          )}
          {issue.issue_id === 'missing_indexes' && objs.length > 0 && (
            <SimpleObjectTable objects={objs} filename="missing-indexes" columns={[
              { key: 'tbl', label: 'Table', render: o => `${o.schema}.${o.table}` },
              { key: 'impact_score', label: 'Impact', render: o => fmtNum(o.impact_score) },
              { key: 'avg_impact_pct', label: 'Avg %', render: o => `${o.avg_impact_pct}%` },
              { key: 'key_columns', label: 'Key Columns' },
              { key: 'included_columns', label: 'Included' },
              { key: 'create_script', label: 'CREATE script' },
            ]} />
          )}
          {issue.issue_id === 'stale_statistics' && objs.length > 0 && (
            <SimpleObjectTable objects={objs} filename="stale-statistics" columns={[
              { key: 'tbl', label: 'Table', render: o => `${o.schema}.${o.table}` },
              { key: 'statistic', label: 'Statistic' },
              { key: 'last_updated', label: 'Last Updated' },
              { key: 'rows', label: 'Rows', render: o => fmtNum(o.rows) },
              { key: 'rows_modified', label: 'Modified', render: o => fmtNum(o.rows_modified) },
              { key: 'modified_pct', label: 'Mod %', render: o => `${o.modified_pct}%` },
            ]} />
          )}
          {issue.issue_id === 'duplicate_indexes' && objs.length > 0 && (
            <SimpleObjectTable objects={objs} filename="duplicate-indexes" columns={[
              { key: 'idx', label: 'Index', render: o => `${o.schema}.${o.table} · ${o.index}` },
              { key: 'kind', label: 'Kind' },
              { key: 'key_columns', label: 'Key Columns' },
              { key: 'redundant_with', label: 'Redundant With' },
              { key: 'size_mb', label: 'Size', render: o => `${fmtNum(o.size_mb)} MB` },
              { key: 'drop_script', label: 'DROP script' },
            ]} />
          )}
          {issue.issue_id === 'security_audit' && objs.length > 0 && (
            <SimpleObjectTable objects={objs} filename="security-audit" columns={[
              { key: 'risk', label: 'Risk', render: o => (
                <span className={`badge ${o.risk === 'High' ? 'badge-high' : o.risk === 'Medium' ? 'badge-medium' : 'badge-low'}`}>{o.risk}</span>
              ) },
              { key: 'category', label: 'Category' },
              { key: 'finding', label: 'Finding' },
              { key: 'detail', label: 'Detail' },
            ]} />
          )}
          {issue.issue_id === 'adhoc_plan_cache' && objs.length > 0 && (
            <>
              <div className="toast toast-warning" style={{ fontSize: '0.78rem' }}>
                <span>🧠</span>
                <span>Raw text interpolation (e.g. hardcoded values baked into a <code>WHERE</code> clause
                  instead of parameters) forces the engine to compile a <strong>separate execution plan</strong>
                  {' '}for every literal variation. These single-use plans choke system memory — starving the
                  buffer pool of data pages and forcing more physical reads (I/O bottlenecks).</span>
              </div>
              <SimpleObjectTable objects={objs} filename="adhoc-plan-cache" columns={[
                { key: 'plan_type', label: 'Cache Plan Type' },
                { key: 'single_use_plans', label: 'Single-Use Plans', render: o => fmtNum(o.single_use_plans) },
                { key: 'wasted_mb', label: 'Wasted Cache (MB)', render: o => fmtNum(o.wasted_mb) },
              ]} />
              <ScriptViewer scripts={m.remediation_scripts || []} />
            </>
          )}
          {issue.issue_id === 'storage_redundancy' && (
            <StorageRedundancyPanel />
          )}
          {issue.issue_id === 'table_intelligence' && (
            <TableIntelligencePanel />
          )}
          {issue.issue_id === 'data_compression' && (
            <DataCompressionPanel />
          )}

          {/* Impact */}
          {issue.estimated_impact && issue.estimated_impact !== 'N/A' && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>💡 Estimated impact:</span>
              <span style={{ fontSize: '0.8rem', color: 'var(--severity-low)', fontWeight: 600 }}>
                {issue.estimated_impact}
              </span>
            </div>
          )}

          {/* Blocked reason */}
          {blockedMsg && (
            <div className="toast toast-warning">
              <span>⚠️</span>
              <span style={{ fontSize: '0.8rem' }}><strong>Fix blocked:</strong> {blockedMsg}</span>
            </div>
          )}

          {/* Analysis note */}
          {issue.analysis_note && (
            <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
              ℹ️ {issue.analysis_note}
            </p>
          )}
        </>
      )}
    </div>
  )
}
