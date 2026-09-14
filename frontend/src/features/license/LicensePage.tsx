import { useEffect, useRef, useState } from 'react'
import { licenseApi, type LicenseStatus } from './license.api'
import { localize, useLocale } from '@/app/i18n'
import './LicensePage.css'

type Props = {
  status: LicenseStatus
  gate?: boolean
  embedded?: boolean
  onStatusChange: (status: LicenseStatus) => void
}

function errorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error)
  try {
    const parsed = JSON.parse(raw)
    return parsed.detail || parsed.message || raw
  } catch {
    return raw
  }
}

export default function LicensePage({ status, gate = false, embedded = false, onStatusChange }: Props) {
  const { locale } = useLocale()
  const t = (vietnamese: string, english: string) => localize(locale, vietnamese, english)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (busy) return
    const timer = window.setTimeout(() => inputRef.current?.focus(), 80)
    return () => window.clearTimeout(timer)
  }, [busy, gate])

  async function activate() {
    if (!key.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      const next = await licenseApi.activate(key.trim())
      onStatusChange(next)
      setKey('')
    } catch (exc) {
      setError(errorText(exc))
    } finally {
      setBusy(false)
    }
  }

  async function deactivate() {
    if (busy || !status.configured) return
    if (!window.confirm(t('Huỷ kích hoạt key trên máy này?', 'Deactivate this key on this computer?'))) return
    setBusy(true)
    setError('')
    try {
      onStatusChange(await licenseApi.deactivate())
    } catch (exc) {
      setError(errorText(exc))
    } finally {
      setBusy(false)
    }
  }

  const expiry = status.expiresAt
    ? new Date(status.expiresAt).toLocaleString(locale === 'vi' ? 'vi-VN' : 'en-US')
    : status.remainingDay === -1 ? t('Không giới hạn', 'Unlimited') : '—'
  const stateTitle = status.valid ? t('Đã kích hoạt', 'Activated') : t('Chưa kích hoạt', 'Not activated')
  const stateMessage = status.message.trim() === stateTitle ? '' : status.message.trim()

  return (
    <main className={`license-page${gate ? ' license-gate' : ''}${embedded ? ' license-embedded' : ''}`}>
      <section className="license-card">
        <div className="license-brand">
          <strong>ZM AI TOOL</strong>
          <span>{t('Kích hoạt bản quyền sử dụng', 'Activate your license')}</span>
        </div>
        <div className={`license-state${status.valid ? ' is-valid' : ' is-invalid'}`}>
          <strong>{stateTitle}</strong>
          {stateMessage && <span>{stateMessage}</span>}
        </div>
        {status.configured && (
          <dl className="license-details">
            <div><dt>Key</dt><dd>{status.keyMasked}</dd></div>
            <div><dt>{t('Thời hạn', 'Term')}</dt><dd>{status.remainingDay === -1 ? t('Không giới hạn', 'Unlimited') : t(`Còn ${status.remainingDay} ngày`, `${status.remainingDay} days remaining`)}</dd></div>
            <div><dt>{t('Hết hạn', 'Expires')}</dt><dd>{expiry}</dd></div>
            <div><dt>{t('Lượt kích hoạt còn lại', 'Activations remaining')}</dt><dd>{status.activationLimit}</dd></div>
          </dl>
        )}
        <label className="license-input-label" htmlFor="license-key">
          {status.valid ? t('Nhập key khác', 'Enter another key') : t('Nhập key ZM Tool để tiếp tục', 'Enter your ZM Tool key to continue')}
        </label>
        <div className="license-form">
          <input
            id="license-key"
            ref={inputRef}
            value={key}
            onChange={(event) => setKey(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') void activate() }}
            placeholder={t('Nhập key kích hoạt', 'Enter activation key')}
            autoComplete="off"
            autoFocus
            tabIndex={0}
            disabled={busy}
          />
          <button type="button" onClick={() => void activate()} disabled={busy || !key.trim()}>
            {busy ? t('Đang kiểm tra…', 'Checking…') : t('Kích hoạt', 'Activate')}
          </button>
        </div>
        {status.configured && (
          <button type="button" className="license-deactivate" onClick={() => void deactivate()} disabled={busy}>
            {t('Huỷ kích hoạt key hiện tại', 'Deactivate current key')}
          </button>
        )}
        {error && <p className="license-error">{error}</p>}
      </section>
    </main>
  )
}
