import type { JobStatus, ProjectSettings } from '@/features/project/project.types'

export const SETTINGS_LS = 'zm_ai_tool.settings'
export const SESSION_LS = 'zm_ai_tool.session'
export const SIDEBAR_W_LS = 'zm_ai_tool.sidebarWidth'
export const THEME_LS = 'zm_ai_tool.theme'
export const SETUP_GATE_LS = 'zm_ai_tool.setupGate'

export function loadSetupGate(): boolean {
  try {
    return localStorage.getItem(SETUP_GATE_LS) === '1'
  } catch {
    return false
  }
}

export function persistSetupGate() {
  try {
    localStorage.setItem(SETUP_GATE_LS, '1')
  } catch {
    /* ignore */
  }
}

export const idleStatus: JobStatus = {
  step: 'video',
  progress: 0,
  message: 'Chọn video để bắt đầu',
  running: false,
}

export const SIDEBAR_MIN = 240
export const SIDEBAR_MAX = 560
export const SIDEBAR_DEFAULT = 360

/** Mặc định lần đầu — từng engine khác nhau (user chỉnh sau thì nhớ riêng). */
export const ENGINE_DEFAULTS = {
  whisper: {
    matchDuration: 'preferVideo' as const,
    processOriginalAudio: false,
    originalAudioMode: 'original' as const,
    originalAudioVolume: 100,
  },
  capcut: {
    matchDuration: 'preferVideo' as const,
    processOriginalAudio: false,
    originalAudioMode: 'original' as const,
    originalAudioVolume: 100,
  },
  paddleocr: {
    matchDuration: 'none' as const,
    processOriginalAudio: false,
    originalAudioMode: 'original' as const,
    originalAudioVolume: 100,
  },
  subtitle: {
    matchDuration: 'none' as const,
    processOriginalAudio: false,
    originalAudioMode: 'original' as const,
    originalAudioVolume: 100,
  },
}

export const TRANSLATORS = [
  'google', 'mymemory', 'tiktok', 'capcut', 'ollama', 'openai', 'gemini',
  'deepseek', 'openrouter', 'grok', 'groq', 'nvidia',
] as const

const TRANSLATOR_LABELS: Record<(typeof TRANSLATORS)[number], string> = {
  google: 'Google Translate',
  mymemory: 'MyMemory',
  tiktok: 'TikTok Translate',
  capcut: 'CapCut cloud',
  ollama: 'Ollama',
  openai: 'OpenAI',
  gemini: 'Gemini',
  deepseek: 'DeepSeek',
  openrouter: 'OpenRouter',
  grok: 'Grok (xAI)',
  groq: 'Groq',
  nvidia: 'NVIDIA NIM',
}

export function normalizeTranslatorForEngine(
  engine: ProjectSettings['engine'],
  translator: ProjectSettings['translator'],
): ProjectSettings['translator'] {
  if (engine === 'capcut') return 'capcut'
  return translator === 'capcut' ? 'google' : translator
}

export function availableTranslators(engine: ProjectSettings['engine']) {
  return engine === 'capcut' ? TRANSLATORS : TRANSLATORS.filter((id) => id !== 'capcut')
}

/** Canonical options for every translator picker; never combine display and raw-id lists. */
export function translatorOptions(engine: ProjectSettings['engine']) {
  return availableTranslators(engine).map((id) => ({ id, label: TRANSLATOR_LABELS[id] }))
}

export const defaultSettings: ProjectSettings = {
  engine: 'whisper',
  sourceLang: 'auto',
  targetLang: 'vi',
  translator: 'google',
  ollamaMode: 'cloud',
  ollamaModel: 'minimax-m3:cloud',
  ollamaLocalTier: 'balanced',
  matchDuration: ENGINE_DEFAULTS.whisper.matchDuration,
  defaultVoice: 'cc:BV075_streaming:7102355803792740865',
  stableCaptionLocate: false,
  analysisRegion: null,
  coverLogo: false,
  hiddenLogoTexts: [],
  coverHardsubs: true,
  coverMaskStyle: 'blur',
  coverMaskColor: '#4c1d95',
  coverMaskOpacity: 0,
  burnSubs: true,
  captionPlacement: 'above',
  subtitleFontSize: 0,
  subtitleFontFamily: 'system',
  captionTextColor: '#ffffff',
  captionBgStyle: 'none',
  captionBgColor: '#000000',
  captionBgOpacity: 55,
  captionStroke: true,
  sourceSubtitleVisible: false,
  dubSubtitleVisible: true,
  subtitleExportTrack: 'dub',
  colorAdjust: { brightness: 0, contrast: 0, saturation: 100, temperature: 0, tint: 0 },
  lutAssetId: '',
  processOriginalAudio: ENGINE_DEFAULTS.whisper.processOriginalAudio,
  originalAudioMode: ENGINE_DEFAULTS.whisper.originalAudioMode,
  originalAudioVolume: ENGINE_DEFAULTS.whisper.originalAudioVolume,
  previewSec: 20,
  workers: 0,
  previewAspectRatio: 'original',
  previewCrop: null,
  videoScaleX: 100,
  videoScaleY: 100,
  exportResolution: '1080',
  engineProfiles: {
    whisper: { ...ENGINE_DEFAULTS.whisper },
    capcut: { ...ENGINE_DEFAULTS.capcut },
    paddleocr: { ...ENGINE_DEFAULTS.paddleocr },
    subtitle: { ...ENGINE_DEFAULTS.subtitle },
  },
  blurBandMode: 'auto',
}

