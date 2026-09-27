export type RealtimeStatus = 'connected' | 'reconnecting' | 'fallback'

export type RealtimeEvent<T = unknown> = {
  id: string
  type: string
  topic: string
  entityId: string
  payload: T
  timestamp: number
}

export type RealtimeHandler = (event: RealtimeEvent) => void
