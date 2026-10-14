import { fetchJson } from '@/shared/api/fetchJson'
import type { CleanJob, CleanMethod, AdvancedOptions } from '@/pages/VideoCleanerPage'

const base = '/api/cleaner/jobs'

export const cleanerApi = {
  async list(): Promise<CleanJob[]> {
    return fetchJson<CleanJob[]>(base, undefined, 5000)
  },

  async start(files: File[], method: CleanMethod, options: AdvancedOptions, outputDir = ''): Promise<CleanJob[]> {
    const fd = new FormData()
    files.forEach(f => fd.append('files', f))
    fd.append('method', method)
    fd.append('options', JSON.stringify(options))
    fd.append('output_dir', outputDir)
    
    // timeout larger since uploading files might take time
    return fetchJson<CleanJob[]>(base, {
      method: 'POST',
      body: fd,
    }, 60000)
  },

  async cancel(id: string): Promise<{ ok: boolean }> {
    return fetchJson(`${base}/${id}/cancel`, { method: 'POST' }, 5000)
  },

  async retry(id: string, method: CleanMethod, options: AdvancedOptions, outputDir = ''): Promise<CleanJob> {
    const fd = new FormData()
    fd.append('method', method)
    fd.append('options', JSON.stringify(options))
    fd.append('output_dir', outputDir)
    return fetchJson(`${base}/${id}/retry`, { method: 'POST', body: fd }, 60000)
  },

  async remove(id: string): Promise<{ ok: boolean }> {
    return fetchJson(`${base}/${id}`, { method: 'DELETE' }, 5000)
  },

  async clearLogs(): Promise<{ ok: boolean; cleared: number }> {
    return fetchJson('/api/cleaner/logs', { method: 'DELETE' }, 5000)
  },

  async reveal(id: string): Promise<{ ok: boolean }> {
    return fetchJson(`${base}/${id}/reveal`, { method: 'POST' }, 5000)
  },

  fileUrl(id: string, download = false): string {
    return `${base}/${id}/file${download ? '?download=1' : ''}`
  },
}
