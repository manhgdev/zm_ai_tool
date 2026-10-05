import type { RealtimeEvent } from './types'

export type BackgroundJob = { id: string; topic: string; progress: number; message: string; queued: boolean; updatedAt: number }

export function updateBackgroundJobs(current: BackgroundJob[], event: RealtimeEvent): BackgroundJob[] {
  const normalize = (id: string, value: unknown): BackgroundJob[] => {
    if (!id || !value || typeof value !== 'object') return []
    const row = value as Record<string, unknown>
    const state = String(row.status || '')
    const running = event.topic === 'tts'
      ? !row.done && !row.error && (row.running === true || Number(row.pct) > 0 && Number(row.pct) < 99)
      : ['queued', 'processing', 'running', 'pending'].includes(state)
    if (!running) return []
    const pct = Number(row.pct ?? row.progress ?? 0)
    return [{ id, topic: event.topic, progress: Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0,
      message: String(row.message || row.name || row.filename || row.title || ''), queued: state === 'queued' || state === 'pending',
      updatedAt: Number.isFinite(event.timestamp) ? event.timestamp : Date.now() }]
  }
  if (event.type === 'snapshot') {
    const jobs = (event.payload as { jobs?: unknown } | null)?.jobs
    const rows = Array.isArray(jobs)
      ? jobs.flatMap(row => normalize(String(row.id || ''), row))
      : jobs && typeof jobs === 'object' ? Object.entries(jobs).flatMap(([id, row]) => normalize(id, row)) : []
    return [...current.filter(row => row.topic !== event.topic), ...rows]
  }
  if (!event.type.includes('.job.')) return current
  const next = normalize(event.entityId, event.payload)[0]
  const index = current.findIndex(row => row.topic === event.topic && row.id === event.entityId)
  if (event.type.endsWith('.deleted') || !next) return current.filter((_row, rowIndex) => rowIndex !== index)
  if (index < 0) return [next, ...current]
  return current.map((row, rowIndex) => rowIndex === index ? next : row)
}
