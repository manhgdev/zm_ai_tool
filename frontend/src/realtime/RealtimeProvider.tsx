import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { RealtimeEvent, RealtimeHandler, RealtimeStatus } from './types'
import { currentWebAccountId } from '@/app/WebSession'

type RealtimeContextValue = {
  status: RealtimeStatus
  subscribe: (topic: string, handler: RealtimeHandler) => () => void
}

const RealtimeContext = createContext<RealtimeContextValue | null>(null)
const API = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

export function RealtimeProvider({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) {
  const [status, setStatus] = useState<RealtimeStatus>('reconnecting')
  const sourceRef = useRef<EventSource | null>(null)
  const retryRef = useRef<number | null>(null)
  const topicsRef = useRef(new Set<string>())
  const handlersRef = useRef(new Map<string, Set<RealtimeHandler>>())
  const lastIdRef = useRef('')
  const connectRef = useRef<() => void>(() => undefined)
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled

  const dispatch = useCallback((event: RealtimeEvent) => {
    if (event.id && (!lastIdRef.current || Number(event.id) > Number(lastIdRef.current))) {
      lastIdRef.current = event.id
    }
    if (event.type === 'snapshot.required') {
      // The server dropped intermediate events because the client queue was
      // full. Reconnect to receive an authoritative snapshot immediately.
      window.setTimeout(() => connectRef.current(), 0)
      return
    }
    // Every subscriber consumes snapshots directly; replaying rows duplicates logs.
    handlersRef.current.get(event.topic)?.forEach((handler) => handler(event))
  }, [])

  const connect = useCallback(() => {
    if (!enabledRef.current) return
    sourceRef.current?.close()
    const topics = [...topicsRef.current]
    const params = new URLSearchParams()
    const accountId = currentWebAccountId()
    if (accountId) params.set('web_account', accountId)
    if (topics.length) params.set('topics', topics.join(','))
    if (lastIdRef.current) params.set('after', lastIdRef.current)
    const query = params.toString() ? `?${params.toString()}` : ''
    const source = new EventSource(`${API}/api/events${query}`)
    sourceRef.current = source
    source.onopen = () => setStatus('connected')
    source.onerror = () => {
      source.close()
      setStatus('reconnecting')
      if (retryRef.current === null) {
        retryRef.current = window.setTimeout(() => {
          retryRef.current = null
          connectRef.current()
        }, 3000)
      }
    }
    const latestIds = new Map<string, number>()
    const receive = (event: RealtimeEvent) => {
      if (sourceRef.current !== source) return
      const id = Number(event.id)
      if (event.type !== 'snapshot.required' && Number.isFinite(id)) {
        if (id <= (latestIds.get(event.topic) ?? -1)) return
        latestIds.set(event.topic, id)
      }
      dispatch(event)
    }
    source.onmessage = (message) => {
      try { receive(JSON.parse(message.data) as RealtimeEvent) } catch { /* ignore malformed event */ }
    }
    const listen = (eventType: string) => {
      source.addEventListener(eventType, (message) => {
        const data = (message as MessageEvent).data
        try { receive(JSON.parse(data) as RealtimeEvent) } catch { /* ignore malformed event */ }
      })
    }
    ;['connected', 'snapshot', 'snapshot.required', 'tts.job.updated', 'tts.engine.status', 'flow.job.updated', 'flow.job.created', 'flow.job.deleted', 'flow.account.updated', 'flow.account.created', 'flow.account.deleted', 'flow.log.appended', 'flow.log.deleted', 'drawing.job.created', 'drawing.job.updated', 'drawing.job.deleted', 'automation.job.created', 'automation.job.updated', 'automation.job.deleted', 'srt-export.job.created', 'srt-export.job.updated', 'srt-export.job.deleted', 'cleaner.job.created', 'cleaner.job.updated', 'cleaner.job.deleted', 'srt-image.job.created', 'srt-image.job.updated', 'srt-image.job.deleted', 'download.job.created', 'download.job.updated', 'download.job.deleted', 'series.created', 'series.updated', 'series.deleted'].forEach(listen)
  }, [dispatch])
  connectRef.current = connect

  const subscribe = useCallback((topic: string, handler: RealtimeHandler) => {
    const handlers = handlersRef.current.get(topic) || new Set<RealtimeHandler>()
    handlers.add(handler)
    handlersRef.current.set(topic, handlers)
    const hadTopic = topicsRef.current.has(topic)
    topicsRef.current.add(topic)
    if (enabledRef.current && (!sourceRef.current || sourceRef.current.readyState === EventSource.CLOSED || !hadTopic)) {
      connectRef.current()
    }
    return () => {
      handlers.delete(handler)
      if (!handlers.size) {
        handlersRef.current.delete(topic)
        topicsRef.current.delete(topic)
      }
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      sourceRef.current?.close()
      sourceRef.current = null
      if (retryRef.current !== null) window.clearTimeout(retryRef.current)
      retryRef.current = null
      setStatus('fallback')
      return
    }
    connect()
    return () => {
      if (retryRef.current !== null) window.clearTimeout(retryRef.current)
      sourceRef.current?.close()
    }
  }, [connect, enabled])

  const value = useMemo(() => ({ status, subscribe }), [status, subscribe])
  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>
}

export function useRealtimeEvents(topic: string, handler: RealtimeHandler) {
  const context = useContext(RealtimeContext)
  const subscribe = context?.subscribe
  useEffect(() => subscribe?.(topic, handler), [subscribe, topic, handler])
  return context?.status || 'fallback'
}

export function useRealtimeStatus() {
  return useContext(RealtimeContext)?.status || 'fallback'
}