export function loadTheme(): boolean {
  try {
    return localStorage.getItem(THEME_LS) === 'dark'
  } catch {
    return false
  }
}

export function loadSidebarWidth(): number {
  try {
    const raw = localStorage.getItem(SIDEBAR_W_LS)
    if (raw != null && raw !== '') {
      const n = Number(raw)
      if (Number.isFinite(n) && n > 0) {
        return Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, n))
      }
    }
  } catch {
    /* ignore */
  }
  return SIDEBAR_MAX
}

export function applyEngineProfile(
  s: ProjectSettings,
  engine: ProjectSettings['engine'],
): ProjectSettings {
  const base = ENGINE_DEFAULTS[engine]
  const saved = s.engineProfiles?.[engine]
  return {
    ...s,
    engine,
    matchDuration: saved?.matchDuration ?? base.matchDuration,
    processOriginalAudio: saved?.processOriginalAudio ?? base.processOriginalAudio,
    originalAudioMode: saved?.originalAudioMode ?? base.originalAudioMode,
    originalAudioVolume: saved?.originalAudioVolume ?? base.originalAudioVolume,
  }
}

/** Ghi profile engine đang active (matchDuration / lọc âm) — không đụng engine kia. */
export function snapshotEngineProfile(s: ProjectSettings): ProjectSettings {
  const eng = s.engine === 'paddleocr' || s.engine === 'subtitle' || s.engine === 'capcut' ? s.engine : 'whisper'
  return {
    ...s,
    engineProfiles: {
      ...s.engineProfiles,
      [eng]: {
        matchDuration: s.matchDuration,
        processOriginalAudio: s.processOriginalAudio,
        originalAudioMode: s.originalAudioMode,
        originalAudioVolume: s.originalAudioVolume,
      },
    },
  }
}

