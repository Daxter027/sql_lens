/**
 * TableExport.jsx
 * Reusable "Copy + Excel" control shown above a data table.
 *
 * Props:
 *   - filename : base name for the downloaded .xlsx (no extension)
 *   - headers  : string[]  column headers
 *   - rows     : any[][]    row-major cell values (RAW values — numbers stay
 *                numeric, dates as-is — so Excel treats them correctly)
 *   - sheetName: optional worksheet name (default "Data")
 *   - count    : optional row count to show as a hint
 *
 * The xlsx library is imported dynamically so it is code-split into its own lazy
 * chunk and never weighs down the initial page load.
 */
import { useState } from 'react'

const cell = (v) => {
  if (v === null || v === undefined) return ''
  if (typeof v === 'boolean') return v ? 'Yes' : 'No'
  return v            // numbers stay numbers; strings stay strings
}

const esc = (s) => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

// Date-like text ("2024-01-31" or "2024-01-31 12:00:00"). Excel auto-converts
// such text to a real date on paste and, if the column is narrow, shows "######".
const DATE_RE = /^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$/

// Build an HTML table for the clipboard. Date cells get mso-number-format:'\@'
// (Excel "Text" format) so Excel keeps them as text — text overflows instead of
// showing "######". Numbers are left alone so they stay numeric/summable.
const buildHtml = (headers, rows) => {
  const td = (v) => {
    const s = v == null ? '' : String(v)
    const style = DATE_RE.test(s) ? ` style="mso-number-format:'\\@'"` : ''
    return `<td${style}>${esc(s)}</td>`
  }
  const head = `<tr>${headers.map(h => `<th>${esc(h)}</th>`).join('')}</tr>`
  const body = rows.map(r => `<tr>${r.map(td).join('')}</tr>`).join('')
  return `<table>${head}${body}</table>`
}

// Copy rich HTML (+ its plain-text form) via a hidden selection + execCommand,
// which works in non-secure contexts (plain-HTTP LAN). Selecting a table also
// yields tab-separated text/plain automatically, so non-Excel targets still work.
const legacyCopyRich = (html) => {
  try {
    const div = document.createElement('div')
    div.setAttribute('contenteditable', 'true')
    div.style.position = 'fixed'
    div.style.top = '-9999px'
    div.style.opacity = '0'
    div.innerHTML = html
    document.body.appendChild(div)
    const range = document.createRange()
    range.selectNodeContents(div)
    const sel = window.getSelection()
    sel.removeAllRanges()
    sel.addRange(range)
    const ok = document.execCommand('copy')
    sel.removeAllRanges()
    document.body.removeChild(div)
    return ok
  } catch {
    return false
  }
}

// Last-resort plain-text-only copy (non-Excel targets, or if rich copy fails).
const legacyCopy = (text) => {
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    ta.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

// Copy arbitrary plain text, with the same secure-context fallback used for tables.
export async function copyPlainText(text) {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* fall through */ }
  return legacyCopy(text)
}

// Small standalone copy button for a text blob (e.g. a remediation script).
export function CopyButton({ text, label = '⧉ Copy', copiedLabel = '✓ Copied' }) {
  const [copied, setCopied] = useState(false)
  const onClick = async () => {
    if (await copyPlainText(text)) { setCopied(true); setTimeout(() => setCopied(false), 1500) }
  }
  return (
    <button type="button" className="btn btn-ghost" style={{ padding: '3px 10px', fontSize: '0.72rem' }} onClick={onClick}>
      {copied ? copiedLabel : label}
    </button>
  )
}

export default function TableExport({ filename, headers, rows, sheetName = 'Data', count }) {
  const [copied, setCopied] = useState(false)
  const [err, setErr] = useState(false)
  if (!rows || rows.length === 0) return null

  const matrix = () => [headers, ...rows.map(r => r.map(cell))]

  const flashCopied = () => { setCopied(true); setTimeout(() => setCopied(false), 1500) }

  const copy = async () => {
    setErr(false)
    const m = matrix()
    const tsv = m.map(r => r.map(c => String(c ?? '')).join('\t')).join('\r\n')
    const html = buildHtml(m[0], m.slice(1))
    // Prefer the async Clipboard API with BOTH html + text (needs a secure
    // context: HTTPS/localhost). Excel picks the html flavor → dates paste as
    // text, no "######".
    try {
      if (window.isSecureContext && navigator.clipboard && window.ClipboardItem) {
        await navigator.clipboard.write([new window.ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([tsv], { type: 'text/plain' }),
        })])
        flashCopied()
        return
      }
    } catch { /* fall through to legacy path */ }
    // Plain-HTTP LAN: copy the rich HTML via execCommand; if that fails, plain text.
    if (legacyCopyRich(html) || legacyCopy(tsv)) flashCopied()
    else { setErr(true); setTimeout(() => setErr(false), 2500) }
  }

  const download = async () => {
    setErr(false)
    try {
      const XLSX = await import('xlsx')
      const m = matrix()
      const ws = XLSX.utils.aoa_to_sheet(m)
      // Auto-size each column to its widest value so dates/numbers don't render
      // as "######" on open. Bounded to keep very long text reasonable.
      ws['!cols'] = m[0].map((_, ci) => {
        const widest = m.reduce((mx, row) => Math.max(mx, String(row[ci] ?? '').length), 0)
        return { wch: Math.min(Math.max(widest + 1, 8), 45) }
      })
      const wb = XLSX.utils.book_new()
      XLSX.utils.book_append_sheet(wb, ws, sheetName.slice(0, 31))   // Excel sheet-name limit
      XLSX.writeFile(wb, `${filename}.xlsx`)
    } catch {
      setErr(true); setTimeout(() => setErr(false), 2500)
    }
  }

  const btn = { padding: '3px 10px', fontSize: '0.72rem' }
  return (
    <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', flexWrap: 'wrap' }}>
      <button type="button" className="btn btn-ghost" style={btn} onClick={copy}
        title="Copy for Excel/Sheets — paste straight into cells (dates paste as text, no ######)">
        {copied ? '✓ Copied' : '⧉ Copy'}
      </button>
      <button type="button" className="btn btn-ghost" style={btn} onClick={download}
        title="Download as an .xlsx spreadsheet">
        ⭳ Excel
      </button>
      {count != null && (
        <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>{count.toLocaleString()} rows</span>
      )}
      {err && <span style={{ fontSize: '0.7rem', color: 'var(--error)' }}>Export failed</span>}
    </div>
  )
}
