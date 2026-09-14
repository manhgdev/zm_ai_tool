import { useEffect, useRef, useState } from 'react'
import { localize, useLocale } from '@/app/i18n'
import { copyText } from '@/shared/lib/clipboard'
import { PromptDialog } from '@/shared/components/PromptDialog'
import './ChatPage.css'

type Conversation = { id: string; title: string; account_id: string; provider_id?: string; model: string }
type ChatArtifact = { id: string; name: string; kind: string; content_type: string; url: string }
type Message = { id: string; role: 'user' | 'assistant'; content: string; status: string; error?: string; attachments?: ChatArtifact[] }
type Account = { id: string; label: string; configured: boolean; experimental: boolean; status: string; email?: string; last_model?: string; error?: string; errorCode?: string }
type ChatModel = { id: string; label: string; provider: string; free: boolean; capabilities: string[]; available: boolean; reason?: string }
type ChatProvider = { id: string; label: string; kind: 'api' | 'browser'; configured: boolean; status: string; capabilities: string[]; models: ChatModel[]; errorCode?: string; reason?: string }
type ChatMode = 'chat' | 'search' | 'research' | 'image'

const API = '/api/chat'
const LAST_ACCOUNT = 'zm_ai_tool.chat.account'
const LAST_PROVIDER = 'zm_ai_tool.chat.provider'
const LAST_MODEL = 'zm_ai_tool.chat.model'
const DEFAULT_MODEL = 'GPT-5.6 Sol'
const DEFAULT_PROVIDER = 'openrouter'
const modelCache = new Map<string, ChatModel[]>()
const modelRequests = new Map<string, Promise<ChatModel[]>>()

function fetchChatModels(provider: string) {
  const pending = modelRequests.get(provider)
  if (pending) return pending
  const request = fetch(`${API}/models?provider=${encodeURIComponent(provider)}`).then(async response => {
    if (!response.ok) throw new Error('models')
    return response.json() as Promise<{ models?: unknown; errorCode?: string; reason?: string }>
  }).then(data => {
    if (data.errorCode) throw new Error(data.errorCode)
    const available = Array.isArray(data.models) ? data.models.reduce<ChatModel[]>((result, item) => {
      if (!item || typeof item !== 'object') return result
      const row = item as Record<string, unknown>
      const id = String(row.id || '').trim()
      if (!id || result.some(current => current.id.toLowerCase() === id.toLowerCase())) return result
      result.push({ id, label: String(row.label || id), provider: String(row.provider || provider), free: row.free !== false, capabilities: Array.isArray(row.capabilities) ? row.capabilities.map(String) : ['text'], available: row.available !== false, reason: row.reason ? String(row.reason) : undefined })
      return result
    }, []) : []
    modelCache.set(provider, available)
    return available
  }).finally(() => { modelRequests.delete(provider) })
  modelRequests.set(provider, request)
  return request
}

function MessageBody({ content }: { content: string }) {
  const parts = content.split(/(```[\s\S]*?```)/g)
  return <>{parts.map((part, index) => part.startsWith('```')
    ? <pre key={index}><code>{part.replace(/^```[^\n]*\n?/, '').replace(/```$/, '')}</code><button type="button" onClick={() => navigator.clipboard.writeText(part.replace(/^```[^\n]*\n?/, '').replace(/```$/, ''))}>Copy</button></pre>
    : <span key={index} className="chat-text">{part.split(/(https?:\/\/[^\s]+)/g).map((text, linkIndex) => /^https?:\/\//.test(text)
      ? <a key={linkIndex} href={text} target="_blank" rel="noopener noreferrer">{text}</a>
      : text)}</span>)}</>
}

