import { useState, useEffect, useRef } from 'react'
import { apiAnalyze, apiExecute } from '../api.js'
import IssueTile from './IssueTile.jsx'
import IssueModal from './IssueModal.jsx'

// Canonical display order + names (match the backend ISSUE_NAME values).
const CHECK_ORDER = [
  'transaction_log_growth', 'heap_clustering', 'string_storage',
  'unused_indexes', 'ghost_pages', 'index_fragmentation',
  'blank_string_contamination',
  'shadow_tables', 'inappropriate_datatypes', 'archival_candidates',
  'data_file_reclaim',
  'missing_indexes', 'stale_statistics', 'duplicate_indexes', 'security_audit',
  'adhoc_plan_cache',
  'data_compression', 'storage_redundancy', 'table_intelligence',
]

// Synthetic, always-present tiles for the on-demand features. These are NOT part
// of the /analyze batch — they run only when the user opens their modal and hits
// "Run", via their own endpoints. Keeps the batch fast.
const STORAGE_REDUNDANCY_TILE = {
  issue_id: 'storage_redundancy',
  issue_name: 'AI Storage & Redundancy Analysis',
  severity: 'Low',
  executable: true,
  eligible_for_fix: false,
  affected_objects: [],
  current_metrics: {},
}
const TABLE_INTELLIGENCE_TILE = {
  issue_id: 'table_intelligence',
  issue_name: 'Table Intelligence',
  severity: 'Low',
  executable: true,
  eligible_for_fix: false,
  affected_objects: [],
  current_metrics: {},
}
const DATA_COMPRESSION_TILE = {
  issue_id: 'data_compression',
  issue_name: 'Data Compression Savings',
  severity: 'Low',
  executable: true,
  eligible_for_fix: false,
  affected_objects: [],
  current_metrics: {},
}
const SYNTHETIC_TILES = {
  storage_redundancy: STORAGE_REDUNDANCY_TILE,
  table_intelligence: TABLE_INTELLIGENCE_TILE,
  data_compression: DATA_COMPRESSION_TILE,
}

// Fast checks (metadata / sub-second) paint immediately. The heavy checks that
// physically sample or scan table data stream in afterwards so the screen is
// usable right away even against a large/remote database.
const FAST_CHECKS = [
  'transaction_log_growth', 'heap_clustering', 'unused_indexes', 'shadow_tables',
]
const SLOW_CHECKS = [
  'string_storage', 'ghost_pages', 'index_fragmentation',
  'blank_string_contamination', 'inappropriate_datatypes', 'archival_candidates',
  'data_file_reclaim',
  'missing_indexes', 'stale_statistics', 'duplicate_indexes', 'security_audit',
  'adhoc_plan_cache',
]

const ISSUE_NAMES = {
  transaction_log_growth:     'Unchecked Transaction Log (.ldf) Growth',
  heap_clustering:            'Clustered Index Conversion for Unordered Heaps',
  string_storage:             'Data Type String Storage Optimization',
  unused_indexes:             'High-Overhead Unused Index Audit & Purge',
  ghost_pages:                'Ghost Page Data Reconciliation',
  index_fragmentation:        'Fragmented Index Rebuild',
  blank_string_contamination: 'Blank String Bypass Contamination',
  shadow_tables:              'Structural Twin Tables & Shadow Copies',
  inappropriate_datatypes:    'Inappropriate Datatypes for Core Values',
  archival_candidates:        'Legacy Table Archival Candidate Detection',
  data_file_reclaim:          'Data File Space Reclamation',
  missing_indexes:            'Missing Index Recommendations',
  stale_statistics:           'Stale Statistics',
  duplicate_indexes:          'Duplicate & Overlapping Indexes',
  security_audit:             'Security Posture & PII Audit',
  adhoc_plan_cache:           'Ad-Hoc Workload Analyzer',
  data_compression:           'Data Compression Savings',
  storage_redundancy:         'AI Storage & Redundancy Analysis',
  table_intelligence:         'Table Intelligence',
}

// Merge issue lists by id and return them in canonical order.
function mergeIssues(...lists) {
  const byId = {}
  for (const list of lists) for (const iss of (list || [])) byId[iss.issue_id] = iss
  return CHECK_ORDER.filter(id => byId[id]).map(id => byId[id])
}

const SEVERITY_DOTS = { High: '🔴', Medium: '🟠', Low: '🟢' }

