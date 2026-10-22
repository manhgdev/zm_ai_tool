import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Toaster } from 'sonner'
import { accountStorage } from './appSettings'
import { loadLocale, persistLocale, localize, translate, type AppLocale, type MessageKey } from './i18n'
import { IconArrowRight, IconGoogle, IconLogo, IconUser } from '@/shared/components/Icons'
import '../pages/LandingPage.css'

export type Access = { state: string; valid: boolean; startsAt: string | null; expiresAt: string | null; lifetime: boolean }
export type WebUser = { id: string; email: string; name: string; role: string; permissions: string[]; access: Access; max_accounts?: number; max_keys?: number; max_days?: number; allow_lifetime?: number }
type Session = { mode: 'desktop' | 'web'; configured: boolean; user: WebUser | null }
const WebAccount = createContext<WebUser | null>(null)
export function useWebUser() { return useContext(WebAccount) }
let accountId = ''
export function currentWebAccountId() { return accountId }
let installed = false
const webErrors: Record<string, MessageKey> = {
  WEB_LOGIN_REQUIRED: 'error.web.loginRequired',
  WEB_ACCOUNT_CHANGED: 'error.web.accountChanged',
  WEB_WORKER_CAPACITY: 'error.web.capacity',
  WEB_WORKER_START_FAILED: 'error.web.workerFailed',
  WEB_WORKER_UNAVAILABLE: 'error.web.workerFailed',
  WEB_SERVER_REQUIRES_POSIX: 'error.web.serverPlatform',
  WEB_PATH_OUTSIDE_ACCOUNT: 'error.web.privatePath',
  WEB_ORIGIN_REJECTED: 'error.web.origin',
  DESKTOP_ONLY: 'error.web.desktopOnly',
}

function announceAccount(id: string) {
  if (typeof BroadcastChannel === 'undefined') return
  const channel = new BroadcastChannel('zm-web-account')
  channel.postMessage(id)
  channel.close()
}

function bindAccount(user: WebUser) {
  if (installed) return
  installed = true
  accountId = user.id
  announceAccount(user.id)
  for (const name of ['localStorage', 'sessionStorage'] as const) {
    try {
      const scoped = accountStorage(window[name], user.id)
      Object.defineProperty(window, name, { configurable: true, value: scoped })
    } catch { /* Browser privacy settings may disable storage entirely. */ }
  }
  const originalFetch = window.fetch.bind(window)
  window.fetch = async (input, options) => {
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.href)
    if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/') || url.pathname.startsWith('/api/auth/')) {
      return originalFetch(input, options)
    }
    const headers = new Headers(options?.headers || (input instanceof Request ? input.headers : undefined))
    headers.set('X-ZM-Account', user.id)
    const response = await originalFetch(input, { ...options, headers })
    if (response.status === 401) window.dispatchEvent(new Event('zm-web-session-expired'))
    if (response.status === 403) window.dispatchEvent(new Event('zm-web-access-changed'))
    if (!response.ok) {
      const body = await response.clone().json().catch(() => null) as { detail?: string } | null
      const key = body?.detail && webErrors[body.detail]
      if (key) {
        const localized = { ...body, code: body?.detail, detail: translate(loadLocale(), key) }
        const responseHeaders = new Headers(response.headers)
        responseHeaders.delete('content-length')
        responseHeaders.delete('content-encoding')
        return new Response(JSON.stringify(localized), { status: response.status, headers: responseHeaders })
      }
    }
    return response
  }
}

export function WebAccountMenu({ locale, signOutOnly = false }: { locale: AppLocale; signOutOnly?: boolean }) {
  const user = useContext(WebAccount)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  if (!user) return null
  const logout = async () => {
    setBusy(true)
    setFailed(false)
    try {
      const response = await fetch('/api/auth/logout', { method: 'POST' })
      if (!response.ok) throw new Error('logout')
      announceAccount('')
      window.location.assign('/login')
    } catch {
      setBusy(false)
      setFailed(true)
    }
  }
  const signOut = <><button type="button" disabled={busy} onClick={() => void logout()}>{translate(locale, 'header.signOut')}</button>{failed && <span role="alert">{translate(locale, 'header.signOutFailed')}</span>}</>
  if (signOutOnly) return <div className="web-account-signout">{signOut}</div>
  return <details className="web-account-menu">
    <summary aria-label={`${localize(locale, 'Tài khoản của tôi', 'My account')}: ${user.name || user.email}`} title={user.name ? `${user.name} (${user.email})` : user.email}><span className="web-menu-avatar" aria-hidden="true">{(user.name || user.email).slice(0, 1).toUpperCase()}</span><span className="web-menu-name">{user.name || user.email}</span><span aria-hidden="true">⌄</span></summary>
    <div className="web-account-popover">
      <span>{user.email}</span>
      <a href="/account">{localize(locale, 'Tài khoản của tôi', 'My account')}</a>
      {user.permissions.length > 0 && <a href="/admin/accounts">{localize(locale, 'Quản lý', 'Administration')}</a>}
      {signOut}
    </div>
  </details>
}

