/**
 * IssueTile.jsx
 * Compact, selectable tile for the analysis grid. Clicking the body opens the
 * full IssueCard in a modal; the corner checkbox drives selection without
 * opening the modal. Status logic mirrors IssueCard so the two stay in sync.
 */

const SEVERITY_META = {
  High:   { cls: 'badge-high',   icon: '🔴', desc: 'High',   accent: 'var(--severity-high)' },
  Medium: { cls: 'badge-medium', icon: '🟠', desc: 'Medium', accent: 'var(--severity-medium)' },
  Low:    { cls: 'badge-low',    icon: '🟢', desc: 'Low',    accent: 'var(--severity-low)' },
  None:   { cls: 'badge-none',   icon: '⬜', desc: 'None',   accent: 'var(--severity-none)' },
}

const fmtMb  = (v) => (v != null ? `${Number(v).toLocaleString()} MB` : null)
const fmtNum = (v) => (typeof v === 'number' ? v.toLocaleString() : v)

// Up to two headline {label, value} pairs per issue, drawn from current_metrics.
// Keys match the metric pills already mapped in IssueCard.
function tileHeadline(issue) {
  const m = issue.current_metrics || {}
  const pick = (label, value) => (value != null && value !== '' ? { label, value } : null)
  let pairs = []
  switch (issue.issue_id) {
    case 'transaction_log_growth':
      pairs = [pick('Reclaimable', fmtMb(m.reclaimable_mb)), pick('Log Used', m.log_used_pct != null ? `${m.log_used_pct.toFixed(1)}%` : null)]
      break
    case 'heap_clustering':
      pairs = [pick('Heap Tables', fmtNum(m.heap_count)), pick('Total Size', fmtMb(m.total_size_mb))]
      break
    case 'string_storage':
      pairs = [pick('Flagged Cols', fmtNum(m.flagged_columns))]
      break
    case 'unused_indexes':
      pairs = [pick('Wasted Space', fmtMb(m.wasted_space_mb)), pick('Confidence', m.confidence)]
      break
    case 'ghost_pages':
      pairs = [pick('Ghost Records', fmtNum(m.total_ghost_records)), pick('Indexes', fmtNum(m.affected_indexes))]
      break
    case 'index_fragmentation':
      pairs = [pick('Frag. Indexes', fmtNum(m.fragmented_indexes)),
               pick('Worst', m.max_fragmentation_pct != null ? `${m.max_fragmentation_pct}%` : null)]
      break
    case 'blank_string_contamination':
      pairs = [pick('Flagged Cols', fmtNum(m.flagged_columns)), pick('Blank Values', fmtNum(m.total_blank_values))]
      break
    case 'shadow_tables':
      pairs = [pick('Candidates', fmtNum(m.candidate_count)), pick('Total Size', fmtMb(m.total_size_mb))]
      break
    case 'inappropriate_datatypes':
      pairs = [pick('FLOAT/REAL', fmtNum(m.float_columns)), pick('Identifier-like', fmtNum(m.identifier_like))]
      break
    case 'archival_candidates':
      pairs = [pick('Candidates', fmtNum(m.total_candidates)),
               pick('High-conf', fmtNum((m.very_high || 0) + (m.high || 0)))]
      break
    case 'data_file_reclaim':
      pairs = [pick('Reclaimable', fmtMb(m.reclaimable_mb)),
               pick('Files', fmtNum(m.actionable_files))]
      break
    case 'missing_indexes':
      pairs = [pick('Suggestions', fmtNum(m.suggestion_count)),
               pick('Confidence', m.confidence)]
      break
    case 'stale_statistics':
      pairs = [pick('Stale Stats', fmtNum(m.stale_count)),
               pick('Tables', fmtNum(m.affected_tables))]
      break
    case 'duplicate_indexes':
      pairs = [pick('Redundant', fmtNum(m.redundant_count)),
               pick('Wasted', fmtMb(m.wasted_space_mb))]
      break
    case 'security_audit':
      pairs = [pick('Findings', fmtNum(m.finding_count)),
               pick('High Risk', fmtNum(m.high_risk))]
      break
    case 'adhoc_plan_cache':
      pairs = [pick('Single-Use Plans', fmtNum(m.total_single_use_plans)),
               pick('Wasted Cache', fmtMb(m.wasted_cache_mb))]
      break
    default:
      pairs = []
  }
  return pairs.filter(Boolean).slice(0, 2)
}

