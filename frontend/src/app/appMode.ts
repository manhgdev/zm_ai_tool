import type { AppMode } from '@/shared/components/Header'

export const APP_MODE_LS = 'zm_ai_tool.appMode'

export const APP_MODES = ['clone', 'live-preview', 'renders', 'film', 'batch', 'flow', 'chat', 'automation', 'cleaner', 'srt-image', 'srt-export', 'drawing', 'download', 'tts'] as const satisfies readonly AppMode[]

/**
 * Mỗi màn cấp ứng dụng có một URL ổn định.  Trước đây Download chỉ sống trong
 * localStorage nên mở /download-video vẫn có thể rơi vào tab đã dùng lần trước.
 */
export const APP_MODE_PATHS: Record<AppMode, string> = {
  clone: '/video-clone',
  'live-preview': '/live-preview',
  renders: '/renders',
  download: '/download-video',
  tts: '/text-to-speech',
  cleaner: '/video-cleaner',
  'srt-image': '/media-compose',
  'srt-export': '/subtitle-export',
  drawing: '/drawing',
  film: '/film',
  batch: '/batch',
  flow: '/flow-veo',
  chat: '/chat',
  automation: '/automation',
  license: '/license',
}

export function appModeFromPath(pathname: string): AppMode | null {
  const path = pathname.replace(/\/+$/, '') || '/'
  return (Object.entries(APP_MODE_PATHS) as [AppMode, string][])
    .find(([, candidate]) => candidate === path)?.[0] ?? null
}

export function appModePath(mode: AppMode): string {
  return APP_MODE_PATHS[mode]
}

/** Modes that should not stick across app restarts (editor / gate screens). */
const EPHEMERAL_MODES: ReadonlySet<AppMode> = new Set(['live-preview', 'license'])

/** Validate raw storage — invalid/corrupt/missing → Clone Video. */
export function parseAppMode(raw: string | null | undefined): AppMode {
  if (typeof raw === 'string' && (APP_MODES as readonly string[]).includes(raw)) {
    const mode = raw as AppMode
    return EPHEMERAL_MODES.has(mode) ? 'clone' : mode
  }
  return 'clone'
}

export function loadAppMode(): AppMode {
  // Direct deep links win. Desktop's `/app` entry restores the last tab from storage.
  if (typeof window !== 'undefined') {
    const path = window.location.pathname.replace(/\/+$/, '') || '/'
    if (path !== '/') {
      const fromPath = appModeFromPath(path)
      if (fromPath) return EPHEMERAL_MODES.has(fromPath) ? 'clone' : fromPath
    }
  }
  try {
    return parseAppMode(localStorage.getItem(APP_MODE_LS))
  } catch {
    return 'clone'
  }
}

export function persistAppMode(mode: AppMode): void {
  if (EPHEMERAL_MODES.has(mode)) return
  try {
    localStorage.setItem(APP_MODE_LS, mode)
  } catch {
    /* quota / private mode */
  }
}

/** ponytail: self-check — invalid/missing → clone; sticky tabs restore */
export function __checkParseAppMode(): void {
  if (parseAppMode(null) !== 'clone') throw new Error('null → clone')
  if (parseAppMode(undefined) !== 'clone') throw new Error('undefined → clone')
  if (parseAppMode('') !== 'clone') throw new Error('empty → clone')
  if (parseAppMode('bogus') !== 'clone') throw new Error('bogus → clone')
  if (parseAppMode('TTS') !== 'clone') throw new Error('wrong case → clone')
  if (parseAppMode('{"mode":"tts"}') !== 'clone') throw new Error('json junk → clone')
  if (parseAppMode('live-preview') !== 'clone') throw new Error('live-preview → clone')
  if (parseAppMode('license') !== 'clone') throw new Error('license → clone')
  for (const m of APP_MODES) {
    if (EPHEMERAL_MODES.has(m)) continue
    if (parseAppMode(m) !== m) throw new Error(`keep ${m}`)
  }
}
