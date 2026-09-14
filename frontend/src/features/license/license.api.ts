import { fetchJson } from '@/shared/api/fetchJson'

export type LicenseStatus = {
  valid: boolean
  configured: boolean
  keyMasked: string
  remainingDay: number
  expiresAt: string | null
  activationLimit: number
  message: string
}

const STATUS_CACHE_KEY = 'zm_ai_tool.license.status.v1'
const STATUS_CACHE_MS = 5 * 60 * 1000
let statusInFlight: Promise<LicenseStatus> | null = null

export function readCachedStatus(): LicenseStatus | null {
  try {
    const cached = JSON.parse(sessionStorage.getItem(STATUS_CACHE_KEY) || 'null')
    return cached && Date.now() - Number(cached.at || 0) < STATUS_CACHE_MS
      ? cached.status as LicenseStatus
      : null
  } catch {
    return null
  }
}

function cacheStatus(status: LicenseStatus): LicenseStatus {
  try { sessionStorage.setItem(STATUS_CACHE_KEY, JSON.stringify({ at: Date.now(), status })) } catch {}
  return status
}

function getStatus(): Promise<LicenseStatus> {
  const cached = readCachedStatus()
  if (cached) return Promise.resolve(cached)
  if (statusInFlight) return statusInFlight
  statusInFlight = fetchJson<LicenseStatus>('/api/license/status', undefined, 35_000)
    .then(cacheStatus)
    .finally(() => { statusInFlight = null })
  return statusInFlight
}

export const licenseApi = {
  status: getStatus,
  activate: (key: string) =>
    fetchJson<LicenseStatus>('/api/license/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }, 65_000).then(cacheStatus),
  deactivate: () =>
    fetchJson<LicenseStatus>('/api/license/deactivate', { method: 'POST' }, 15_000).then(cacheStatus),
}
