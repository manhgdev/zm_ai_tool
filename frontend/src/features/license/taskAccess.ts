import { licenseApi } from './license.api'

export const TASK_ACCESS_REQUIRED = 'zm-task-access-required'

/** Only requests that create results, never navigation, polling or autosaves. */
export function isTaskRequest(url: string, init?: RequestInit): boolean {
  if ((init?.method || 'GET').toUpperCase() !== 'POST') return false
  const path = url.split('?')[0].replace(/\/$/, '')
  if (/^\/api\/(?:flow|automation|download|cleaner|media-compose|srt-export|drawing)\/jobs(?:\/(?:batch|start|batch-retry))?$/.test(path)) return true
  if (/^\/api\/queue(?:\/[^/]+)?\/action$/.test(path)) {
    try {
      return ['resume', 'retry', 'resume_all', 'retry_failed'].includes(JSON.parse(String(init?.body || '{}')).op)
    } catch { return false }
  }
  return path === '/api/queue'
    || /^\/api\/tts\/studio\/(?:synthesize|transcribe|clone)$/.test(path)
    || /^\/api\/projects\/[^/]+\/(?:run|dub|export|ocr-translate|rebake-speed|review-script|audio\/no-vocals|segments\/(?:retranscribe-range|compound)|segments\/[^/]+\/(?:preview-tts|retranslate|review-translation))$/.test(path)
    || /^\/api\/review\/generate$/.test(path)
    || /^\/api\/chat\/conversations(?:\/[^/]+\/messages(?:\/[^/]+\/retry)?)?$/.test(path)
    || /^\/api\/automation\/(?:suggest-topics|youtube\/rewrite|jobs\/[^/]+\/(?:resume|retry|select-topic))$/.test(path)
    || /^\/api\/flow\/(?:jobs\/(?:[^/]+\/retry|batch-retry)|series\/(?:draft|[^/]+\/(?:draft-more|merge|run|anchors\/generate|episodes\/[^/]+\/(?:merge|scenes\/[^/]+\/generate))))$/.test(path)
}

/** Observe the shared fetch boundary, including the raw streaming API clients. */
export function watchTaskAccess(fetcher: typeof fetch): typeof fetch {
  return async (input, init) => {
    const response = await fetcher(input, init)
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.href)
    const options = init || (input instanceof Request ? { method: input.method } : undefined)
    if (url.origin === window.location.origin && response.status === 403 && isTaskRequest(url.pathname, options)) {
      const body = await response.clone().json().catch(() => null) as { detail?: unknown } | null
      if (body?.detail === 'ACCESS_REQUIRED') window.dispatchEvent(new Event(TASK_ACCESS_REQUIRED))
    }
    return response
  }
}

// Task preparation can fail on a GET before the actual run request is sent.
export async function requireTaskAccess(): Promise<void> {
  const status = await licenseApi.status()
  if (!status.valid) {
    window.dispatchEvent(new Event(TASK_ACCESS_REQUIRED))
    throw new Error('ACCESS_REQUIRED')
  }
}