export function loadSettings(): ProjectSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_LS)
    if (!raw) return defaultSettings
    const s = { ...defaultSettings, ...JSON.parse(raw) } as ProjectSettings
    if (typeof s.stableCaptionLocate !== 'boolean') s.stableCaptionLocate = false
    if (typeof s.coverLogo !== 'boolean') s.coverLogo = false
    if (s.analysisRegion != null && typeof s.analysisRegion === 'object') {
      const r = s.analysisRegion as { x?: number; y?: number; w?: number; h?: number }
      const x = Math.max(0, Math.min(1, Number(r.x) || 0))
      const y = Math.max(0, Math.min(1, Number(r.y) || 0))
      const w = Math.max(0.05, Math.min(1 - x, Number(r.w) || 0.9))
      const h = Math.max(0.05, Math.min(1 - y, Number(r.h) || 0.4))
      s.analysisRegion = { x, y, w, h }
    } else {
      s.analysisRegion = null
    }
    if (typeof s.workers !== 'number' || Number.isNaN(s.workers) || s.workers < 0) s.workers = 0
    if (typeof s.originalAudioVolume !== 'number' || Number.isNaN(s.originalAudioVolume)) {
      s.originalAudioVolume = 100
    } else {
      s.originalAudioVolume = Math.max(0, Math.min(200, s.originalAudioVolume))
    }
    if (!TRANSLATORS.includes(s.translator as (typeof TRANSLATORS)[number])) s.translator = 'google'
    if (s.ollamaMode !== 'local' && s.ollamaMode !== 'cloud') s.ollamaMode = 'cloud'
    if (typeof s.ollamaModel !== 'string' || !s.ollamaModel.trim()) {
      s.ollamaModel = 'minimax-m3:cloud'
    }
    if (!['fast', 'balanced', 'quality'].includes(s.ollamaLocalTier)) {
      s.ollamaLocalTier = 'balanced'
    }
    const okMask = ['blur', 'feather', 'solid', 'mosaic'] as const
    if (!okMask.includes(s.coverMaskStyle as (typeof okMask)[number])) s.coverMaskStyle = 'blur'
    if (typeof s.coverMaskOpacity !== 'number' || Number.isNaN(s.coverMaskOpacity)) {
      s.coverMaskOpacity = 0
    } else {
      s.coverMaskOpacity = Math.max(0, Math.min(100, s.coverMaskOpacity))
    }
    if (typeof s.coverMaskColor !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(s.coverMaskColor)) {
      s.coverMaskColor = '#4c1d95'
    }
    const okFont = [
      'system', 'segoe', 'arial', 'bold', 'helvetica', 'verdana', 'tahoma',
      'trebuchet', 'rounded', 'impact', 'georgia', 'times', 'palatino', 'garamond',
      'courier', 'mono', 'comic', 'cjk', 'meiryo', 'malgun',
    ] as const
    if (!okFont.includes(s.subtitleFontFamily as (typeof okFont)[number])) {
      s.subtitleFontFamily = 'system'
    }
    if (typeof s.captionTextColor !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(s.captionTextColor)) {
      s.captionTextColor = '#ffffff'
    }
    const okBg = ['none', 'solid', 'blur', 'box'] as const
    if (!okBg.includes(s.captionBgStyle as (typeof okBg)[number])) s.captionBgStyle = 'none'
    if (typeof s.captionBgColor !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(s.captionBgColor)) {
      s.captionBgColor = '#000000'
    }
    if (typeof s.captionBgOpacity !== 'number' || Number.isNaN(s.captionBgOpacity)) {
      s.captionBgOpacity = 55
    } else {
      s.captionBgOpacity = Math.max(0, Math.min(100, s.captionBgOpacity))
    }
    if (typeof s.captionStroke !== 'boolean') s.captionStroke = true
    const okAspect = [
      'original', 'custom', '16:9', '4:3', '2.35:1', '2:1', '1.85:1',
      '9:16', '3:4', '58inch', '1:1',
    ] as const
    if (!okAspect.includes(s.previewAspectRatio as (typeof okAspect)[number])) {
      s.previewAspectRatio = 'original'
    }
    const legacyVideoScale = typeof s.videoScale === 'number' && Number.isFinite(s.videoScale)
      ? s.videoScale
      : 100
    s.videoScaleX = typeof s.videoScaleX === 'number' && Number.isFinite(s.videoScaleX)
      ? Math.max(1, Math.min(500, s.videoScaleX))
      : Math.max(1, Math.min(500, legacyVideoScale))
    s.videoScaleY = typeof s.videoScaleY === 'number' && Number.isFinite(s.videoScaleY)
      ? Math.max(1, Math.min(500, s.videoScaleY))
      : Math.max(1, Math.min(500, legacyVideoScale))
    const okResolution = ['144', '240', '360', '480', '720', '1080', '1440', '2160', 'original'] as const
    if (!okResolution.includes(s.exportResolution as (typeof okResolution)[number])) {
      s.exportResolution = '1080'
    }
    const okMatch = ['preferVideo', 'none', 'natural', 'stretch'] as const
    if (!okMatch.includes(s.matchDuration as (typeof okMatch)[number])) {
      s.matchDuration = 'preferVideo'
    }
    if (s.blurBandMode !== 'off' && s.blurBandMode !== 'manual') {
      s.blurBandMode = 'auto'
    }
    // Migrate projects saved while SenseVoice existed back to Whisper.
    const eng = s.engine === 'paddleocr' || s.engine === 'subtitle' || s.engine === 'capcut' ? s.engine : 'whisper'
    s.translator = normalizeTranslatorForEngine(eng, s.translator)
    const profiles = {
      whisper: {
        ...ENGINE_DEFAULTS.whisper,
        ...s.engineProfiles?.whisper,
      },
      capcut: {
        ...ENGINE_DEFAULTS.capcut,
        ...s.engineProfiles?.capcut,
      },
      paddleocr: {
        ...ENGINE_DEFAULTS.paddleocr,
        ...s.engineProfiles?.paddleocr,
      },
      subtitle: {
        ...ENGINE_DEFAULTS.subtitle,
        ...s.engineProfiles?.subtitle,
      },
    }
    if (!s.engineProfiles?.[eng]) {
      profiles[eng] = {
        matchDuration: s.matchDuration,
        processOriginalAudio: s.processOriginalAudio,
        originalAudioMode: s.originalAudioMode,
        originalAudioVolume: s.originalAudioVolume,
      }
    }
    s.engineProfiles = profiles
    const active = profiles[eng]
    s.matchDuration = active.matchDuration ?? ENGINE_DEFAULTS[eng].matchDuration
    s.processOriginalAudio = active.processOriginalAudio ?? ENGINE_DEFAULTS[eng].processOriginalAudio
    s.originalAudioMode = active.originalAudioMode ?? ENGINE_DEFAULTS[eng].originalAudioMode
    s.originalAudioVolume = active.originalAudioVolume ?? ENGINE_DEFAULTS[eng].originalAudioVolume
    return s
  } catch {
    return defaultSettings
  }
}

export function persistSettings(s: ProjectSettings) {
  try {
    localStorage.setItem(SETTINGS_LS, JSON.stringify(snapshotEngineProfile(s)))
  } catch {
    /* quota / private mode */
  }
}

export function persistSession(projectId: string | null) {
  try {
    if (projectId) localStorage.setItem(SESSION_LS, projectId)
    else localStorage.removeItem(SESSION_LS)
  } catch {
    /* ignore */
  }
}
