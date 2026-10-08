import { useCallback, useEffect, useState } from 'react'
import { AI_REVIEW_TRANSLATORS } from '@/app/appSettings'
import { localize, useLocale } from '@/app/i18n'
import { api } from './project.api'

export function useTranslationProviders() {
  const { locale } = useLocale()
  const [configuredProviders, setConfiguredProviders] = useState<string[]>([])
  useEffect(() => {
    let active = true
    let request = 0
    const refresh = async () => {
      const current = ++request
      try {
        const config = await api.getConfig()
        if (active && current === request) {
          setConfiguredProviders(Object.entries(config.cloud || {})
            .filter(([, value]) => value.apiKeySet).map(([id]) => id))
        }
      } catch {
        if (active && current === request) setConfiguredProviders([])
      }
    }
    void refresh()
    window.addEventListener('focus', refresh)
    window.addEventListener('zm-cloud-config-changed', refresh)
    return () => {
      active = false
      window.removeEventListener('focus', refresh)
      window.removeEventListener('zm-cloud-config-changed', refresh)
    }
  }, [])
  const isAvailable = useCallback((id: string) => id === 'ollama'
    || !AI_REVIEW_TRANSLATORS.some(provider => provider === id)
    || configuredProviders.includes(id), [configuredProviders])
  const providerLabel = useCallback((id: string, label: string) => isAvailable(id) ? label
    : `${label} — ${localize(locale, 'Chưa nhập API key', 'API key not configured')}`, [isAvailable, locale])
  return { configuredProviders, isAvailable, providerLabel }
}
