import { createContext, useContext, useEffect } from 'react'
import englishCatalog from './ui.en.json'

export type AppLocale = 'vi' | 'en'

export const LOCALE_LS = 'zm_ai_tool.locale'

export function detectLocale(): AppLocale {
  try {
    const languages = navigator.languages?.length ? navigator.languages : [navigator.language]
    return languages.some((language) => language?.toLowerCase().startsWith('vi')) ? 'vi' : 'en'
  } catch {
    return 'vi'
  }
}

export function loadLocale(): AppLocale {
  try {
    const saved = localStorage.getItem(LOCALE_LS)
    if (saved === 'vi' || saved === 'en') return saved
  } catch {
    /* private mode */
  }
  return detectLocale()
}

export function persistLocale(locale: AppLocale) {
  try { localStorage.setItem(LOCALE_LS, locale) } catch { /* private mode */ }
  document.cookie = `zm_ai_tool_locale=${locale}; path=/; SameSite=Lax`
}

type LocaleContextValue = { locale: AppLocale; setLocale: (locale: AppLocale) => void }
export const LocaleContext = createContext<LocaleContextValue>({ locale: 'vi', setLocale: () => {} })

const MESSAGES = {
  'brand.tagline': { vi: 'Studio Dịch Thuật & Ghép & Lồng Tiếng AI', en: 'AI Translation, Video Cloning & Dubbing Studio' },
  'nav.clone': { vi: 'Clone / Review', en: 'Clone / Review' },
  'nav.cloneVideo': { vi: 'Clone Video', en: 'Clone Video' },
  'nav.review': { vi: 'Review Phim', en: 'Movie Review' },
  'nav.batch': { vi: 'Hàng loạt', en: 'Batch' },
  'nav.flow': { vi: 'Flow', en: 'Flow' },
  'nav.chat': { vi: 'Chat AI', en: 'AI Chat' },
  'nav.automation': { vi: 'Tự động hoá', en: 'Automation' },
  'nav.livePreview': { vi: 'Live Preview', en: 'Live Preview' },
  'nav.renders': { vi: 'List render', en: 'Render list' },
  'nav.download': { vi: 'Download Video', en: 'Download Video' },
  'nav.tts': { vi: 'Text to Speech', en: 'Text to Speech' },
  'nav.tools': { vi: 'Tools', en: 'Tools' },
  'nav.settings': { vi: 'Cấu hình', en: 'Settings' },
  'tools.downloadVideo': { vi: 'Download Video', en: 'Download Video' },
  'tools.cleanVideo': { vi: 'Làm sạch video', en: 'Clean video' },
  'tools.srtImage': { vi: 'Ghép ảnh/video SRT', en: 'Create SRT image/video' },
  'tools.exportSubtitles': { vi: 'Xuất phụ đề', en: 'Export subtitles' },
  'tools.drawing': { vi: 'Vẽ tay', en: 'Drawing' },
  'header.openTtsMenu': { vi: 'Mở menu TTS', en: 'Open TTS menu' },
  'header.closeTtsMenu': { vi: 'Đóng menu TTS', en: 'Close TTS menu' },
  'header.interfaceLanguage': { vi: 'Ngôn ngữ giao diện', en: 'Interface language' },
  'header.unlimited': { vi: 'Không giới hạn', en: 'Unlimited' },
  'header.daysLeft': { vi: '{count} ngày còn lại', en: '{count} days left' },
  'header.expires': { vi: 'Hết hạn: {date}', en: 'Expires: {date}' },
  'header.switchLight': { vi: 'Chuyển sang giao diện sáng', en: 'Switch to light mode' },
  'header.switchDark': { vi: 'Chuyển sang giao diện tối', en: 'Switch to dark mode' },
  'header.back': { vi: 'Quay lại', en: 'Back' },
  'error.cloud.apiKeyMissing': { vi: '{provider}: chưa có API key. Mở Cấu hình → API dịch cloud.', en: '{provider}: no API key is configured. Open Settings → Cloud translation APIs.' },
  'error.cloud.authFailed': { vi: '{provider}: API key bị từ chối hoặc không có quyền dùng model.', en: '{provider}: the API key was rejected or cannot access this model.' },
  'error.cloud.rateLimited': { vi: '{provider}: hết quota hoặc đang bị giới hạn tốc độ. Thử lại sau hoặc thêm key khác.', en: '{provider}: quota is exhausted or rate-limited. Retry later or add another key.' },
  'error.cloud.serviceUnavailable': { vi: '{provider}: dịch vụ hoặc mạng hiện không khả dụng. Thử lại sau.', en: '{provider}: the service or network is unavailable. Retry later.' },
  'error.cloud.modelInvalid': { vi: '{provider}: model hoặc yêu cầu không hợp lệ. Kiểm tra lại model trong Cấu hình.', en: '{provider}: the model or request is invalid. Check the model in Settings.' },
  'error.cloud.invalidResponse': { vi: '{provider}: phản hồi dịch không hợp lệ. Thử lại hoặc chọn model khác.', en: '{provider}: the translation response is invalid. Retry or choose another model.' },
  'error.cloud.languagePair': { vi: '{provider}: model hiện tại không hỗ trợ cặp ngôn ngữ này.', en: '{provider}: the current model does not support this language pair.' },
} as const

