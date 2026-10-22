import { fetchJson } from '@/shared/api/fetchJson'

export type LicenseStatus = {
  installationId?: string
  deviceType?: 'pc' | 'laptop'
  mode?: 'account' | 'key'
  state?: string
  code?: string
  startsAt?: string | null
  lifetime?: boolean
  user?: { email: string; name: string } | null
  valid: boolean
  configured: boolean
  keyMasked: string
  remainingDay: number
  expiresAt: string | null
  activationLimit: number
  activationCount?: number
  message: string
  transient?: boolean
  loginError?: string
}

let statusInFlight: Promise<LicenseStatus> | null = null
function getStatus(force = false): Promise<LicenseStatus> {
  if (statusInFlight) return statusInFlight
  statusInFlight = fetchJson<LicenseStatus>('/api/license/status' + (force ? '?force=true' : ''), { cache: 'no-store' }, 35_000)
    .finally(() => { statusInFlight = null })
  return statusInFlight
}

async function changeAccess<T>(request: () => Promise<T>) {
  // Finish an older poll before changing credentials so it cannot restore stale UI state.
  if (statusInFlight) await statusInFlight.catch(() => {})
  return request()
}

export const licenseApi = {
  login: () => changeAccess(() => fetchJson<{ authorizationUrl: string }>('/api/auth/desktop/start', { method: 'POST' })),
  status: (force = false) => getStatus(force),
  activate: (key: string) =>
    changeAccess(() => fetchJson<LicenseStatus>('/api/license/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }, 65_000)),
  deactivate: () =>
    changeAccess(() => fetchJson<LicenseStatus>('/api/license/deactivate', { method: 'POST' }, 15_000)),
}
