import { fetchJson } from '@/shared/api/fetchJson'

export type LicenseStatus = {
  mode?: 'account' | 'key'
  state?: string
  code?: string
  startsAt?: string | null
  lifetime?: boolean
  user?: { email: string; name: string }
  valid: boolean
  configured: boolean
  keyMasked: string
  remainingDay: number
  expiresAt: string | null
  activationLimit: number
  message: string
  transient?: boolean
}

let statusInFlight: Promise<LicenseStatus> | null = null
function getStatus(force = false): Promise<LicenseStatus> {
  if (statusInFlight) return statusInFlight
  statusInFlight = fetchJson<LicenseStatus>('/api/license/status' + (force ? '?force=true' : ''), undefined, 35_000)
    .finally(() => { statusInFlight = null })
  return statusInFlight
}

export const licenseApi = {
  login: () => fetchJson<{ authorizationUrl: string }>('/api/auth/desktop/start', { method: 'POST' }),
  status: (force = false) => getStatus(force),
  activate: (key: string) =>
    fetchJson<LicenseStatus>('/api/license/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }, 65_000),
  deactivate: () =>
    fetchJson<LicenseStatus>('/api/license/deactivate', { method: 'POST' }, 15_000),
}