export type MessageKey = keyof typeof MESSAGES

export function translate(locale: AppLocale, key: MessageKey, values: Record<string, string | number> = {}): string {
  return MESSAGES[key][locale].replace(/\{(\w+)\}/g, (_, name: string) => String(values[name] ?? `{${name}}`))
}

export function useLocale() {
  return useContext(LocaleContext)
}

export function useT() {
  const { locale } = useLocale()
  return (key: MessageKey, values?: Record<string, string | number>) => translate(locale, key, values)
}

const TEXT_ATTRIBUTES = ['aria-label', 'placeholder', 'title'] as const
const originalText = new WeakMap<Text, string>()
const originalAttrs = new WeakMap<Element, Map<string, string>>()

/**
 * ponytail: existing UI predates i18n and contains hundreds of JSX literals.
 * This bridge uses the checked-in catalog while modules are migrated to keys,
 * so changing language never relies on an online translation service.
 */
export function LocaleTextSync() {
  const { locale } = useLocale()
  useEffect(() => {
    const translate = (text: string) => englishCatalog[text as keyof typeof englishCatalog]
    const applyText = (node: Text) => {
      const value = node.nodeValue || ''
      const leading = value.match(/^\s*/)?.[0] || ''
      const trailing = value.match(/\s*$/)?.[0] || ''
      const content = value.slice(leading.length, value.length - trailing.length)
      const original = originalText.get(node)
      if (locale === 'en') {
        const english = translate(content)
        if (english && english !== content) {
          originalText.set(node, content)
          node.nodeValue = `${leading}${english}${trailing}`
        }
      } else if (original && content === translate(original)) {
        node.nodeValue = `${leading}${original}${trailing}`
      }
    }
    const applyElement = (element: Element) => {
      if (element.closest('script, style, code, pre')) return
      for (const attribute of TEXT_ATTRIBUTES) {
        const value = element.getAttribute(attribute)
        if (!value) continue
        const saved = originalAttrs.get(element)?.get(attribute)
        if (locale === 'en') {
          const english = translate(value)
          if (english && english !== value) {
            const attrs = originalAttrs.get(element) || new Map<string, string>()
            attrs.set(attribute, value)
            originalAttrs.set(element, attrs)
            element.setAttribute(attribute, english)
          }
        } else if (saved && value === translate(saved)) {
          element.setAttribute(attribute, saved)
        }
      }
      for (const child of element.childNodes) if (child.nodeType === Node.TEXT_NODE) applyText(child as Text)
    }
    const apply = (root: Node) => {
      if (root.nodeType === Node.TEXT_NODE) applyText(root as Text)
      if (root.nodeType === Node.ELEMENT_NODE) {
        const element = root as Element
        applyElement(element)
        for (const child of element.querySelectorAll('*')) applyElement(child)
      }
    }
    apply(document.body)
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        if (record.type === 'characterData') applyText(record.target as Text)
        else if (record.type === 'attributes') applyElement(record.target as Element)
        else for (const node of record.addedNodes) apply(node)
      }
    })
    observer.observe(document.body, {
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: [...TEXT_ATTRIBUTES],
      subtree: true,
    })
    return () => observer.disconnect()
  }, [locale])
  return null
}