export function accessText(state: string, locale: AppLocale) {
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const states: Record<string, string> = {
    active: t('Đang hoạt động', 'Active'), pending: t('Chờ cấp quyền', 'Awaiting access'),
    expired: t('Đã hết hạn', 'Expired'), blocked: t('Đã khóa', 'Blocked'),
    scheduled: t('Chưa đến ngày bắt đầu', 'Scheduled'), unactivated: t('Chưa kích hoạt', 'Not activated'),
    deleted: t('Đã xóa', 'Deleted'),
  }
  return states[state] || t('Chưa xác minh', 'Not verified')
}

export function webErrorText(code: string, locale: AppLocale) {
  const t = (vi: string, en: string) => localize(locale, vi, en)
  if (code.startsWith('ACCESS_')) return accessText(code.slice(7).toLowerCase(), locale)
  const errors: Record<string, string> = {
    ADMIN_FORBIDDEN: t('Bạn không có quyền thực hiện thao tác này.', 'You do not have permission for this action.'),
    LAST_OWNER_REQUIRED: t('Phải giữ ít nhất một tài khoản Chủ đang hoạt động.', 'At least one active owner must remain.'),
    RESELLER_QUOTA_EXCEEDED: t('Đã vượt hạn mức tài khoản hoặc key của đại lý.', 'The reseller account or key quota has been exceeded.'),
    RESELLER_TERM_EXCEEDED: t('Thời hạn vượt giới hạn đại lý được cấp.', 'The term exceeds the reseller allowance.'),
    RESELLER_LIFETIME_FORBIDDEN: t('Đại lý chưa được phép cấp vĩnh viễn.', 'This reseller cannot grant lifetime access.'),
    ACCOUNT_ALREADY_EXISTS: t('Email này đã có tài khoản.', 'An account already exists for this email.'),
    GOOGLE_EMAIL_LINKED: t('Email đã liên kết Google; hãy sửa email từ tài khoản Google.', 'This email is linked to Google; update it through the Google account.'),
    INVALID_EMAIL: t('Email không hợp lệ.', 'Invalid email.'), INVALID_TERM: t('Thời hạn hoặc ngày giờ không hợp lệ.', 'Invalid term or date.'),
    INVALID_NUMBER: t('Số lượng không hợp lệ.', 'Invalid quantity.'), INVALID_FIELDS: t('Thông tin chưa hợp lệ.', 'Invalid details.'),
    INVALID_ISSUER: t('Hãy chọn một đại lý hợp lệ.', 'Choose a valid reseller.'),
    TERM_REPLACE_REQUIRED: t('Hãy đặt lại thời hạn cho mục chưa bắt đầu hoặc đang vĩnh viễn.', 'Replace the term for an unstarted or lifetime item.'),
    DEVICE_LIMIT_BELOW_ACTIVE: t('Hãy thu hồi bớt thiết bị trước khi giảm giới hạn.', 'Revoke devices before reducing the limit.'),
    LICENSE_SERVER_UNAVAILABLE: t('Không kết nối được máy chủ xác thực.', 'Cannot reach the access server.'),
    RATE_LIMITED: t('Quá nhiều yêu cầu. Hãy thử lại sau một phút.', 'Too many requests. Try again in one minute.'),
    TIMEZONE_REQUIRED: t('Ngày giờ phải có múi giờ.', 'Dates must include a time zone.'),
    ACCOUNT_DELETED: t('Tài khoản đã bị xóa.', 'The account was deleted.'),
    DESKTOP_CODE_INVALID: t('Phiên đăng nhập đã hết hạn. Hãy thử lại.', 'The sign-in attempt expired. Try again.'),
    INVALID_LICENSE_RESPONSE: t('Phản hồi xác thực không hợp lệ.', 'Invalid access server response.'),
    DEVICE_LIMIT_REACHED: t('Key đã đủ số thiết bị. Hãy thu hồi thiết bị cũ.', 'The key device limit is reached. Revoke an old device.'),
    DEVICE_REVOKED: t('Phiên thiết bị đã bị thu hồi. Hãy đăng nhập hoặc kích hoạt lại.', 'Device access was revoked. Sign in or activate again.'),
    DEVICE_ALREADY_CONFIGURED: t('Hãy đăng xuất hoặc hủy kích hoạt thiết bị trước khi đổi phương thức.', 'Sign out or deactivate this device before switching access methods.'),
    KEY_INVALID: t('Key không hợp lệ. Hãy dùng key mới bắt đầu bằng ZMAI-.', 'Invalid key. Use a new key starting with ZMAI-.'),
    KEY_COPY_FAILED: t('Không thể sao chép tự động. Chọn key rồi nhấn Ctrl+C (Mac: ⌘C) để sao chép.', 'Could not copy automatically. Select the key and press Ctrl+C (Mac: ⌘C) to copy.'),
    KEY_COPY_UNAVAILABLE: t('Không thể lấy lại giá trị đầy đủ của key này.', 'The full value of this key cannot be recovered.'),
    LICENSE_CHECK_REQUIRED: t('Cần kết nối mạng để kiểm tra quyền sử dụng.', 'Connect to the internet to verify access.'),
    LOGOUT_NETWORK_REQUIRED: t('Cần mạng để đăng xuất và trả lại lượt thiết bị.', 'Connect to sign out and release the device slot.'),
    WEB_LOGIN_REQUIRED: t('Phiên đăng nhập đã hết hạn.', 'Your session has expired.'),
    NOT_FOUND: t('Không tìm thấy mục này.', 'Item not found.'),
  }
  return errors[code] || t('Không thực hiện được thao tác. Kiểm tra thông tin và thử lại.', 'The action failed. Check the details and try again.')
}

