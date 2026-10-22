import { Component, type ReactNode, StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './app/App'
import { appModePath, loadAppMode } from './app/appMode'
import LandingPage from './pages/LandingPage'
import WebSession from './app/WebSession'
import AdminPage from './pages/AdminPage'
import './index.css'

// WKWebView may omit the usual "Mac OS X" token. Check every platform hint so
// the shared macOS form sizing is also enabled in packaged desktop builds.
const navigatorWithPlatformData = navigator as Navigator & {
  userAgentData?: { platform?: string }
}
const platformHint = [
  navigator.userAgent,
  navigator.platform,
  navigatorWithPlatformData.userAgentData?.platform || '',
].join(' ')
if (/Macintosh|Mac OS X|MacIntel|macOS/i.test(platformHint)) {
  document.documentElement.classList.add('platform-macos')
}

class ErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean; error: Error | null }> {
  override state: { hasError: boolean; error: Error | null } = { hasError: false, error: null }
  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error }
  }
  componentDidCatch(error: Error, errorInfo: unknown) {
    console.error('App Crash:', error, errorInfo)
  }
  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: '2rem', background: '#1e1e2e', color: '#f38ba8', fontFamily: 'sans-serif', height: '100vh', boxSizing: 'border-box' }}>
          <h2>Đã xảy ra lỗi giao diện (UI Crash):</h2>
          <pre style={{ background: '#11111b', padding: '1rem', borderRadius: '8px', overflow: 'auto', whiteSpace: 'pre-wrap' }}>
            {this.state.error?.stack || String(this.state.error)}
          </pre>
          <button onClick={() => window.location.reload()} style={{ padding: '0.5rem 1rem', marginTop: '1rem', cursor: 'pointer', borderRadius: '4px', border: 'none', background: '#89b4fa', color: '#11111b', fontWeight: 'bold' }}>
            Tải lại ứng dụng
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

/** Packaged desktop window — browser marketing site has no pywebview. */
export function isDesktopShell(): boolean {
  return typeof window !== 'undefined' && 'pywebview' in window
}

/**
 * Decide Landing vs App for `/`.
 * Desktop cold-start restores the last sticky tab; `?landing=1` keeps marketing Landing in-app.
 */
export function resolveRootEntry(): 'landing' | 'app' {
  const bare = window.location.pathname.replace(/\/+$/, '') || '/'
  if (bare === '/landing') {
    const params = new URLSearchParams(window.location.search)
    params.set('landing', '1')
    const query = params.toString()
    window.history.replaceState(null, '', `/${query ? `?${query}` : ''}${window.location.hash}`)
  }

  const path = window.location.pathname.replace(/\/+$/, '') || '/'
  const params = new URLSearchParams(window.location.search)
  const wantLanding = params.has('landing')

  if (path === '/' && isDesktopShell() && !wantLanding) {
    const dest = `${appModePath(loadAppMode())}${window.location.search}${window.location.hash}`
    window.history.replaceState(null, '', dest)
    return 'app'
  }

  return path === '/' ? 'landing' : 'app'
}

const entry = resolveRootEntry()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <WebSession publicPage={entry === 'landing'}>
        {entry === 'landing' ? <LandingPage /> : /^\/admin\/(accounts|keys)\/?$/.test(window.location.pathname) ? <AdminPage /> : <App />}
      </WebSession>
    </ErrorBoundary>
  </StrictMode>,
)