export function localize(locale: AppLocale, vietnamese: string, english: string): string {
  return locale === 'en' ? english : vietnamese
}

/** Backend job messages include dynamic counts, so they cannot use the static catalog. */
export function localizePipelineMessage(locale: AppLocale, message: string): string {
  const cloud = /^CLOUD_TRANSLATION_([A-Z0-9_]+)_(API_KEY_MISSING|AUTH_FAILED|ACCESS_DENIED|RATE_LIMITED_OR_QUOTA|NETWORK_UNAVAILABLE|SERVICE_UNAVAILABLE|MODEL_OR_REQUEST_INVALID|INVALID_RESPONSE|UNSUPPORTED_LANGUAGE_PAIR|REQUEST_FAILED)$/.exec(message)
  if (cloud) {
    const provider = ({ OPENAI: 'OpenAI', GEMINI: 'Gemini', DEEPSEEK: 'DeepSeek', OPENROUTER: 'OpenRouter', GROK: 'Grok', GROQ: 'Groq', NVIDIA: 'NVIDIA NIM' } as Record<string, string>)[cloud[1]] || cloud[1]
    const key = ({
      API_KEY_MISSING: 'error.cloud.apiKeyMissing',
      AUTH_FAILED: 'error.cloud.authFailed',
      ACCESS_DENIED: 'error.cloud.authFailed',
      RATE_LIMITED_OR_QUOTA: 'error.cloud.rateLimited',
      NETWORK_UNAVAILABLE: 'error.cloud.serviceUnavailable',
      SERVICE_UNAVAILABLE: 'error.cloud.serviceUnavailable',
      MODEL_OR_REQUEST_INVALID: 'error.cloud.modelInvalid',
      INVALID_RESPONSE: 'error.cloud.invalidResponse',
      UNSUPPORTED_LANGUAGE_PAIR: 'error.cloud.languagePair',
      REQUEST_FAILED: 'error.cloud.serviceUnavailable',
    } as Record<string, MessageKey>)[cloud[2]]
    return key ? translate(locale, key, { provider }) : message
  }
  if (message === 'CAPCUT_TRANSLATOR_REQUIRES_CAPCUT_ASR') {
    return localize(locale,
      'CapCut cloud chỉ hỗ trợ dịch cùng nhận dạng CapCut. Chọn nhận dạng CapCut hoặc công cụ dịch khác.',
      'CapCut cloud translation requires CapCut recognition. Choose CapCut recognition or another translation provider.',
    )
  }
  if (locale !== 'en') return message
  return message
    .replace(/^Cache định vị OCR — (\d+) đoạn$/u, 'OCR location cache — $1 segments')
    .replace(/^REVIEW_CLOUD_GEMINI_HTTP_403$/g, 'Gemini rejected this request. Check the API key, its project, and Gemini API access in Settings → Cloud.')
    .replace(/^REVIEW_CLOUD_GEMINI_API_KEY_MISSING$/g, 'Gemini has no API key. Add one in Settings → Cloud, then retry.')
    .replace(/^REVIEW_CLOUD_GEMINI_AUTH_FAILED$/g, 'Gemini rejected the API key or it lacks Gemini API access. Check the key and project in Settings → Cloud.')
    .replace(/^REVIEW_CLOUD_GEMINI_ACCESS_DENIED$/g, 'Gemini denied access to this request. Check the API key and Gemini API access in Settings → Cloud.')
    .replace(/^REVIEW_CLOUD_GEMINI_RATE_LIMITED_OR_QUOTA$/g, 'Gemini is out of quota or rate-limited. Wait for quota to become available, then retry.')
    .replace(/^REVIEW_CLOUD_GEMINI_MODEL_OR_REQUEST_INVALID$/g, 'The Gemini Review analysis model is invalid. Use a Gemini API model such as gemini-2.5-flash in Settings → Cloud.')
    .replace(/^REVIEW_CLOUD_GEMINI_NETWORK_UNAVAILABLE$/g, 'Gemini could not be reached. Check the network and base URL in Settings → Cloud, then retry.')
    .replace(/^REVIEW_CLOUD_GEMINI_SERVICE_UNAVAILABLE$/g, 'Gemini is temporarily unavailable. Retry shortly.')
    .replace(/^REVIEW_CLOUD_GEMINI_INVALID_RESPONSE$/g, 'Gemini returned an invalid response. Retry, or choose another Gemini Review model.')
    .replace(/^REVIEW_CLOUD_GEMINI_UNAVAILABLE$/g, 'Gemini is temporarily unreachable. Check the network, base URL, and API key in Settings → Cloud, then retry.')
    .replace(/^Review script: AI dựng mạch từ timeline thoại · model (.+)$/g, 'Review script: AI builds an arc from the speech timeline · model $1')
    .replace(/^Lập chỉ mục cảnh: (.+)$/g, 'Indexing scenes: $1')
    .replace(/^Chỉ mục cảnh: (\d+) cảnh · (\d+)s$/g, 'Scene index: $1 scenes · $2s')
    .replace(/^Tóm tắt mốc thoại: (\d+)\/(\d+) \((\d+)%\) · tiến trình (\d+)% · (\d+) luồng$/g, 'Summarizing speech beats: $1/$2 ($3%) · overall $4% · $5 workers')
    .replace(/^Kịch bản phần (\d+): (\d+) đoạn · mục tiêu tự nhiên (\d+)s · (\d+)s(?: · cache)?$/g, 'Part $1 script: $2 beats · natural target $3s · $4s')
    .replace(/^Lập chỉ mục cảnh & gắn transcript: (\d+)\/(\d+) cảnh \((\d+)%\)$/g, 'Indexing scenes and attaching transcript: $1/$2 scenes ($3%)')
    .replace(/^LLM trả kịch bản ngắn hơn mục tiêu — ưu tiên mạch review tự nhiên, không kéo dài bằng transcript\.$/g, 'LLM returned a shorter script — keeping a natural review flow instead of padding with transcript.')
    .replace(/^CapCut: đang chuẩn bị video…$/g, 'CapCut: preparing video…')
    .replace(/^CapCut: đang tải video lên…$/g, 'CapCut: uploading video…')
    .replace(/^CapCut: đang nhận dạng và dịch…$/g, 'CapCut: recognizing and translating…')
    .replace(/^CapCut: đang gửi video cho CapCut…$/g, 'Transcript: sending video to CapCut…')
    .replace(/^Transcript: CapCut hoàn tất · (\d+) câu$/g, 'Transcript: CapCut completed · $1 cues')
    .replace(/^CapCut: CapCut đang nhận dạng và dịch · (\d+)% · đã chờ (\d+)s · kiểm tra #(\d+)$/g, 'CapCut: recognizing and translating · $1% · waited $2s · check #$3')
    .replace(/^CapCut: CapCut đã hoàn tất · (\d+)% · đã chờ (\d+)s · kiểm tra #(\d+)$/g, 'CapCut: completed · $1% · waited $2s · check #$3')
    .replace(/^CapCut: đang xếp hàng trên CapCut( · \d+%)? · đã chờ (\d+)s · kiểm tra #(\d+)$/g, 'CapCut: queued on CapCut$1 · waited $2s · check #$3')
    .replace(/^CapCut: đang chờ CapCut xử lý( · \d+%)? · đã chờ (\d+)s · kiểm tra #(\d+)$/g, 'CapCut: waiting for CapCut to process$1 · waited $2s · check #$3')
    .replace(/^CapCut: đang chờ phản hồi từ CapCut · đã chờ (\d+)s · kiểm tra #(\d+)$/g, 'CapCut: waiting for a CapCut response · waited $1s · check #$2')
    .replace(/Xong (\d+) đoạn — tiếp theo: Lồng tiếng → Xuất bản/g, 'Completed $1 segments — next: Dubbing → Export')
    .replace(/Dùng phụ đề SRT: (\d+) đoạn — tiếp theo: Lồng tiếng → Xuất bản/g, 'Using SRT subtitles: $1 segments — next: Dubbing → Export')
    .replace(/Xong (\d+) đoạn — không dịch, không chèn caption/g, 'Completed $1 segments — no translation or captions added')
    .replace(/Xong (\d+) đoạn — bấm Xuất bản để che chữ cũ \+ đè bản dịch/g, 'Completed $1 segments — click Export to cover original text and burn in the translation')
}