export default function AnalysisScreen({ session, analysisData, executedIds = new Set(), onAnalyzed, onExecute }) {
  const [loading, setLoading] = useState(false)        // true only during the fast phase
  const [pending, setPending] = useState(new Set())    // checks still streaming in
  const [error, setError] = useState(null)
  const [issues, setIssues] = useState(analysisData?.issues || [])
  const [selected, setSelected] = useState(new Set())
  const [recoveryChoices, setRecoveryChoices] = useState({})
  const [executing, setExecuting] = useState(false)
  const [expandedId, setExpandedId] = useState(null)   // tile opened into the modal
  const [modalOrigin, setModalOrigin] = useState(null) // click point for the open animation

  // Auto-run analysis on mount. The ref guard prevents React StrictMode (dev)
  // from firing the effect twice and triggering two concurrent full analyses.
  const didRun = useRef(false)
  useEffect(() => {
    if (!analysisData && !didRun.current) {
      didRun.current = true
      runAnalysis()
    }
  }, [])

  const runAnalysis = async () => {
    setLoading(true)
    setError(null)
    setSelected(new Set())
    setRecoveryChoices({})
    setIssues([])
    setPending(new Set(SLOW_CHECKS))
    try {
      // Phase 1 — fast checks; render as soon as these return.
      const fast = await apiAnalyze(FAST_CHECKS)
      setIssues(fast.issues)
      onAnalyzed(fast)
      setLoading(false)

      // Phase 2 — heavy checks, loaded in the background and merged in.
      apiAnalyze(SLOW_CHECKS)
        .then(slow => {
          const merged = mergeIssues(fast.issues, slow.issues)
          setIssues(merged)
          onAnalyzed({ ...fast, issues: merged })
        })
        .catch(err => {
          // Surface the failure on the affected cards rather than the whole page.
          const errored = SLOW_CHECKS.map(id => ({
            issue_id: id, issue_name: ISSUE_NAMES[id], severity: 'Low',
            affected_objects: [], current_metrics: {}, recommended_action: '',
            estimated_impact: 'N/A', executable: false, eligible_for_fix: false,
            error: err.message,
          }))
          const merged = mergeIssues(fast.issues, errored)
          setIssues(merged)
          onAnalyzed({ ...fast, issues: merged })
        })
        .finally(() => setPending(new Set()))
    } catch (err) {
      setError(err.message)
      setLoading(false)
      setPending(new Set())
    }
  }

  const toggleSelect = (issueId) => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(issueId) ? next.delete(issueId) : next.add(issueId)
      return next
    })
  }

  const handleChoiceChange = (issueId, choice) => {
    setRecoveryChoices(prev => ({ ...prev, [issueId]: choice }))
    // A non-skip radio choice implicitly selects the card; "skip" deselects it.
    if (choice !== 'skip') {
      setSelected(prev => new Set(prev).add(issueId))
    } else {
      setSelected(prev => {
        const next = new Set(prev)
        next.delete(issueId)
        return next
      })
    }
  }

  const openTile = (issueId, e) => {
    // Capture the click point so the modal appears to grow from the tile.
    if (e && typeof e.clientX === 'number' && e.clientX !== 0) {
      setModalOrigin({ x: e.clientX, y: e.clientY })
    } else {
      setModalOrigin(null) // keyboard activation → grow from center
    }
    setExpandedId(issueId)
  }

  const selectAllEligible = () => {
    // All non-shadow issues with a ready automated fix. Decision-required issues
    // are left out — they need an explicit per-issue choice in the modal.
    const ids = issues
      .filter(i => i.executable && i.issue_id !== 'shadow_tables' && i.eligible_for_fix
                   && !i.error && !executedIds.has(i.issue_id))
      .map(i => i.issue_id)
    setSelected(new Set(ids))
  }

  const handleRunSelected = async () => {
    if (eligibleSelected.length === 0) return
    setExecuting(true)
    const allResults = []
    try {
      for (const issueId of eligibleSelected) {
        const choice = recoveryChoices[issueId]
        const result = await apiExecute(issueId, choice)
        allResults.push(result)
      }
      onExecute(allResults)
    } catch (err) {
      setError(err.message)
    } finally {
      setExecuting(false)
    }
  }

  const eligibleSelected = [...selected].filter(id => {
    const i = issues.find(issue => issue.issue_id === id)
    if (!i) return false
    if (executedIds.has(id)) return false   // already remediated this session
    if (i.recovery_decision_required) {
      return recoveryChoices[id] && recoveryChoices[id] !== 'skip'
    }
    return i.eligible_for_fix
  })

  const busy = loading || pending.size > 0

  // Severity counts + a rough "potential reclaimable" estimate (only metrics that
  // clearly represent reclaimable disk space are summed).
  const sevCounts = { High: 0, Medium: 0, Low: 0 }
  let reclaimableMb = 0
  for (const i of issues) {
    if (sevCounts[i.severity] != null) sevCounts[i.severity] += 1
    const m = i.current_metrics || {}
    reclaimableMb += (m.reclaimable_mb || 0)
    reclaimableMb += (m.wasted_space_mb || 0)
    if (i.issue_id === 'heap_clustering') reclaimableMb += (m.total_size_mb || 0)
  }

  // Resolve a tile id to its issue object, injecting the synthetic AI tile.
  const resolveIssue = (id) =>
    SYNTHETIC_TILES[id] || issues.find(i => i.issue_id === id)

  const expandedIssue = expandedId ? resolveIssue(expandedId) : null

  const runLabel = executing
    ? <><div className="spinner" style={{ width: 16, height: 16, borderTopColor: 'white' }} /> Running…</>
    : eligibleSelected.length > 0
      ? `⚡ Run ${eligibleSelected.length} Selected Optimization${eligibleSelected.length > 1 ? 's' : ''}`
      : 'No eligible fix selected'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
      {/* Page header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '1rem' }}>
        <div>
          <h1 style={{ fontSize: '1.6rem', marginBottom: '0.25rem' }}>
            Storage Analysis
            <span className="text-gradient" style={{ marginLeft: '0.5rem' }}>
              {session?.database}
            </span>
          </h1>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>
            Read-only analysis — no data, schema, or settings are changed.
          </p>
        </div>
        <button
          id="btn-rerun-analysis"
          className="btn btn-ghost"
          onClick={runAnalysis}
          disabled={busy}
        >
          {busy ? <><div className="spinner" style={{ width: 14, height: 14 }} /> Analysing…</> : '↻ Re-run Analysis'}
        </button>
      </div>

      {error && (
        <div className="toast toast-error" id="analysis-error">
          <span>⚠️</span>
          <span>{error}</span>
        </div>
      )}

      {/* Severity summary bar */}
      {issues.length > 0 && (
        <div className="summary-bar">
          <div className="summary-counts">
            {['High', 'Medium', 'Low'].map(sev => (
              <span key={sev} className="summary-stat">
                <span style={{ fontSize: '0.9rem' }}>{SEVERITY_DOTS[sev]}</span>
                <strong>{sevCounts[sev]}</strong> {sev}
              </span>
            ))}
            {pending.size > 0 && (
              <span className="summary-stat" style={{ color: 'var(--text-muted)' }}>
                <div className="spinner" style={{ width: 12, height: 12 }} /> {pending.size} still scanning…
              </span>
            )}
          </div>
          <div className="summary-counts">
            {reclaimableMb > 0 && (
              <span className="summary-stat" title="Rough estimate of clearly-reclaimable disk space across checks">
                ~{Math.round(reclaimableMb).toLocaleString()} MB potential
              </span>
            )}
            {executedIds.size > 0 && (
              <span className="summary-stat" style={{ color: 'var(--success)' }}>
                ✅ <strong>{executedIds.size}</strong> remediated
              </span>
            )}
            <span className="summary-stat" style={{ color: 'var(--text-accent)' }}>
              <strong>{selected.size}</strong> selected
            </span>
          </div>
        </div>
      )}

      {/* Tile grid */}
      <div className="tile-grid">
        {loading && issues.length === 0 ? (
          CHECK_ORDER.map(id => <IssueTile key={id} loading />)
        ) : (
          CHECK_ORDER.map(id => {
            const issue = resolveIssue(id)
            if (issue) {
              return (
                <IssueTile
                  key={id}
                  issue={issue}
                  checked={selected.has(id)}
                  remediated={executedIds.has(id)}
                  onToggle={() => toggleSelect(id)}
                  onOpen={openTile}
                />
              )
            }
            if (pending.has(id)) return <IssueTile key={id} loading />
            return null
          })
        )}
      </div>

      {/* Sticky action bar */}
      {issues.length > 0 && (
        <div className="sticky-action-bar">
          <span style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            <strong style={{ color: 'var(--text-primary)' }}>{selected.size}</strong> selected
            {eligibleSelected.length !== selected.size && (
              <span style={{ color: 'var(--text-muted)' }}> · {eligibleSelected.length} eligible</span>
            )}
          </span>
          <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
            <button className="btn btn-ghost" onClick={selectAllEligible} disabled={busy || executing}>
              Select all eligible
            </button>
            <button
              id="btn-run-selected"
              className="btn btn-danger btn-lg"
              disabled={eligibleSelected.length === 0 || executing}
              onClick={handleRunSelected}
            >
              {runLabel}
            </button>
          </div>
        </div>
      )}

      {/* Expanded tile → modal with full IssueCard detail */}
      {expandedIssue && (
        <IssueModal
          issue={expandedIssue}
          origin={modalOrigin}
          onClose={() => setExpandedId(null)}
          checked={selected.has(expandedIssue.issue_id)}
          remediated={executedIds.has(expandedIssue.issue_id)}
          onToggle={() => toggleSelect(expandedIssue.issue_id)}
          recoveryChoice={recoveryChoices[expandedIssue.issue_id] || ''}
          onChoiceChange={(choice) => handleChoiceChange(expandedIssue.issue_id, choice)}
        />
      )}
    </div>
  )
}
