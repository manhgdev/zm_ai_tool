export const TASK_ACCESS_REQUIRED = 'zm-task-access-required'

/** Observe the shared fetch boundary, including the raw streaming API clients. */
export function watchTaskAccess(fetcher: typeof fetch): typeof fetch {
  return async (input, init) => {
    const response = await fetcher(input, init)
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.href)
    // The server owns the action policy, including validated JSON/multipart start flags.
    if (url.origin === window.location.origin && url.pathname.startsWith('/api/') && response.status === 403) {
      const body = await response.clone().json().catch(() => null) as { detail?: unknown } | null
      if (body?.detail === 'ACCESS_REQUIRED') window.dispatchEvent(new Event(TASK_ACCESS_REQUIRED))
    }
    return response
  }
}