export function WebHeader({ locale, theme, onLocale, onTheme, action, home = '/' }: {
  locale: AppLocale; theme: 'light' | 'dark'; onLocale: () => void; onTheme: () => void; action?: ReactNode; home?: string
}) {
  const t = (vi: string, en: string) => localize(locale, vi, en)
  return <header className="lp-nav"><div className="lp-container lp-nav-inner">
    <a className="lp-brand" href={home}><IconLogo size={34} /><span>ZM AI TOOL</span></a>
    <nav className="lp-nav-links" aria-label={t('Điều hướng chính', 'Main navigation')}>
      <a href="/#product">{t('Công cụ AI', 'AI tools')}</a><a href="/#workflow">{t('Quy trình', 'Workflow')}</a>
      <a href="/#guide">{t('Hướng dẫn', 'Guide')}</a><a href="/#faq">{t('Hỏi đáp', 'FAQ')}</a>
    </nav>
    <div className="lp-nav-actions">
      <button className="lp-icon-button" onClick={onTheme} aria-label={theme === 'light' ? t('Chuyển sang giao diện tối', 'Switch to dark theme') : t('Chuyển sang giao diện sáng', 'Switch to light theme')}>{theme === 'light' ? '☾' : '☼'}</button>
      <button className="lp-locale" onClick={onLocale} aria-label={locale === 'vi' ? 'Switch to English' : 'Chuyển sang tiếng Việt'}>{locale === 'vi' ? 'EN' : 'VI'}</button>
      {action || <a className="lp-button lp-button-primary" href="/video-clone">{t('Mở workspace', 'Open workspace')}</a>}
      <WebAccountMenu locale={locale} />
    </div>
  </div></header>
}

