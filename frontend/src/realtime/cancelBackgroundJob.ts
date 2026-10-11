import { fetchJson } from '@/shared/api/fetchJson'
import type { BackgroundJob } from './backgroundJobs'

const routes: Record<string, string> = {
  tts: '/api/tts/studio/jobs', flow: '/api/flow/jobs', download: '/api/download/jobs',
  drawing: '/api/drawing/jobs', automation: '/api/automation/jobs', cleaner: '/api/cleaner/jobs',
  'srt-image': '/api/media-compose/jobs', 'srt-export': '/api/srt-export/jobs', project: '/api/projects',
}

export function cancelBackgroundJob(job: Pick<BackgroundJob, 'topic' | 'id'>): Promise<{ ok?: boolean; alreadyRemoved?: boolean }> {
  const route = routes[job.topic]
  if (!route) throw new Error(`Unsupported task topic: ${job.topic}`)
  return fetchJson<{ ok?: boolean; alreadyRemoved?: boolean }>(`${route}/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' })
    .catch(async error => {
      if (job.topic !== 'flow' || !(error instanceof Error) || error.message !== 'Flow job not found') throw error
      // A removed job can outlive its delete event in the background panel.
      // Confirm against a fresh snapshot; never treat a failed cancel as success.
      const snapshot = await fetchJson<{ jobs?: Array<{ id: string }> }>('/api/flow/jobs', { method: 'GET' })
      if (!Array.isArray(snapshot.jobs) || !snapshot.jobs.every(row => row && typeof row.id === 'string')
        || snapshot.jobs.some(row => row.id === job.id)) throw error
      return { alreadyRemoved: true }
    })
}
