export type ChatModelOption = { id: string; label: string; provider: string; free: boolean; capabilities?: string[]; available?: boolean }
export type ChatProviderOption = { id: string; label: string; kind: 'api' | 'browser'; configured: boolean; status: string; models: ChatModelOption[] }

export function normalizeChatProviders(raw: unknown): ChatProviderOption[] {
  const values = raw && typeof raw === 'object' && Array.isArray((raw as { providers?: unknown }).providers)
    ? (raw as { providers: unknown[] }).providers : []
  return values.reduce<ChatProviderOption[]>((result, item) => {
    if (!item || typeof item !== 'object') return result
    const row = item as Record<string, unknown>
    const id = String(row.id || '').trim()
    if (!id || result.some(current => current.id === id)) return result
    const models = Array.isArray(row.models) ? row.models.reduce<ChatModelOption[]>((items, value) => {
      if (!value || typeof value !== 'object') return items
      const model = value as Record<string, unknown>
      const modelId = String(model.id || '').trim()
      if (!modelId || items.some(current => current.id === modelId)) return items
      items.push({ id: modelId, label: String(model.label || modelId), provider: String(model.provider || id), free: model.free !== false, capabilities: Array.isArray(model.capabilities) ? model.capabilities.map(String) : ['text'], available: model.available !== false })
      return items
    }, []) : []
    result.push({ id, label: String(row.label || id), kind: row.kind === 'browser' ? 'browser' : 'api', configured: row.configured !== false, status: String(row.status || ''), models })
    return result
  }, [])
}

/** A provider can run a text prompt now (ChatGPT Codex reports `connected`, not `ready`). */
export function chatProviderUsable(item: ChatProviderOption): boolean {
  return item.status === 'ready' || (item.id === 'chatgpt_web' && item.configured)
}
