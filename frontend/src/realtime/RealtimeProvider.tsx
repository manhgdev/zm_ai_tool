import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { RealtimeEvent, RealtimeHandler, RealtimeStatus } from './types'

type RealtimeContextValue = {
  status: RealtimeStatus
  subscribe: (topic: string, handler: RealtimeHandler) => () => void
}

const RealtimeContext = createContext<RealtimeContextValue | null>(null)
const API = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

export function RealtimeProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<RealtimeStatus>('reconnecting')
  const sourceRef = useRef<EventSource | null>(null)
  const retryRef = useRef<number | null>(null)
  const topicsRef = useRef(new Set<string>())
  const handlersRef = useRef(new Map<string, Set<RealtimeHandler>>())
  const lastIdRef = useRef('')
  const connectRef = useRef<() => void>(() => undefined)

  const dispatch = useCallback((event: RealtimeEvent) => {
    if (event.id) lastIdRef.current = event.id
    handlersRef.current.get(event.topic)?.forEach((handler) => handler(event))
  }, [])

  const connect = useCallback(() => {
    sourceRef.current?.close()
    const topics = [...topicsRef.current]
    const params = new URLSearchParams()
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
    source.onmessage = (message) => {
      try { dispatch(JSON.parse(message.data) as RealtimeEvent) } catch { /* ignore malformed event */ }
    }
    ;['connected', 'snapshot', 'snapshot.required', 'flow.job.updated', 'flow.job.created', 'flow.job.deleted', 'flow.account.updated', 'flow.account.created', 'flow.account.deleted', 'flow.log.appended', 'flow.log.deleted', 'drawing.job.created', 'drawing.job.updated', 'drawing.job.deleted', 'automation.job.created', 'automation.job.updated', 'automation.job.deleted', 'srt-export.job.created', 'srt-export.job.updated', 'srt-export.job.deleted', 'cleaner.job.created', 'cleaner.job.updated', 'cleaner.job.deleted', 'srt-image.job.created', 'srt-image.job.updated', 'srt-image.job.deleted', 'download.job.created', 'download.job.updated', 'download.job.deleted', 'series.created', 'series.updated', 'series.deleted'].forEach((eventType) => {
      source.addEventListener(eventType, (message) => {
        const data = (message as MessageEvent).data
        try { dispatch(JSON.parse(data) as RealtimeEvent) } catch { /* ignore malformed event */ }
      })
    })
  }, [dispatch])
  connectRef.current = connect

  const subscribe = useCallback((topic: string, handler: RealtimeHandler) => {
    const handlers = handlersRef.current.get(topic) || new Set<RealtimeHandler>()
    handlers.add(handler)
    handlersRef.current.set(topic, handlers)
    const hadTopic = topicsRef.current.has(topic)
    topicsRef.current.add(topic)
    if (!sourceRef.current || sourceRef.current.readyState === EventSource.CLOSED || !hadTopic) connectRef.current()
    return () => {
      handlers.delete(handler)
      if (!handlers.size) {
        handlersRef.current.delete(topic)
        topicsRef.current.delete(topic)
      }
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      if (retryRef.current !== null) window.clearTimeout(retryRef.current)
      sourceRef.current?.close()
    }
  }, [connect])

  const value = useMemo(() => ({ status, subscribe }), [status, subscribe])
  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>
}

export function useRealtimeEvents(topic: string, handler: RealtimeHandler) {
  const context = useContext(RealtimeContext)
  useEffect(() => context?.subscribe(topic, handler), [context, topic, handler])
  return context?.status || 'fallback'
}

export function useRealtimeStatus() {
  return useContext(RealtimeContext)?.status || 'fallback'
}
