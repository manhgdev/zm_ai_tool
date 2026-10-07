import { fetchJson } from '@/shared/api/fetchJson'
import type { BackgroundJob } from './backgroundJobs'

const routes: Record<string, string> = {
  tts: '/api/tts/studio/jobs', flow: '/api/flow/jobs', download: '/api/download/jobs',
  drawing: '/api/drawing/jobs', automation: '/api/automation/jobs', cleaner: '/api/cleaner/jobs',
  'srt-image': '/api/media-compose/jobs', 'srt-export': '/api/srt-export/jobs', project: '/api/projects',
}

export function cancelBackgroundJob(job: Pick<BackgroundJob, 'topic' | 'id'>) {
  const route = routes[job.topic]
  if (!route) throw new Error(`Unsupported task topic: ${job.topic}`)
  return fetchJson<{ ok?: boolean }>(`${route}/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' })
}
