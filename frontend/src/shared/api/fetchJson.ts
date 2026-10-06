/** fetch + timeout — tránh treo khi backend reload / đang cài gói.
 *  Hỗ trợ AbortSignal ngoài (hủy tốc độ cũ khi chọn tốc độ mới).
 */
const pendingReads = new Map<string, Promise<unknown>>()

export async function fetchJson<T>(url: string, init?: RequestInit, timeoutMs = 12_000): Promise<T> {
  // ponytail: only default GETs share in-flight work; custom headers/signals
  // stay isolated. No settled cache, so refreshes always read current state.
  if (init && Object.keys(init).length) {
    if (!['GET', 'HEAD'].includes((init.method || 'GET').toUpperCase())) pendingReads.clear()
    return requestJson<T>(url, init, timeoutMs)
  }
  const key = JSON.stringify([url, timeoutMs])
  let request = pendingReads.get(key)
  if (!request) {
    request = requestJson<T>(url, init, timeoutMs)
    pendingReads.set(key, request)
  }
  try {
    // Callers may edit returned rows; never share mutable parsed objects.
    return structuredClone(await request) as T
  } finally {
    if (pendingReads.get(key) === request) pendingReads.delete(key)
  }
}

async function requestJson<T>(
  url: string,
  init?: RequestInit,
  timeoutMs = 12_000,
): Promise<T> {
  const ac = new AbortController()
  const external = init?.signal
  const onExtAbort = () => ac.abort()
  if (external) {
    if (external.aborted) ac.abort()
    else external.addEventListener('abort', onExtAbort)
  }
  const t = window.setTimeout(() => ac.abort(), timeoutMs)
  try {
    const { signal: _ignore, ...rest } = init || {}
    const res = await fetch(url, { ...rest, signal: ac.signal })
    if (!res.ok) {
      const body = await res.text()
      let message = body
      try {
        const parsed = JSON.parse(body) as { detail?: unknown }
        if (typeof parsed.detail === 'string') message = parsed.detail
      } catch {
        // Non-JSON errors already contain the most useful server message.
      }
      throw new Error(message || res.statusText)
    }
    return res.json() as Promise<T>
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      if (external?.aborted) throw e
      const sec = Math.round(timeoutMs / 1000)
      throw new Error(`API timeout (${sec}s) — backend bận hoặc đang cài gói?`)
    }
    throw e
  } finally {
    window.clearTimeout(t)
    external?.removeEventListener('abort', onExtAbort)
  }
}
