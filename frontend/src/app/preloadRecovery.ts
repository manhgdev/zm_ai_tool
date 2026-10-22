/** One retry per minute; persistent/offline failures still reach the error boundary. */
export function recoverPreloadError(event: Event): void {
  const key = 'zm-preload-retry'
  try {
    const now = Date.now()
    const previous = Number(sessionStorage.getItem(key) || 0)
    if (previous && now - previous < 60_000) return
    sessionStorage.setItem(key, String(now))
    event.preventDefault()
    window.location.reload()
  } catch {
    // Storage may be unavailable; do not risk an unlimited reload loop.
  }
}
