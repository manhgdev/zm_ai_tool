import type { AppMode } from '@/shared/components/Header'

export const APP_MODE_LS = 'zm_ai_tool.appMode'

export const APP_MODES = ['clone', 'live-preview', 'renders', 'film', 'batch', 'flow', 'chat', 'automation', 'cleaner', 'srt-image', 'srt-export', 'drawing', 'download', 'tts'] as const satisfies readonly AppMode[]

/**
 * Mỗi màn cấp ứng dụng có một URL ổn định.  Trước đây Download chỉ sống trong
 * localStorage nên mở /download-video vẫn có thể rơi vào tab đã dùng lần trước.
 */
export const APP_MODE_PATHS: Record<AppMode, string> = {
  clone: '/',
  'live-preview': '/live-preview',
  renders: '/renders',
  download: '/download-video',
  tts: '/text-to-speech',
  cleaner: '/video-cleaner',
  'srt-image': '/subtitle-image',
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

/** Validate raw storage — invalid/corrupt/missing → Clone Video. */
export function parseAppMode(raw: string | null | undefined): AppMode {
  if (typeof raw === 'string' && (APP_MODES as readonly string[]).includes(raw)) {
    return raw as AppMode
  }
  return 'clone'
}

export function loadAppMode(): AppMode {
  // URL được ưu tiên để link trực tiếp (đặc biệt /download-video) luôn đúng.
  if (typeof window !== 'undefined') {
    const fromPath = appModeFromPath(window.location.pathname)
    if (fromPath && window.location.pathname !== '/') return fromPath
  }
  try {
    return parseAppMode(localStorage.getItem(APP_MODE_LS))
  } catch {
    return 'clone'
  }
}

export function persistAppMode(mode: AppMode): void {
  try {
    localStorage.setItem(APP_MODE_LS, mode)
  } catch {
    /* quota / private mode */
  }
}

/** ponytail: self-check — invalid/missing → clone; all tabs restore */
export function __checkParseAppMode(): void {
  if (parseAppMode(null) !== 'clone') throw new Error('null → clone')
  if (parseAppMode(undefined) !== 'clone') throw new Error('undefined → clone')
  if (parseAppMode('') !== 'clone') throw new Error('empty → clone')
  if (parseAppMode('bogus') !== 'clone') throw new Error('bogus → clone')
  if (parseAppMode('TTS') !== 'clone') throw new Error('wrong case → clone')
  if (parseAppMode('{"mode":"tts"}') !== 'clone') throw new Error('json junk → clone')
  for (const m of APP_MODES) {
    if (parseAppMode(m) !== m) throw new Error(`keep ${m}`)
  }
}
