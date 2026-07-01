/**
 * IssueModal.jsx
 * Overlay + centered panel that wraps the full IssueCard. Renders every detail
 * section unchanged (metrics, recommendation/decision radios, per-issue detail
 * panels, impact, analysis note). Handles ESC / click-outside / focus-trap and
 * locks body scroll while open. The panel scales open from the clicked tile's
 * position via an inline transform-origin.
 */
import { useEffect, useRef } from 'react'
import IssueCard from './IssueCard.jsx'

export default function IssueModal({ issue, origin, onClose, checked, onToggle, recoveryChoice, onChoiceChange }) {
  const panelRef = useRef(null)
  // Remember what was focused before opening so we can restore it on close.
  const prevFocus = useRef(null)

  useEffect(() => {
    prevFocus.current = document.activeElement

    // Lock background scroll.
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    // Focus the panel for keyboard users.
    panelRef.current?.focus()

    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
        return
      }
      if (e.key === 'Tab') {
        // Simple focus trap within the panel.
        const focusable = panelRef.current?.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])'
        )
        if (!focusable || focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKeyDown)

    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = prevOverflow
      // Restore focus to the tile that opened the modal.
      if (prevFocus.current && prevFocus.current.focus) prevFocus.current.focus()
    }
  }, [onClose])

  // transform-origin from the click point so the panel appears to grow out of the tile.
  const transformOrigin = origin
    ? `${origin.x}px ${origin.y}px`
    : 'center center'

  return (
    <div
      className="modal-overlay"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        className="modal-panel"
        role="dialog"
        aria-modal="true"
        aria-label={issue.issue_name}
        ref={panelRef}
        tabIndex={-1}
        style={{ transformOrigin }}
      >
        <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        <div className="modal-scroll">
          <IssueCard
            issue={issue}
            checked={checked}
            onToggle={onToggle}
            recoveryChoice={recoveryChoice}
            onChoiceChange={onChoiceChange}
          />
        </div>
      </div>
    </div>
  )
}