export default function ChatPage({ onOpenConfig: _onOpenConfig }: { onOpenConfig: () => void }) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [active, setActive] = useState<string>('')
  const [messages, setMessages] = useState<Message[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [providers, setProviders] = useState<ChatProvider[]>([])
  const [account, setAccount] = useState(() => {
    const saved = localStorage.getItem(LAST_ACCOUNT) || ''
    // `openai_api` belonged to the removed multi-provider skeleton. Keeping
    // it here makes the first health check target a non-existent account and
    // looks like the Codex session was reset.
    return saved === 'openai_api' ? '' : saved
  })
  const [provider, setProvider] = useState(() => localStorage.getItem(LAST_PROVIDER) || '')
  const [models, setModels] = useState<ChatModel[]>([])
  const [model, setModel] = useState(() => {
    const saved = localStorage.getItem(LAST_MODEL) || ''
    // GPT-5.5 was the old built-in fallback. Migrate that stale default while
    // preserving any model the user explicitly saved afterwards.
    return saved === 'GPT-5.5' ? DEFAULT_MODEL : saved
  })
  const [modelsLoading, setModelsLoading] = useState(false)
  const [modelsError, setModelsError] = useState(false)
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [mode, setMode] = useState<ChatMode>('chat')
  const [attachmentIds, setAttachmentIds] = useState<string[]>([])
  const [attachmentNames, setAttachmentNames] = useState<string[]>([])
  const [railOpen, setRailOpen] = useState(false)
  const [renameTarget, setRenameTarget] = useState<Conversation | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const autoScroll = useRef(true)
  const healthInFlight = useRef(false)
  const accountName = (_item: Account) => t('ChatGPT Codex', 'ChatGPT Codex')
  const accountState = (item: Account) => item.status === 'connected' && item.configured
    ? t('Phiên Codex đang hoạt động', 'Codex session active')
    : item.status === 'connecting'
      ? t('Đang chờ đăng nhập trong Chrome', 'Waiting for Chrome sign-in')
      : item.status === 'reauth_required'
      ? t('Chưa đăng nhập hoặc phiên đã hết hạn', 'Not signed in or session expired')
      : item.status === 'unavailable'
        ? t('Kiểm tra phiên thất bại', 'Session check failed')
        : t('Chưa đăng nhập', 'Not signed in')
  const accountError = (item: Account) => item.errorCode === 'CHAT_CHROME_REQUIRED'
    ? t('Không tìm thấy Google Chrome. Hãy cài Chrome như yêu cầu của FLOW‑VEO.', 'Google Chrome was not found. Install Chrome as required by FLOW‑VEO.')
    : item.errorCode === 'CHATGPT_LOGIN_IN_PROGRESS'
      ? t('Cửa sổ đăng nhập ChatGPT Codex đang mở.', 'The ChatGPT Codex sign-in window is already open.')
    : item.errorCode === 'CHATGPT_TOKEN_CHECK_FAILED'
      ? t('Không thể kiểm tra token ChatGPT Codex lúc này. Phiên đã lưu vẫn được giữ nguyên.', 'Could not check the ChatGPT Codex token right now. The saved session was kept.')
    : item.errorCode === 'CHATGPT_LOGIN_BROWSER_FAILED'
      ? t('Cửa sổ đăng nhập ChatGPT Codex đã đóng hoặc gặp lỗi. Profile vẫn được giữ lại.', 'The ChatGPT Codex sign-in window closed or failed. The profile was kept.')
      : item.errorCode
        ? t(`Phiên ChatGPT Codex không khả dụng.${item.error ? ` Chi tiết: ${item.error.slice(0, 220)}` : ''}`, `ChatGPT Codex session is unavailable.${item.error ? ` Details: ${item.error.slice(0, 220)}` : ''}`)
        : ''
  const errorText = (value: unknown) => {
    const code = String(value || '')
    if (code.includes('CHAT_CHROME_REQUIRED')) return t('Không tìm thấy Google Chrome. Hãy cài Chrome như yêu cầu của FLOW‑VEO.', 'Google Chrome was not found. Install Chrome as required by FLOW‑VEO.')
    if (code.includes('CHATGPT_LOGIN_IN_PROGRESS')) return t('Cửa sổ đăng nhập ChatGPT Codex đang mở.', 'The ChatGPT Codex sign-in window is already open.')
    if (code.includes('CHAT_PROVIDER_KEY_MISSING')) return t('Provider chưa có API key trong Cấu hình → Cloud.', 'This provider has no API key in Settings → Cloud.')
    if (code.includes('CHAT_FREE_MODEL_UNAVAILABLE')) return t('Provider này hiện không có model khả dụng.', 'This provider has no available model.')
    if (code.includes('CHAT_ATTACHMENT_UNSUPPORTED')) return t('Model đã chọn không hỗ trợ loại file đính kèm này.', 'The selected model does not support this attachment type.')
    if (code.includes('CHAT_PROVIDER_CAPABILITY_UNAVAILABLE')) return t('Tính năng này chưa khả dụng với ChatGPT Codex.', 'This feature is not available with ChatGPT Codex.')
    if (code.includes('CHAT_PROVIDER_HTTP_429')) return t('Provider đã hết quota hoặc đang giới hạn tốc độ. Hãy chọn model khả dụng khác rồi thử lại.', 'The provider is rate-limited or out of quota. Choose another available model and retry.')
    if (code.includes('CHAT_PROVIDER_HTTP_401') || code.includes('CHAT_PROVIDER_HTTP_403')) return t('API key hoặc quyền provider không hợp lệ. Kiểm tra lại Cấu hình → Cloud.', 'The provider API key or permission is invalid. Check Settings → Cloud.')
    if (code.includes('CHAT_PROVIDER_MODELS_UNAVAILABLE')) return t('Không tải được danh sách model của provider. Hãy bấm làm mới rồi thử lại.', 'Could not load this provider’s model list. Refresh and try again.')
    if (code.includes('CHAT_PROVIDER_STREAM_UNAVAILABLE')) return t('Provider không trả được luồng phản hồi. Hãy chọn model khác rồi thử lại.', 'The provider did not return a response stream. Choose another model and retry.')
    if (code.includes('CHAT_PROVIDER_EMPTY_RESPONSE')) return t('Provider kết thúc nhưng không trả về nội dung. Hãy chọn model khác rồi thử lại.', 'The provider completed without returning content. Choose another model and retry.')
    if (code.includes('CHAT_STREAM_INTERRUPTED')) return t('Kết nối phản hồi bị ngắt trước khi hoàn tất. Hãy thử lại.', 'The response connection ended before completion. Please retry.')
    if (code.includes('CHATGPT_LOGIN_REQUIRED') || code.includes('CHAT_MODEL_UNAVAILABLE')) return t('Phiên ChatGPT Codex hoặc model đã chọn không khả dụng. Hãy đăng nhập lại.', 'The ChatGPT Codex session or selected model is unavailable. Sign in again.')
    if (code.includes('CHAT_CONVERSATION_SETTINGS_FAILED')) return t('Không lưu được model cho cuộc trò chuyện này. Hãy thử lại.', 'Could not save the model for this conversation. Try again.')
    if (code.includes('ACCOUNT_NOT_FOUND')) return t('Không tìm thấy tài khoản ChatGPT Codex.', 'ChatGPT Codex account was not found.')
    const detail = code.replace(/^Error:\s*/i, '').replace(/\s+/g, ' ').slice(0, 320)
    const providerLabel = ({
      chatgpt_web: t('ChatGPT Codex', 'ChatGPT Codex'), openai: t('OpenAI API', 'OpenAI API'),
      gemini: 'Gemini', deepseek: 'DeepSeek', openrouter: 'OpenRouter', grok: 'Grok (xAI)', groq: 'Groq', nvidia: t('NVIDIA NIM', 'NVIDIA NIM'),
    } as Record<string, string>)[provider] || t('provider AI', 'AI provider')
    return detail
      ? t(`Không thể xử lý yêu cầu với ${providerLabel}. Chi tiết: ${detail}`, `Could not process the request with ${providerLabel}. Details: ${detail}`)
      : t(`Không thể xử lý yêu cầu với ${providerLabel}.`, `Could not process the request with ${providerLabel}.`)
  }
  const activeAccount = accounts[0]
  const accountReady = activeAccount?.id === account && activeAccount.configured
  const activeProvider = providers.find(item => item.id === provider)
  const providerReady = provider === 'chatgpt_web' ? accountReady : Boolean(activeProvider?.configured && activeProvider.status === 'ready')

  const loadList = () => fetch(`${API}/conversations`).then(r => r.json()).then(setConversations)
  const openConversation = (id: string) => {
    setActive(id)
    fetch(`${API}/conversations/${id}`).then(r => r.json()).then(data => {
      setMessages(data.messages || [])
      const savedProvider = data.provider_id || (data.account_id === accounts[0]?.id ? 'chatgpt_web' : (data.account_id === 'openai_api' ? 'openai' : data.account_id))
      if (savedProvider) setProvider(savedProvider)
      if (data.account_id === accounts[0]?.id && accounts[0]?.id) setAccount(accounts[0].id)
      setModel(String(data.model || ''))
    })
  }
  const refreshAccounts = () => fetch(`${API}/accounts`).then(async r => {
    if (!r.ok) throw new Error(await r.text())
    return r.json() as Promise<Account[]>
  }).then(items => {
    setAccounts(items)
    setAccount(current => items.some(item => item.id === current) ? current : (items[0]?.id || ''))
    return items
  })
  const refreshProviders = (force = false) => fetch(`${API}/providers${force ? '?refresh=true' : ''}`).then(async response => {
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<{ providers?: ChatProvider[] }>
  }).then(data => {
    const items = Array.isArray(data.providers) ? data.providers : []
    setProviders(items)
    setProvider(current => {
      const remembered = items.find(item => item.id === current && (item.status === 'ready' || (item.id === 'chatgpt_web' && item.configured)))
      if (remembered) return remembered.id
      const web = items.find(item => item.id === 'chatgpt_web' && item.configured)
      const fallback = items.find(item => item.id === DEFAULT_PROVIDER && item.status === 'ready') || items.find(item => item.status === 'ready')
      return web?.id || fallback?.id || current || ''
    })
    return items
  })
  const refreshProviderModels = async () => {
    if (!provider || modelsLoading) return
    setModelsLoading(true)
    setModelsError(false)
    try {
      const items = await refreshProviders(true)
      const next = items.find(item => item.id === provider)?.models || []
      modelCache.set(provider, next)
      setModels(next)
      setModel(current => next.some(item => item.id === current) ? current : (next[0]?.id || ''))
    } catch {
      setModels([])
      setModelsError(true)
    } finally {
      setModelsLoading(false)
    }
  }
  const refreshHealth = (accountId = account) => {
    if (!accountId || healthInFlight.current) return
    healthInFlight.current = true
    void fetch(`${API}/accounts/${accountId}/health`).then(response => response.ok ? response.json() : { status: 'unavailable', errorCode: 'CHATGPT_TOKEN_CHECK_FAILED' }).then(health => {
      setAccounts(items => items.map(item => item.id === accountId ? { ...item, ...health, error: health.error, errorCode: health.errorCode } : item))
      if (health.status === 'connected') { setError(''); setNotice(''); void refreshProviders() }
    }).catch(() => undefined).finally(() => { healthInFlight.current = false })
  }
  useEffect(() => {
    void Promise.all([
      refreshAccounts(),
      refreshProviders(),
      fetch(`${API}/conversations`).then(r => r.json()).then((items: Conversation[]) => { setConversations(items); if (items[0]) openConversation(items[0].id) }),
    ])
  }, [])
  useEffect(() => {
    localStorage.setItem(LAST_ACCOUNT, account)
    localStorage.setItem(LAST_PROVIDER, provider)
    if (!provider || !providerReady) {
      setModels([]); setModelsLoading(false); setModelsError(false); return
    }
    let activeRequest = true
    const remembered = provider === 'chatgpt_web' ? accounts.find(item => item.id === account)?.last_model : ''
    const applyModels = (available: ChatModel[]) => {
      if (!activeRequest) return
      setModels(available)
      setModel(current => available.some(item => item.id === current) ? current : (remembered && available.some(item => item.id === remembered) ? remembered : (provider === 'chatgpt_web' && available.some(item => item.id === DEFAULT_MODEL) ? DEFAULT_MODEL : (available[0]?.id || ''))))
    }
    const cached = modelCache.get(provider)
    if (cached) {
      setModelsLoading(false)
      setModelsError(false)
      applyModels(cached)
      return () => { activeRequest = false }
    }
    setModels([])
    setModelsLoading(true)
    setModelsError(false)
    fetchChatModels(provider).then(applyModels).catch(() => {
      if (!activeRequest) return
      setModels([])
      setModelsError(true)
    }).finally(() => { if (activeRequest) setModelsLoading(false) })
    return () => { activeRequest = false }
  }, [provider, providerReady, account, accountReady])
  useEffect(() => { localStorage.setItem(LAST_MODEL, model) }, [model])
  useEffect(() => { if (autoScroll.current) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }) }, [messages])

  const create = async () => {
    if (!providerReady) { setError(provider === 'chatgpt_web' ? (activeAccount ? accountError(activeAccount) || t('Hãy đăng nhập ChatGPT Codex trước.', 'Sign in to ChatGPT Codex first.') : t('Hãy đăng nhập ChatGPT Codex trước.', 'Sign in to ChatGPT Codex first.')) : t('Hãy chọn một provider có model khả dụng.', 'Choose a provider with an available model.')); return }
    const storedProvider = provider === 'chatgpt_web' ? account : provider
    const response = await fetch(`${API}/conversations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: t('Cuộc trò chuyện mới', 'New chat'), provider, accountId: storedProvider, model }) })
    const conv = await response.json(); await loadList(); openConversation(conv.id)
  }
  const remove = async (id: string) => {
    await fetch(`${API}/conversations/${id}`, { method: 'DELETE' })
    setActive(''); setMessages([]); await loadList()
  }
  const renameConversation = (conversation: Conversation) => {
    setRenameTarget(conversation)
  }
  const confirmRename = async (rawTitle: string) => {
    const conversation = renameTarget
    setRenameTarget(null)
    const title = rawTitle.trim()
    if (!conversation || !title || title === conversation.title) return
    try {
      const response = await fetch(`${API}/conversations/${conversation.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) })
      if (!response.ok) throw new Error(await response.text())
      await loadList()
      setNotice(t('Đã đổi tên cuộc trò chuyện.', 'Conversation renamed.'))
      window.setTimeout(() => setNotice(''), 3_000)
    } catch (e) { setError(errorText(e instanceof Error ? e.message : e)) }
  }
  const updateSettings = async (nextProvider: string, nextModel: string) => {
    setProvider(nextProvider); setModel(nextModel)
    const storedProvider = nextProvider === 'chatgpt_web' ? account : nextProvider
    if (active) await fetch(`${API}/conversations/${active}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: nextProvider, accountId: storedProvider, model: nextModel }) })
  }
  const downloadResponse = (message: Message) => {
    const blob = new Blob([message.content], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `chatgpt-response-${message.id.slice(0, 12)}.md`
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
    setNotice(t('Đã bắt đầu tải phản hồi.', 'Response download started.'))
    window.setTimeout(() => setNotice(''), 3_000)
  }
  const stopGeneration = async () => {
    if (!active || !busy || stopping) return
    setStopping(true)
    setNotice(t('Đang dừng tạo câu trả lời…', 'Stopping response…'))
    try {
      const response = await fetch(`${API}/conversations/${active}/cancel`, { method: 'POST' })
      if (!response.ok) throw new Error(await response.text())
    } catch (e) {
      setStopping(false)
      setNotice('')
      setError(errorText(e instanceof Error ? e.message : e))
    }
  }
  const consume = async (response: Response) => {
    if (!response.ok || !response.body) throw new Error((await response.text()) || `HTTP ${response.status}`)
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; let assistantId = ''; let terminal = false
    while (true) {
      const { done, value } = await reader.read(); if (done) break
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split('\n\n'); buffer = blocks.pop() || ''
      for (const block of blocks) {
        const event = block.match(/^event: (.+)$/m)?.[1]
        const raw = block.match(/^data: (.+)$/m)?.[1]; if (!raw) continue
        const data = JSON.parse(raw)
        if (event === 'message.started') { assistantId = data.messageId; setMessages(cur => [...cur, { id: assistantId, role: 'assistant', content: '', status: 'streaming' }]) }
        if (event === 'content.delta') setMessages(cur => cur.map(m => m.id === assistantId ? { ...m, content: m.content + data.delta } : m))
        if (event === 'artifact.completed') setMessages(cur => cur.map(m => m.id === assistantId ? { ...m, attachments: [...(m.attachments || []), data.artifact] } : m))
        if (event === 'message.completed') { terminal = true; setMessages(cur => cur.map(m => m.id === assistantId ? { ...m, content: data.content, status: data.status } : m)) }
        if (event === 'message.failed') {
          terminal = true
          if (String(data.error || '').includes('CHATGPT_LOGIN_REQUIRED')) {
            setAccounts(items => items.map(item => item.id === account ? { ...item, configured: false, status: 'reauth_required' } : item))
          }
          const messageError = errorText(data.error)
          setError(messageError)
          setMessages(cur => cur.map(m => m.id === assistantId ? { ...m, status: 'failed', error: messageError } : m))
        }
      }
    }
    if (assistantId && !terminal) {
      const messageError = errorText('CHAT_STREAM_INTERRUPTED')
      setMessages(cur => cur.map(m => m.id === assistantId ? { ...m, status: 'failed', error: messageError } : m))
      throw new Error('CHAT_STREAM_INTERRUPTED')
    }
  }
  const send = async () => {
    const content = input.trim(); if (!content || busy) return
    if (!providerReady) { setError(provider === 'chatgpt_web' ? (activeAccount ? accountError(activeAccount) || t('Hãy đăng nhập ChatGPT Codex trước khi gửi.', 'Sign in to ChatGPT Codex before sending a message.') : t('Hãy đăng nhập ChatGPT Codex trước khi gửi.', 'Sign in to ChatGPT Codex before sending a message.')) : t('Provider chưa sẵn sàng hoặc không có model khả dụng.', 'The provider is not ready or has no available model.')); return }
    const storedProvider = provider === 'chatgpt_web' ? account : provider
    let id = active
    if (!id) { const r = await fetch(`${API}/conversations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: content.slice(0, 60), provider, accountId: storedProvider, model }) }); id = (await r.json()).id; setActive(id) }
    setInput(''); setError(''); setNotice(''); setStopping(false); setBusy(true); setMessages(cur => [...cur, { id: `local-${Date.now()}`, role: 'user', content, status: 'completed' }])
    try {
      // Keep the value shown in the composer authoritative for older
      // conversations whose database row predates model selection.
      if (id && active) {
        const settings = await fetch(`${API}/conversations/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider, accountId: storedProvider, model }) })
        if (!settings.ok) throw new Error('CHAT_CONVERSATION_SETTINGS_FAILED')
      }
      await consume(await fetch(`${API}/conversations/${id}/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content, mode, provider, model, attachmentIds }) })); setAttachmentIds([]); setAttachmentNames([]); await loadList()
    }
    catch (e) {
      const detail = e instanceof Error ? e.message : String(e)
      setError(errorText(detail))
      if (detail.includes('CHATGPT_LOGIN_REQUIRED')) {
        await Promise.all([refreshAccounts(), refreshProviders()])
      }
    } finally { setStopping(false); setBusy(false) }
  }
  const retry = async (messageId: string) => {
    if (!active || busy) return
    if (!providerReady) { setError(provider === 'chatgpt_web' ? (activeAccount ? accountError(activeAccount) || t('Hãy đăng nhập ChatGPT Codex trước khi thử lại.', 'Sign in to ChatGPT Codex before retrying.') : t('Hãy đăng nhập ChatGPT Codex trước khi thử lại.', 'Sign in to ChatGPT Codex before retrying.')) : t('Provider chưa sẵn sàng hoặc không có model khả dụng.', 'The provider is not ready or has no available model.')); return }
    const storedProvider = provider === 'chatgpt_web' ? account : provider
    setError(''); setNotice(''); setStopping(false); setBusy(true)
    try {
      const settings = await fetch(`${API}/conversations/${active}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider, accountId: storedProvider, model }) })
      if (!settings.ok) throw new Error('CHAT_CONVERSATION_SETTINGS_FAILED')
      await consume(await fetch(`${API}/conversations/${active}/messages/${messageId}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode, provider, model }) }))
    }
    catch (e) {
      const detail = e instanceof Error ? e.message : String(e)
      setError(errorText(detail))
      if (detail.includes('CHATGPT_LOGIN_REQUIRED')) {
        await Promise.all([refreshAccounts(), refreshProviders()])
      }
    } finally { setStopping(false); setBusy(false) }
  }
  const uploadFiles = async (files: File[]) => {
    if (!files.length) return
    if (!active) { setError(t('Hãy tạo chat trước khi đính kèm.', 'Create a chat before attaching files.')); return }
    for (const file of files.slice(0, Math.max(0, 10 - attachmentIds.length))) {
      const body = new FormData(); body.append('file', file)
      const response = await fetch(`${API}/conversations/${active}/attachments`, { method: 'POST', body })
      if (!response.ok) { setError(errorText(await response.text())); return }
      const saved = await response.json(); setAttachmentIds(value => [...value, saved.id]); setAttachmentNames(value => [...value, file.name])
    }
  }
  const signIn = async (accountId = account) => {
    setError(''); setNotice('')
    try {
      const response = await fetch(`${API}/accounts/${accountId}/login`, { method: 'POST' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail?.message || data.detail || t('Không mở được đăng nhập.', 'Could not open sign-in.'))
      if (!data.loginId) throw new Error(t('Không tạo được phiên đăng nhập ChatGPT Codex.', 'Could not create a ChatGPT Codex sign-in session.'))
      await refreshAccounts()
      setNotice(t('Đã mở Chrome để đăng nhập ChatGPT Codex. Hoàn tất trong cửa sổ đó để tiếp tục.', 'Chrome is open for ChatGPT Codex sign-in. Finish it in that window to continue.'))
      for (let attempt = 0; attempt < 300; attempt += 1) {
        await new Promise(resolve => window.setTimeout(resolve, 2000))
        const poll = await fetch(`${API}/accounts/${accountId}/login/${data.loginId}/poll`, { method: 'POST' })
        const result = await poll.json()
        if (!poll.ok) throw new Error(result.detail?.message || result.detail || t('Đăng nhập ChatGPT Codex thất bại.', 'ChatGPT Codex sign-in failed.'))
        if (result.status === 'connected') {
          await Promise.all([refreshAccounts(), refreshProviders(true)])
          setNotice(t('Đăng nhập ChatGPT Codex thành công.', 'ChatGPT Codex sign-in completed.'))
          return
        }
        if (result.status === 'expired' || result.status === 'failed') {
          throw new Error(result.error || t('Đăng nhập ChatGPT Codex thất bại.', 'ChatGPT Codex sign-in failed.'))
        }
      }
      throw new Error(t('Đăng nhập ChatGPT Codex hết thời gian chờ.', 'ChatGPT Codex sign-in timed out.'))
    } catch (e) {
      setNotice('')
      setError(errorText(e instanceof Error ? e.message : e))
      void refreshAccounts().catch(() => undefined)
    }
  }
  const signOut = async (accountId = account) => {
    const response = await fetch(`${API}/accounts/${accountId}/logout`, { method: 'POST' })
    modelCache.delete('chatgpt_web')
    if (!response.ok) setError(t('Không đăng xuất được ChatGPT Codex.', 'Could not sign out from ChatGPT Codex.'))
    await Promise.all([refreshAccounts(), refreshProviders()])
  }
  const addAccount = async () => {
    setError('')
    const current = accounts[0]
    if (current) { setAccount(current.id); setProvider('chatgpt_web'); await signIn(current.id); return }
    const response = await fetch(`${API}/accounts`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: 'chatgpt_account', label: t('Tài khoản ChatGPT Codex', 'ChatGPT Codex account') }) })
    const data = await response.json()
    if (!response.ok) { setError(data.detail?.message || data.detail || t('Không thêm được tài khoản.', 'Could not add account.')); return }
    await refreshAccounts(); setAccount(data.id); setProvider('chatgpt_web'); await signIn(data.id)
  }
  const modelSelection = `${provider}::${model}`
  const providerName = (id: string) => ({
    chatgpt_web: t('ChatGPT Codex', 'ChatGPT Codex'), openai: t('OpenAI API', 'OpenAI API'), gemini: 'Gemini',
    deepseek: 'DeepSeek', openrouter: 'OpenRouter', grok: 'Grok (xAI)', groq: 'Groq', nvidia: t('NVIDIA NIM', 'NVIDIA NIM'),
  } as Record<string, string>)[id] || id
  const selectModel = (value: string) => {
    const separator = value.indexOf('::')
    if (separator < 0) return
    void updateSettings(value.slice(0, separator), value.slice(separator + 2))
  }
  const selectableProviders = providers.filter(item => (item.id === 'chatgpt_web' ? item.configured : item.status === 'ready'))
  const availableModelsCount = providers.reduce((total, item) => total + item.models.filter(option => option.available && option.free).length, 0)
  const availableProviderCount = providers.filter(item => item.models.some(option => option.available && option.free)).length
  const currentProviderModelCount = models.filter(option => option.available && option.free).length

  return <main className="chat-page">
    <aside className={`chat-sidebar${railOpen ? ' open' : ''}`}><button type="button" className="chat-rail-close" onClick={() => setRailOpen(false)} aria-label={t('Đóng lịch sử', 'Close history')}>×</button><button type="button" className="chat-new" onClick={create}>＋ {t('Chat mới', 'New chat')}</button>
      <div className="chat-history">{conversations.map(c => <div className={`chat-history-item${active === c.id ? ' active' : ''}`} key={c.id}><button type="button" onClick={() => openConversation(c.id)}>{c.title}</button><button type="button" className="chat-history-action" title={t('Đổi tên cuộc trò chuyện', 'Rename conversation')} aria-label={t('Đổi tên cuộc trò chuyện', 'Rename conversation')} onClick={() => void renameConversation(c)}>✎</button><button type="button" className="chat-history-action" aria-label={t('Xóa cuộc trò chuyện', 'Delete conversation')} onClick={() => void remove(c.id)}>×</button></div>)}</div>
      <section className="chat-account-dock" aria-label={t('Cài đặt tài khoản ChatGPT Codex', 'ChatGPT Codex account settings')}>
        <div className="chat-account-dock-head"><span className="chat-account-avatar">C</span><div><strong>{activeAccount ? accountName(activeAccount) : t('ChatGPT Codex', 'ChatGPT Codex')}</strong><small>{activeAccount ? accountState(activeAccount) : t('Chưa đăng nhập', 'Not signed in')}</small></div><button type="button" className="chat-account-check" onClick={() => refreshHealth()} disabled={!activeAccount || activeAccount.status === 'connecting'} aria-label={t('Kiểm tra phiên', 'Check session')}>↻</button></div>
        {activeAccount?.email ? <small className="chat-account-email">{activeAccount.email}</small> : null}
        {activeAccount && accountError(activeAccount) ? <p className="chat-account-error">{accountError(activeAccount)}</p> : null}
        <div className="chat-account-actions">{!activeAccount ? <button type="button" onClick={() => void addAccount()}>{t('Đăng nhập ChatGPT Codex', 'Sign in to ChatGPT Codex')}</button> : activeAccount.configured ? <button type="button" onClick={() => void signOut(activeAccount.id)}>{t('Đăng xuất', 'Sign out')}</button> : <button type="button" disabled={activeAccount.status === 'connecting'} onClick={() => void signIn(activeAccount.id)}>{activeAccount.status === 'connecting' ? t('Đang đăng nhập…', 'Signing in…') : t('Đăng nhập lại', 'Sign in again')}</button>}</div>
      </section>
    </aside>
    <section className="chat-main">
      <header className="chat-toolbar"><button type="button" className="chat-rail-open" onClick={() => setRailOpen(true)} aria-label={t('Mở lịch sử', 'Open history')}>☰</button></header>
      <div className="chat-modes" role="toolbar" aria-label={t('Chế độ AI', 'AI mode')}>{([['chat','Chat'],['search',t('Tìm kiếm web','Web search')],['research',t('Nghiên cứu sâu','Deep research')],['image',t('Tạo ảnh','Create image')]] as [ChatMode,string][]).map(([id,label]) => { const enabled = id === 'chat'; return <button type="button" key={id} className={mode === id ? 'active' : ''} aria-pressed={mode === id} disabled={!enabled} title={!enabled ? t('Chế độ này chưa khả dụng qua ChatGPT Codex.', 'This mode is not available through ChatGPT Codex.') : undefined} onClick={() => setMode(id)}>{label}</button> })}</div>
      <div className="chat-messages" ref={scrollRef} onScroll={e => { const el = e.currentTarget; autoScroll.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }}>
        {!messages.length && <div className="chat-empty"><h1>{t('Tôi có thể giúp gì?', 'How can I help?')}</h1><p>{t('Chat nhiều lượt với provider bạn đã chọn; model khả dụng được lọc tự động.', 'Multi-turn chat with your selected provider; available models are filtered automatically.')}</p></div>}
        {messages.map(m => <article key={m.id} className={`chat-message ${m.role}`}><div className="chat-avatar">{m.role === 'user' ? t('Bạn', 'You') : 'AI'}</div><div>
          {m.status === 'streaming' && !m.content
            ? <div className="chat-generating" role="status" aria-live="polite"><span>{t('Đang trả lời…', 'Answering…')}</span><span className="chat-generating-dots" aria-hidden="true"><i /><i /><i /></span></div>
            : m.status === 'failed' && !m.content && m.error
              ? <p className="chat-message-error" role="alert">{m.error}</p>
              : <MessageBody content={m.content} />}
          {m.attachments?.length ? <div className="chat-artifacts">{m.attachments.map(item => item.content_type.startsWith('image/') ? <a key={item.id} href={item.url} target="_blank" rel="noopener noreferrer"><img src={item.url} alt={item.name}/><span>{item.name}</span></a> : <a key={item.id} href={item.url} download={item.name}>{item.name}</a>)}</div> : null}
          {m.role === 'assistant' && m.status === 'completed' && m.content ? <div className="chat-message-actions"><button type="button" onClick={() => void copyText(m.content, t('Đã sao chép phản hồi.', 'Response copied.'))} aria-label={t('Sao chép phản hồi', 'Copy response')}>{t('Sao chép phản hồi', 'Copy response')}</button><button type="button" onClick={() => downloadResponse(m)} aria-label={t('Tải phản hồi', 'Download response')}>{t('Tải phản hồi', 'Download response')}</button></div> : null}
          {m.role === 'assistant' && m.status === 'interrupted' ? <p className="chat-interrupted" role="status">{t('Đã dừng tạo câu trả lời.', 'Generation stopped.')}</p> : null}
          {(m.status === 'failed' || m.status === 'interrupted') && messages[messages.length - 1]?.id === m.id && <button type="button" className="chat-retry" onClick={() => void retry(m.id)}>{t('Thử lại', 'Retry')}</button>}
        </div></article>)}
        {busy && !messages.some(m => m.status === 'streaming') ? <article className="chat-message assistant chat-message-pending"><div className="chat-avatar">AI</div><div className="chat-generating" role="status" aria-live="polite"><span>{t('Đang trả lời…', 'Answering…')}</span><span className="chat-generating-dots" aria-hidden="true"><i /><i /><i /></span></div></article> : null}
      </div>{notice && <div className="chat-notice" role="status">{notice}</div>}{error && !(messages[messages.length-1]?.status === 'failed' && !messages[messages.length-1]?.content) && <div className="chat-error" role="alert">{error}</div>}
      {attachmentNames.length ? <div className="chat-attachment-tray">{attachmentNames.map((name, index) => <span key={`${name}-${index}`}>{name}<button type="button" aria-label={t(`Bỏ ${name}`, `Remove ${name}`)} onClick={() => { setAttachmentNames(v => v.filter((_, i) => i !== index)); setAttachmentIds(v => v.filter((_, i) => i !== index)) }}>×</button></span>)}</div> : null}
      <div className="chat-composer" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); void uploadFiles(Array.from(e.dataTransfer.files)) }}><textarea value={input} onChange={e => setInput(e.target.value)} onPaste={e => { const files = Array.from(e.clipboardData.files); if (files.length) { e.preventDefault(); void uploadFiles(files) } }} placeholder={t('Nhắn tin cho AI…', 'Message AI…')} aria-label={t('Nội dung tin nhắn', 'Message content')} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }}/>
        <div className="chat-composer-bar">
          <label className="chat-attach" title={t('Đính kèm file', 'Attach file')}>＋<input type="file" multiple accept="image/png,image/jpeg,image/webp,image/gif,application/pdf,text/plain,text/markdown,text/vtt,application/x-subrip,audio/wav,audio/x-wav,audio/mpeg,audio/mp4,audio/aac,audio/flac,audio/ogg" onChange={e => void uploadFiles(Array.from(e.target.files || []))}/></label>
          <label className="chat-model-picker chat-model-picker-bottom"><span className="sr-only">{t('Provider và model', 'Provider and model')}</span><select value={modelSelection} onChange={e => selectModel(e.target.value)} aria-label={t('Chọn provider và model', 'Choose provider and model')} aria-busy={modelsLoading} disabled={!selectableProviders.length || modelsLoading} title={modelsError ? t('Không tải được danh sách model.', 'Could not load models.') : undefined}>
            {!selectableProviders.length && <option value="">{t('Chưa có provider/model khả dụng', 'No available provider/model')}</option>}
            {selectableProviders.map(item => <optgroup key={item.id} label={`${providerName(item.id)} · ${item.models.length}`}>
              {item.models.map(option => <option key={`${item.id}:${option.id}`} value={`${item.id}::${option.id}`}>{option.label || option.id}</option>)}
            </optgroup>)}
          </select></label>
          <button type="button" className="chat-model-refresh" onClick={() => void refreshProviderModels()} disabled={!provider || modelsLoading} aria-label={t('Làm mới danh sách model', 'Refresh model list')} title={t('Làm mới danh sách model', 'Refresh model list')}>↻</button>
          {modelsLoading ? <span className="chat-model-status" role="status">{t('Đang tải model…', 'Loading models…')}</span> : availableModelsCount ? <span className="chat-model-status" role="status" title={t(`${currentProviderModelCount} model từ provider đang chọn`, `${currentProviderModelCount} models from the selected provider`)}>{availableModelsCount} {t('model khả dụng', 'available model(s)')} · {availableProviderCount} {t('cloud', 'cloud providers')}{modelsError ? ` · ${t('provider đang chọn lỗi', 'selected provider unavailable')}` : ''}</span> : modelsError ? <span className="chat-model-status" role="status">{t('Không tải được model', 'Could not load models')}</span> : null}
          <span className="chat-composer-spacer" />
          {busy ? <button type="button" onClick={() => void stopGeneration()} disabled={stopping} aria-label={stopping ? t('Đang dừng tạo', 'Stopping generation') : t('Dừng tạo', 'Stop generating')}>{stopping ? '…' : '■'}</button> : <button type="button" onClick={() => void send()} disabled={!input.trim()} aria-label={t('Gửi tin nhắn', 'Send message')}>↑</button>}
        </div>
      </div><p className="chat-note">{t('AI có thể mắc lỗi. Hãy kiểm tra thông tin quan trọng.', 'AI can make mistakes. Check important information.')}</p>
    </section>
    <PromptDialog open={Boolean(renameTarget)} title={t('Đổi tên cuộc trò chuyện', 'Rename conversation')} message={t('Nhập tên mới cho cuộc trò chuyện.', 'Enter a new name for this conversation.')} initialValue={renameTarget?.title || ''} placeholder={t('Tên cuộc trò chuyện', 'Conversation name')} cancelLabel={t('Quay lại', 'Go back')} confirmLabel={t('Lưu', 'Save')} onCancel={() => setRenameTarget(null)} onConfirm={(value) => void confirmRename(value)} />
  </main>
}
