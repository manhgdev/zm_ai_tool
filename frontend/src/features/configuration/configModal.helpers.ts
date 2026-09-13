import type { CloudProviderId, SystemChecks } from '@/features/project/project.types'

export const PROVIDERS: CloudProviderId[] = ['openai', 'gemini', 'deepseek', 'openrouter', 'grok', 'groq', 'nvidia']

export type InstallKind = 'ai_runtime' | 'ai_runtime_ocr' | 'ai_runtime_vieneu' | 'ocr_cuda' | 'demucs_cuda' | 'nvm'

export const INSTALL_LABELS: Record<InstallKind, string> = {
  ai_runtime: 'gói AI', ai_runtime_ocr: 'gói AI', ai_runtime_vieneu: 'gói AI',
  ocr_cuda: 'OCR CUDA', demucs_cuda: 'Demucs', nvm: 'NVM + Node.js LTS',
}

export const INSTALL_ORDER: InstallKind[] = ['ai_runtime', 'ai_runtime_ocr', 'ai_runtime_vieneu', 'ocr_cuda', 'demucs_cuda']

export function installLabel(kind: string): string {
  return INSTALL_LABELS[kind as InstallKind] || kind
}

export function nextAutoInstall(checks: SystemChecks): InstallKind | null {
  for (const id of INSTALL_ORDER) {
    const it = checks.items.find((i) => !i.ok && i.install === id)
    if (it?.required) return id
  }
  return null
}

export type Section = 'setup' | 'cloud' | 'tts' | 'output' | 'license' | 'logs'
export type CloudTab = CloudProviderId
export type UpdateDialog = {
  kind: 'available' | 'info' | 'downloading' | 'ready' | 'applying' | 'error' | 'complete'
  title: string; detail: string; progress?: number
}

export type CloudDraft = Record<
  CloudProviderId,
  { apiKey: string; apiKeys?: string; keyCount?: number; baseUrl: string; model: string; apiKeySet: boolean; label: string }
>