export function WebFooter({ locale, workspaceHref = '/video-clone', home = '/', showDownload = true }: { locale: AppLocale; workspaceHref?: string; home?: string; showDownload?: boolean }) {
  const t = (vi: string, en: string) => localize(locale, vi, en)
  return <footer className="lp-footer"><div className="lp-container"><div><a className="lp-brand" href={home}><IconLogo size={30} /><span>ZM AI TOOL</span></a><p>{t('Một workspace cho mọi nhịp sáng tạo.', 'One workspace for every creative moment.')}</p></div><nav aria-label={t('Liên kết cuối trang', 'Footer links')}><a href="/#product">{t('Công cụ AI', 'AI tools')}</a><a href="/#guide">{t('Hướng dẫn', 'Guide')}</a>{showDownload && <a href="/#download">{t('Tải về', 'Download')}</a>}<a href="/#faq">{t('Hỏi đáp', 'FAQ')}</a><a href={workspaceHref}>{t('Mở workspace', 'Open workspace')}</a></nav><span>© {new Date().getFullYear()} ZM AI TOOL</span></div></footer>
}

export function WebFrame({ children, locale, onLocale }: { children: ReactNode; locale: AppLocale; onLocale: (locale: AppLocale) => void }) {
  const frame = useRef<HTMLDivElement>(null)
  const [toastContainer] = useState(() => document.createElement('div'))
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try { return localStorage.getItem('zm-landing-theme') === 'dark' ? 'dark' : 'light' } catch { return 'light' }
  })
  useLayoutEffect(() => {
    const element = frame.current!
    const placeToasts = () => {
      // Native modal dialogs cover outside content, including a normal toast host.
      const dialogs = element.querySelectorAll('dialog[open]')
      const target = dialogs[dialogs.length - 1] || element
      if (toastContainer.parentElement !== target) target.appendChild(toastContainer)
    }
    const observer = new MutationObserver(placeToasts)
    observer.observe(element, { childList: true, subtree: true, attributes: true, attributeFilter: ['open'] })
    placeToasts()
    return () => { observer.disconnect(); toastContainer.remove() }
  }, [toastContainer])
  return <div ref={frame} className="landing web-frame" data-theme={theme} lang={locale}>
    <WebHeader locale={locale} theme={theme} onLocale={() => { const next = locale === 'vi' ? 'en' : 'vi'; persistLocale(next); onLocale(next) }} onTheme={() => {
      const next = theme === 'light' ? 'dark' : 'light'; setTheme(next)
      try { localStorage.setItem('zm-landing-theme', next) } catch { /* Storage may be disabled. */ }
    }} />
    {children}<WebFooter locale={locale} />
    {createPortal(<Toaster position="bottom-right" theme={theme} richColors closeButton
      containerAriaLabel={localize(locale, 'Thông báo', 'Notifications')}
      toastOptions={{ closeButtonAriaLabel: localize(locale, 'Đóng thông báo', 'Close notification') }} />, toastContainer)}
  </div>
}

function safeReturnTo() {
  const value = new URLSearchParams(window.location.search).get('return_to') || '/video-clone'
  return value.startsWith('/') && !value.startsWith('//') && !value.includes('\\') && !/[\x00-\x1f]/.test(value) ? value : '/video-clone'
}

