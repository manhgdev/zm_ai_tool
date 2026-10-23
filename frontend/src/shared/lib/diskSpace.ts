export type DiskSpaceInfo = {
  errorCode?: string
  diskPath?: string
  diskDrive?: string
  freeDiskBytes?: number | null
  requiredDiskBytes?: number
}

/** Worker tracebacks also expose stable OS identifiers before structured status is available. */
export function diskSpaceError(raw: string, info?: DiskSpaceInfo): DiskSpaceInfo | null {
  const marker = /\bDISK_FULL\b|\b(?:winerror|os error)\s*112\b|\berrno\s*28\b|no space left|not enough space on the disk|disk quota exceeded/gi
  const matches = [...raw.matchAll(marker)]
  if (info?.errorCode !== 'DISK_FULL' && !matches.length) return null
  // Ignore earlier traceback source paths; the failing write follows the OS error.
  let path = info?.diskPath || ''
  for (const match of matches.reverse()) {
    if (path) break
    const line = raw.slice(match.index).split(/\r?\n/, 1)[0]
    path = line.match(/(['"])((?:[a-z]:[\\/]|\/|\\\\).*?)\1/i)?.[2] || ''
  }
  // Python repr and JSON escape backslashes. Preserve UNC's leading pair.
  const normalized = path.replace(/([^\\])\\\\/g, '$1\\')
  const drive = info?.diskDrive || normalized.match(/^[a-z]:|^\\\\[^\\]+\\[^\\]+/i)?.[0] || (normalized.startsWith('/') ? '/' : '')
  return { ...info, errorCode: 'DISK_FULL', diskPath: normalized, diskDrive: /^[a-z]:$/i.test(drive) ? drive.toUpperCase() : drive }
}
