import { useEffect, useRef } from 'react'
import './ConfirmDialog.css'

type ConfirmDialogProps = {
  open: boolean
  title: string
  message: string
  confirmLabel: string
  cancelLabel: string
  onConfirm: () => void
  onCancel: () => void
  danger?: boolean
  scrollMessage?: boolean
}

/** Small in-app confirmation surface shared by destructive actions. */
export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
  danger = false,
  scrollMessage = false,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return undefined
    cancelRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onCancel, open])

  if (!open) return null
  return (
    <div
      className="app-confirm-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel()
      }}
    >
      <section className="app-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="app-confirm-title">
        <header>
          <strong id="app-confirm-title">{title}</strong>
          <button type="button" onClick={onCancel} aria-label={cancelLabel}>×</button>
        </header>
        <p style={scrollMessage ? { maxHeight: '50vh', overflowY: 'auto', whiteSpace: 'pre-wrap' } : undefined}>{message}</p>
        <footer>
          <button ref={cancelRef} type="button" onClick={onCancel}>{cancelLabel}</button>
          <button type="button" className={danger ? 'is-danger' : ''} onClick={onConfirm}>{confirmLabel}</button>
        </footer>
      </section>
    </div>
  )
}