export function emptyCloud(): CloudDraft {
  return {
    openai:     { apiKey: '', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o-mini', apiKeySet: false, label: 'OpenAI' },
    gemini:     { apiKey: '', baseUrl: 'https://generativelanguage.googleapis.com/v1beta', model: 'gemini-3.1-flash-lite', apiKeySet: false, label: 'Gemini' },
    deepseek:   { apiKey: '', baseUrl: 'https://api.deepseek.com', model: 'deepseek-chat', apiKeySet: false, label: 'DeepSeek' },
    openrouter: { apiKey: '', baseUrl: 'https://openrouter.ai/api/v1', model: 'google/gemini-2.5-flash', apiKeySet: false, label: 'OpenRouter' },
    grok:       { apiKey: '', baseUrl: 'https://api.x.ai/v1', model: 'grok-3-mini', apiKeySet: false, label: 'Grok' },
    groq:       { apiKey: '', baseUrl: 'https://api.groq.com/openai/v1', model: 'openai/gpt-oss-20b', apiKeySet: false, label: 'Groq' },
    nvidia:     { apiKey: '', baseUrl: 'https://integrate.api.nvidia.com/v1', model: 'nvidia/riva-translate-4b-instruct-v2', apiKeySet: false, label: 'NVIDIA NIM' },
  }
}

export function providerKeyPlaceholder(provider: CloudProviderId): string {
  switch (provider) {
    case 'gemini':
      return 'AQ.…'
    case 'groq':
      return 'gsk_…'
    case 'nvidia':
      return 'nvapi-…'
    case 'openrouter':
      return 'sk-or-…'
    case 'grok':
      return 'xai-…'
    default:
      return 'sk-…'
  }
}

export function savedKeyPlaceholder(config: CloudDraft[CloudProviderId], index: number): string {
  const masked = (config.apiKeys || '').split(',')[index]?.trim()
  return masked || (index < (config.keyCount || 0) ? '••••••••' : 'sk-…')
}

export interface ModelPreset {
  id: string
  labelVi: string
  labelEn: string
}

export const PROVIDER_PRESET_MODELS: Record<CloudProviderId, ModelPreset[]> = {
  openai: [
    { id: 'gpt-4o-mini', labelVi: 'gpt-4o-mini (Khuyên dùng — nhanh & rẻ)', labelEn: 'gpt-4o-mini (Recommended — fast & cheap)' },
    { id: 'gpt-4o', labelVi: 'gpt-4o (Chất lượng cao)', labelEn: 'gpt-4o (High quality)' },
    { id: 'gpt-4.1-mini', labelVi: 'gpt-4.1-mini (Mới)', labelEn: 'gpt-4.1-mini (New)' },
    { id: 'gpt-4.1', labelVi: 'gpt-4.1 (Mới — mạnh mẽ)', labelEn: 'gpt-4.1 (New — powerful)' },
    { id: 'o3-mini', labelVi: 'o3-mini (Lý luận)', labelEn: 'o3-mini (Reasoning)' },
  ],
  gemini: [
    { id: 'gemini-3.1-flash-lite', labelVi: 'gemini-3.1-flash-lite (Mặc định — siêu nhanh)', labelEn: 'gemini-3.1-flash-lite (Default — ultra fast)' },
    { id: 'gemini-2.5-flash', labelVi: 'gemini-2.5-flash (Khuyên dùng — dịch chuẩn)', labelEn: 'gemini-2.5-flash (Recommended — accurate)' },
    { id: 'gemini-2.5-pro', labelVi: 'gemini-2.5-pro (Chất lượng cao nhất)', labelEn: 'gemini-2.5-pro (Highest quality)' },
    { id: 'gemini-2.0-flash', labelVi: 'gemini-2.0-flash (Thế hệ 2.0)', labelEn: 'gemini-2.0-flash (Gen 2.0)' },
    { id: 'gemini-1.5-flash', labelVi: 'gemini-1.5-flash (Ổn định)', labelEn: 'gemini-1.5-flash (Stable)' },
  ],
  deepseek: [
    { id: 'deepseek-chat', labelVi: 'deepseek-chat (V3 — Khuyên dùng dịch thuật)', labelEn: 'deepseek-chat (V3 — Recommended for translation)' },
    { id: 'deepseek-reasoner', labelVi: 'deepseek-reasoner (R1 — Mô hình suy luận)', labelEn: 'deepseek-reasoner (R1 — Reasoning model)' },
  ],
  openrouter: [
    { id: 'google/gemini-3.1-flash-lite', labelVi: 'google/gemini-3.1-flash-lite (Siêu nhanh & rẻ)', labelEn: 'google/gemini-3.1-flash-lite (Ultra fast & cheap)' },
    { id: 'google/gemini-2.5-flash', labelVi: 'google/gemini-2.5-flash (Khuyên dùng — nhanh & rẻ)', labelEn: 'google/gemini-2.5-flash (Recommended — fast & cheap)' },
    { id: 'deepseek/deepseek-chat', labelVi: 'deepseek/deepseek-chat (DeepSeek V3)', labelEn: 'deepseek/deepseek-chat (DeepSeek V3)' },
    { id: 'qwen/qwen-2.5-72b-instruct', labelVi: 'qwen/qwen-2.5-72b-instruct (Dịch tốt)', labelEn: 'qwen/qwen-2.5-72b-instruct (Good for translation)' },
    { id: 'anthropic/claude-3.5-sonnet', labelVi: 'anthropic/claude-3.5-sonnet (Chất lượng cao)', labelEn: 'anthropic/claude-3.5-sonnet (High quality)' },
    { id: 'meta-llama/llama-3.3-70b-instruct', labelVi: 'meta-llama/llama-3.3-70b-instruct (Mã nguồn mở)', labelEn: 'meta-llama/llama-3.3-70b-instruct (Open source)' },
    { id: 'openai/gpt-4o-mini', labelVi: 'openai/gpt-4o-mini (Nhanh & tiết kiệm)', labelEn: 'openai/gpt-4o-mini (Fast & cost-effective)' },
  ],
  grok: [
    { id: 'grok-3-mini', labelVi: 'grok-3-mini (Mặc định — nhanh & rẻ)', labelEn: 'grok-3-mini (Default — fast & cheap)' },
    { id: 'grok-3', labelVi: 'grok-3 (Mạnh nhất)', labelEn: 'grok-3 (Most powerful)' },
    { id: 'grok-2', labelVi: 'grok-2 (Ổn định)', labelEn: 'grok-2 (Stable)' },
  ],
  groq: [
    { id: 'openai/gpt-oss-20b', labelVi: 'openai/gpt-oss-20b (Mặc định — siêu nhanh)', labelEn: 'openai/gpt-oss-20b (Default — ultra fast)' },
    { id: 'llama-3.3-70b-versatile', labelVi: 'llama-3.3-70b-versatile (Chất lượng cao)', labelEn: 'llama-3.3-70b-versatile (High quality)' },
    { id: 'llama-3.1-8b-instant', labelVi: 'llama-3.1-8b-instant (Siêu tốc)', labelEn: 'llama-3.1-8b-instant (Instant speed)' },
    { id: 'mixtral-8x7b-32768', labelVi: 'mixtral-8x7b-32768 (Context dài)', labelEn: 'mixtral-8x7b-32768 (Long context)' },
  ],
  nvidia: [
    { id: 'nvidia/riva-translate-4b-instruct-v2', labelVi: 'nvidia/riva-translate-4b-instruct-v2 (Mặc định — chuyên dịch)', labelEn: 'nvidia/riva-translate-4b-instruct-v2 (Default — translation specialized)' },
    { id: 'meta/llama-3.3-70b-instruct', labelVi: 'meta/llama-3.3-70b-instruct (Mạnh mẽ)', labelEn: 'meta/llama-3.3-70b-instruct (Powerful)' },
    { id: 'mistralai/mixtral-8x7b-instruct-v0.1', labelVi: 'mistralai/mixtral-8x7b-instruct-v0.1 (Mixtral)', labelEn: 'mistralai/mixtral-8x7b-instruct-v0.1 (Mixtral)' },
  ],
}