export default function IssueTile({ issue, checked, remediated, onToggle, onOpen, loading }) {
  if (loading) {
    return (
      <div className="issue-tile is-skeleton" aria-hidden="true">
        <div className="skeleton" style={{ height: 14, width: '40%', borderRadius: 6 }} />
        <div className="skeleton" style={{ height: 16, width: '85%', borderRadius: 6, marginTop: 10 }} />
        <div className="skeleton" style={{ height: 44, width: '100%', borderRadius: 8, marginTop: 14 }} />
      </div>
    )
  }

  const meta = SEVERITY_META[issue.severity] || SEVERITY_META.None
  const isStorageAI = issue.issue_id === 'storage_redundancy'    // on-demand AI feature
  const isTableIntel = issue.issue_id === 'table_intelligence'   // on-demand per-table profiler
  const isCompression = issue.issue_id === 'data_compression'    // on-demand compression estimate
  const isInfoTile = isStorageAI || isTableIntel || isCompression  // on-demand, not a finding
  const isNoIssue = !isInfoTile && !issue.affected_objects?.length && issue.severity === 'Low'
  const isShadow = issue.issue_id === 'shadow_tables'
  // Executable only via their own per-object panel buttons — no batch checkbox.
  const isPanelExec = isShadow || issue.issue_id === 'data_file_reclaim' || isInfoTile
  const canCheck = issue.executable && !isPanelExec
    && (issue.eligible_for_fix || issue.recovery_decision_required) && !issue.error
  const comingSoon = !issue.executable
  const headline = tileHeadline(issue)
  const isActive = checked || issue.recovery_decision_required

  const open = (e) => onOpen(issue.issue_id, e)
  const onKeyDown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(e) }
  }

  // Right-side status affordance — matches IssueCard's header logic.
  let statusEl
  if (remediated) {
    statusEl = <span className="badge badge-low">✅ Remediated</span>
  } else if (comingSoon || issue.error) {
    statusEl = <span className="tile-pill">Analysis only</span>
  } else if (isPanelExec) {
    statusEl = <span className="tile-pill">{
      isShadow ? 'Per-table actions'
        : isStorageAI ? '🤖 AI report'
        : isTableIntel ? '📊 Table profiles'
        : isCompression ? '🗜️ Estimate'
        : 'File actions'
    }</span>
  } else if (issue.recovery_decision_required) {
    statusEl = <span style={{ fontSize: '0.72rem', color: 'var(--text-accent)', fontWeight: 600 }}>Action Required</span>
  } else {
    statusEl = (
      // Stop propagation so toggling selection doesn't open the modal.
      <label
        className="tile-check"
        onClick={(e) => e.stopPropagation()}
        style={{ cursor: canCheck ? 'pointer' : 'not-allowed' }}
      >
        <input
          id={`chk-${issue.issue_id}`}
          type="checkbox"
          checked={checked}
          disabled={!canCheck}
          onChange={canCheck ? onToggle : undefined}
          style={{ width: 18, height: 18, accentColor: 'var(--accent)', cursor: canCheck ? 'pointer' : 'not-allowed' }}
        />
        <span style={{ fontSize: '0.78rem', color: canCheck ? 'var(--text-secondary)' : 'var(--text-muted)' }}>Select</span>
      </label>
    )
  }

  return (
    <div
      className={`issue-tile${isActive ? ' is-selected' : ''}${isNoIssue ? ' is-muted' : ''}`}
      id={`issue-tile-${issue.issue_id}`}
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={onKeyDown}
      style={{ '--tile-accent': meta.accent }}
    >
      <div className="tile-head">
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
          <span className={`badge ${meta.cls}`}>{meta.icon} {meta.desc}</span>
          {issue.error && <span className="badge badge-high">⚠ Failed</span>}
        </div>
        {statusEl}
      </div>

      <h3 className="tile-title">{issue.issue_name}</h3>

      {headline.length > 0 ? (
        <div className="tile-metrics">
          {headline.map(({ label, value }) => (
            <div className="tile-metric" key={label}>
              <span className="tile-metric-value">{value}</span>
              <span className="tile-metric-label">{label}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="tile-sub">
          {isNoIssue ? '✅ No action needed'
            : isStorageAI ? 'Top 20% tables → AI report'
            : isTableIntel ? 'Every table profiled → metrics'
            : isCompression ? 'Estimate ROW/PAGE savings'
            : 'Open for details'}
        </p>
      )}

      <span className="tile-open-hint">View details →</span>
    </div>
  )
}