export default function WebSession({ children, publicPage = false }: { children: ReactNode; publicPage?: boolean }) {
  const [session, setSession] = useState<Session | null>(null)
  const [failed, setFailed] = useState(false)
  const [locale, setLocale] = useState(loadLocale)
  const t = (vi: string, en: string) => localize(locale, vi, en)
  useEffect(() => {
    let dead = false
    const expired = () => { if (!dead) window.location.assign('/login') }
    const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('zm-web-account') : null
    if (channel) channel.onmessage = event => { if (event.data !== accountId) expired() }
    window.addEventListener('zm-web-session-expired', expired)
    const refresh = () => fetch('/api/auth/session', { cache: 'no-store' }).then(async response => {
      if (!response.ok) throw new Error('session')
      const next = await response.json() as Session
      if (dead) return
      if (next.mode === 'web' && next.user) {
        bindAccount(next.user)
        if (window.location.pathname === '/login' && (next.user.access.valid || safeReturnTo().startsWith('/api/desktop/authorize'))) {
          window.location.replace(safeReturnTo()); return
        }
      }
      setSession(next); setFailed(false)
    }).catch(() => { if (!dead) setFailed(true) })
    void refresh()
    const changed = () => { void refresh() }
    const timer = window.setInterval(changed, 30_000)
    window.addEventListener('zm-web-access-changed', changed)
    return () => { dead = true; channel?.close(); clearInterval(timer); window.removeEventListener('zm-web-session-expired', expired); window.removeEventListener('zm-web-access-changed', changed) }
  }, [])
  const user = session?.user || null
  const adminPage = window.location.pathname.startsWith('/admin/')
  const accountPage = window.location.pathname === '/account'
  if (session?.mode === 'desktop' || session?.mode === 'web' && (publicPage || user && !accountPage && (adminPage ? user.permissions.length > 0 : user.access.valid))) {
    return <WebAccount.Provider value={user}>{children}</WebAccount.Provider>
  }
  const returnTo = window.location.pathname === '/login' ? safeReturnTo() : window.location.pathname + window.location.search
  const loginError = new URLSearchParams(window.location.search).get('error') === 'google_login_failed'
  return <WebAccount.Provider value={user}><WebFrame locale={locale} onLocale={setLocale}>
    <main className="web-login"><section className={`web-login-card${user ? ' web-profile-card' : ''}`} aria-labelledby="web-login-title">
      <div className="web-login-emblem" aria-hidden="true">{user ? <IconUser size={25} /> : <IconLogo size={42} />}</div>
      <h1 id="web-login-title">{user ? t('Tài khoản của tôi', 'My account') : t('Đăng nhập tài khoản', 'Sign in to your account')}</h1>
      <p className="web-login-description">{t('Project, job và kết quả của bạn được lưu riêng theo tài khoản Google.', 'Your projects, jobs and results are saved separately for your Google account.')}</p>
      {!session && !failed && <p role="status">{t('Đang kiểm tra phiên đăng nhập…', 'Checking your session…')}</p>}
      {failed ? <><p role="alert">{t('Không kết nối được máy chủ. Vui lòng thử lại.', 'Cannot reach the server. Please try again.')}</p><button className="web-login-action" onClick={() => window.location.reload()}>{t('Thử lại', 'Retry')}</button></> : user ? <>
        <div className="web-profile-identity"><strong>{user.name || user.email}</strong><span>{user.email}</span></div>
        <span className={'admin-status is-' + user.access.state} role="status">{accessText(user.access.state, locale)}</span>
        <dl className="web-profile-dates">
          <div><dt>{t('Bắt đầu · Giờ Việt Nam', 'Starts · Vietnam time')}</dt><dd>{user.access.startsAt ? new Date(user.access.startsAt).toLocaleString(locale === 'vi' ? 'vi-VN' : 'en-US', { timeZone: 'Asia/Ho_Chi_Minh' }) : '—'}</dd></div>
          <div><dt>{t('Thời hạn', 'Term')}</dt><dd>{user.access.lifetime ? t('Vĩnh viễn', 'Lifetime') : user.access.expiresAt ? new Date(user.access.expiresAt).toLocaleString(locale === 'vi' ? 'vi-VN' : 'en-US', { timeZone: 'Asia/Ho_Chi_Minh' }) : '—'}</dd></div>
        </dl>
        {!user.access.startsAt && <p className="web-login-notice">{t('Vui lòng liên hệ quản trị viên để được cấp quyền.', 'Contact an administrator to receive access.')}</p>}
        {user.access.valid && <a className="web-login-action is-primary" href="/video-clone">{t('Mở workspace', 'Open workspace')}<IconArrowRight /></a>}
        {user.permissions.length > 0 && <a className="web-login-action" href="/admin/accounts">{t('Quản lý tài khoản và key', 'Manage accounts and keys')}</a>}
        <WebAccountMenu locale={locale} signOutOnly />
      </> : session && !session.configured ? <p role="status">{t('Đăng nhập Google chưa được cấu hình. Quản trị viên sẽ bổ sung sau.', 'Google sign-in is not configured yet. An administrator will set it up later.')}</p> : session && <a className="web-google-button" href={`/api/auth/google?return_to=${encodeURIComponent(returnTo)}`}><IconGoogle />{t('Tiếp tục với Google', 'Continue with Google')}<IconArrowRight /></a>}
      {loginError && <p role="alert">{t('Đăng nhập Google chưa hoàn tất. Vui lòng thử lại.', 'Google sign-in did not complete. Please try again.')}</p>}
    </section></main>
  </WebFrame></WebAccount.Provider>
}
