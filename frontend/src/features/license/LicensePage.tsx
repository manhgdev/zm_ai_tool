import { useEffect, useState } from 'react'
import { licenseApi, type LicenseStatus } from './license.api'
import { localize, useLocale } from '@/app/i18n'
import { accessText, useWebUser, webErrorText } from '@/app/WebSession'
import { IconGoogle, IconKey, IconLogo, IconUser } from '@/shared/components/Icons'
import './LicensePage.css'

type Props = { status: LicenseStatus; gate?: boolean; embedded?: boolean; onStatusChange: (status: LicenseStatus) => void }

export default function LicensePage({ status, gate = false, embedded = false, onStatusChange }: Props) {
  const { locale } = useLocale()
  const user = useWebUser()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [waiting, setWaiting] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { if (status.configured) setWaiting(false) }, [status.configured])
  useEffect(() => {
    if (user || busy) return
    let disposed = false
    const refresh = () => licenseApi.status().then(next => { if (!disposed) onStatusChange(next) }).catch(() => {})
    const timer = window.setInterval(refresh, waiting ? 2000 : 30_000)
    const deadline = window.setTimeout(() => setWaiting(false), 600_000)
    return () => { disposed = true; clearInterval(timer); clearTimeout(deadline) }
  }, [user, busy, waiting, onStatusChange])
  async function perform(action: () => Promise<void>) {
    setBusy(true); setError('')
    try { await action() }
    catch (e) { setError(webErrorText(e instanceof Error ? e.message : '', locale)) }
    finally { setBusy(false) }
  }
  const date = (value?: string | null) => value ? new Date(value).toLocaleString(locale === 'vi' ? 'vi-VN' : 'en-US', { timeZone: 'Asia/Ho_Chi_Minh' }) : '—'
  return <main className={`license-page${gate ? ' license-gate' : ''}${embedded ? ' license-embedded' : ''}`}><section className="license-card">
    <div className="license-card-head"><span className="license-emblem" aria-hidden="true">{gate ? <IconLogo size={36} /> : status.mode === 'key' ? <IconKey size={22} /> : <IconUser size={22} />}</span><div className="license-brand"><strong>{gate ? 'ZM AI TOOL' : t('Quyền sử dụng', 'Access')}</strong><span>{t('Tài khoản và quyền sử dụng', 'Account and access')}</span></div></div>
    <div className={`license-state${status.valid ? ' is-valid' : ' is-invalid'}`} role="status"><strong>{accessText(status.state || (status.valid ? 'active' : 'pending'), locale)}</strong>
      {status.code && status.code !== 'ACCESS_' + status.state?.toUpperCase() && <span>{webErrorText(status.code, locale)}</span>}
    </div>
    {status.configured && <dl className="license-details">
      <div><dt>{user ? t('Phương thức đăng nhập', 'Sign-in method') : t('Phương thức kích hoạt APP', 'APP activation method')}</dt><dd>{status.mode === 'key' ? t('Key ứng dụng', 'Application key') : t('Tài khoản Google', 'Google account')}</dd></div>
      <div><dt>{status.mode === 'key' ? t('Key ứng dụng', 'Application key') : t('Tài khoản', 'Account')}</dt><dd>{status.mode === 'key' ? status.keyMasked : user?.email || status.user?.email || '—'}</dd></div>
      <div><dt>{t('Bắt đầu · Giờ Việt Nam', 'Starts · Vietnam time')}</dt><dd>{date(status.startsAt)}</dd></div>
      <div><dt>{t('Hết hạn · Giờ Việt Nam', 'Expires · Vietnam time')}</dt><dd>{status.lifetime || status.remainingDay === -1 ? t('Vĩnh viễn', 'Lifetime') : date(status.expiresAt)}</dd></div>
      {status.mode === 'key' && <div><dt>{t('Thiết bị đang kích hoạt / giới hạn', 'Active devices / limit')}</dt><dd>{status.activationCount ?? '—'} / {status.activationLimit}</dd></div>}
    </dl>}
    {user ? <a className="license-deactivate" href="/account">{t('Thông tin tài khoản và đăng xuất', 'Account details and sign out')}</a> : <>
      {status.configured ? <>
        <p className="license-method-hint">{t('APP đang dùng quyền của phương thức trên. Muốn đổi tài khoản hoặc key, hãy đăng xuất hoặc hủy kích hoạt thiết bị trước.', 'The APP uses the access shown above. To switch accounts or keys, sign out or deactivate this device first.')}</p>
        <div className="license-actions">
          <button className="license-deactivate" type="button" disabled={busy} onClick={() => void perform(async () => onStatusChange(await licenseApi.status(true)))}>{t('Kiểm tra lại quyền', 'Refresh access')}</button>
          <button className="license-deactivate license-danger" type="button" disabled={busy} onClick={() => {
            const question = status.mode === 'key'
              ? t('Hủy kích hoạt thiết bị này và trả lại lượt thiết bị cho key?', 'Deactivate this device and release its key device slot?')
              : t('Đăng xuất tài khoản khỏi APP trên thiết bị này?', 'Sign out of this account in the APP on this device?')
            if (window.confirm(question)) void perform(async () => { onStatusChange(await licenseApi.deactivate()); setWaiting(false) })
          }}>{status.mode === 'key' ? t('Hủy kích hoạt thiết bị', 'Deactivate device') : t('Đăng xuất', 'Sign out')}</button>
        </div>
      </> : <>
      <p className="license-method-hint">{t('Chọn một cách để kích hoạt APP: tài khoản Google đã được cấp quyền hoặc key ứng dụng. Hai phương thức không cộng dồn thời hạn.', 'Choose one way to activate the APP: an authorized Google account or an application key. Their access periods are not combined.')}</p>
      <button type="button" className="license-google" disabled={busy || waiting} onClick={() => void perform(async () => { await licenseApi.login(); setWaiting(true) })}><IconGoogle />{waiting ? t('Đang chờ đăng nhập trên trình duyệt…', 'Waiting for browser sign-in…') : t('Đăng nhập bằng Google', 'Sign in with Google')}</button>
      {waiting && <button type="button" className="license-deactivate" onClick={() => setWaiting(false)}>{t('Thử đăng nhập lại', 'Try sign-in again')}</button>}
      <p className="license-divider">{t('Hoặc kích hoạt bằng key mới được cấp cho ứng dụng.', 'Or activate with a new application key.')}</p>
      <form className="license-form" onSubmit={e => { e.preventDefault(); void perform(async () => { onStatusChange(await licenseApi.activate(key.trim())); setKey('') }) }}>
        <label className="license-input-label" htmlFor="license-key">{t('Key ứng dụng', 'Application key')}</label>
        <input id="license-key" value={key} onChange={e => setKey(e.target.value)} placeholder="ZMAI-…" autoComplete="off" disabled={busy || waiting} required />
        <button type="submit" disabled={busy || waiting || !key.trim()}>{busy ? t('Đang kiểm tra…', 'Checking…') : t('Kích hoạt', 'Activate')}</button>
      </form>
      </>}
    </>}
    {error && <p className="license-error" role="alert">{error}</p>}
  </section></main>
}
