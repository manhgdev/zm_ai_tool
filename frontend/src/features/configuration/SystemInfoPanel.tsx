import { useEffect, useState } from 'react'
import { localize, useLocale, type AppLocale } from '@/app/i18n'
import { api } from '@/features/project/project.api'
import type { SystemInfo } from '@/features/project/project.types'
import { copyText } from '@/shared/lib/clipboard'

export function formatSystemInfo(info: SystemInfo, locale: AppLocale): string {
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const labels: Record<string, string> = {
    app: t('Ứng dụng', 'Application'), system: t('Hệ thống', 'System'), cpu: t('Bộ xử lý (CPU)', 'Processor (CPU)'),
    memory: t('Bộ nhớ (RAM / swap)', 'Memory (RAM / swap)'), graphics: t('Đồ họa', 'Graphics'), gpu: t('GPU', 'GPU'),
    disk: t('Ổ đĩa', 'Disk'), paths: t('Thư mục ứng dụng', 'Application folders'), tools: t('Công cụ xử lý', 'Media tools'),
    pythonPackages: t('Gói Python của ứng dụng', 'Application Python packages'), aiPackages: t('Gói AI runtime', 'AI runtime packages'),
    browser: t('Trình duyệt', 'Browser'), display: t('Màn hình', 'Display'),
    version: t('Phiên bản', 'Version'), packaged: t('Bản đóng gói', 'Packaged build'), python: t('Python', 'Python'),
    pythonBits: t('Python (bit)', 'Python (bits)'), executable: t('Tệp thực thi', 'Executable'),
    collectedAt: t('Thời điểm thu thập', 'Collected at'), os: t('Hệ điều hành', 'Operating system'), kernel: t('Nhân hệ điều hành', 'OS kernel'),
    arch: t('Kiến trúc', 'Architecture'), hostname: t('Tên máy', 'Computer name'), manufacturer: t('Nhà sản xuất', 'Manufacturer'),
    model: t('Mẫu / tên', 'Model / name'), bios: t('Phiên bản BIOS', 'BIOS version'), timezone: t('Múi giờ', 'Time zone'),
    uptime: t('Thời gian từ khi khởi động', 'System uptime'), physicalCores: t('Số nhân vật lý', 'Physical cores'),
    logicalCores: t('Số luồng xử lý', 'Logical processors'), usage: t('Đang sử dụng', 'Usage'), frequency: t('Xung nhịp hiện tại', 'Current frequency'),
    maxFrequency: t('Xung nhịp tối đa', 'Maximum frequency'), total: t('Tổng dung lượng', 'Total capacity'),
    available: t('Khả dụng', 'Available'), used: t('Đã dùng', 'Used'), free: t('Còn trống', 'Free'),
    swapTotal: t('Tổng swap', 'Total swap'), swapUsed: t('Swap đã dùng', 'Used swap'), adapterCount: t('Số GPU phát hiện', 'Detected GPUs'),
    acceleration: t('Loại tăng tốc phần cứng', 'Hardware acceleration type'), vendor: t('Hãng GPU', 'GPU vendor'),
    vram: t('Bộ nhớ GPU riêng (VRAM)', 'Dedicated GPU memory (VRAM)'), driver: t('Phiên bản driver', 'Driver version'),
    device: t('Thiết bị', 'Device'), mountpoint: t('Điểm gắn / ký tự ổ', 'Mount point / drive letter'), filesystem: t('Hệ thống tệp', 'File system'),
    appFolder: t('Ứng dụng', 'Application'), dataFolder: t('Dữ liệu', 'Private data'), publicFolder: t('Dữ liệu xuất bản', 'Public data'),
    runtimeFolder: t('AI runtime đang chọn', 'Selected AI runtime'), ffmpeg: t('FFmpeg', 'FFmpeg'), ffprobe: t('FFprobe', 'FFprobe'),
    packageCount: t('Số gói', 'Package count'), package: t('Gói', 'Package'), userAgent: t('Thông tin trình duyệt', 'User agent'),
    platform: t('Nền tảng trình duyệt báo cáo', 'Browser-reported platform'), language: t('Ngôn ngữ', 'Language'),
    online: t('Trình duyệt có kết nối mạng', 'Browser reports online'), cookiesEnabled: t('Cho phép cookie', 'Cookies enabled'),
    secureContext: t('Ngữ cảnh bảo mật', 'Secure context'), estimatedMemory: t('RAM ước tính (trình duyệt)', 'Estimated RAM (browser)'),
    resolution: t('Độ phân giải', 'Resolution'), availableResolution: t('Vùng màn hình khả dụng', 'Available screen area'),
    viewport: t('Kích thước cửa sổ', 'Viewport size'), pixelRatio: t('Tỉ lệ điểm ảnh', 'Pixel ratio'), colorDepth: t('Độ sâu màu (bit)', 'Color depth (bits)'),
    renderer: t('Bộ dựng WebGL', 'WebGL renderer'),
  }
  const label = (id: string) => labels[id] || id
  const number = (value: number, maximumFractionDigits = 2) => value.toLocaleString(locale === 'vi' ? 'vi-VN' : 'en-US', { maximumFractionDigits })
  const valueText = (row: SystemInfo['sections'][number]['rows'][number]): string => {
    const value = row.value
    if (value === null || value === '') return t('Không có dữ liệu', 'Unavailable')
    if (typeof value === 'boolean') return value ? t('Có', 'Yes') : t('Không', 'No')
    if (typeof value !== 'number') return value
    if (row.unit === 'bytes') {
      const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
      const index = value > 0 ? Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1) : 0
      return `${number(value / 1024 ** index)} ${units[index]}`
    }
    if (row.unit === 'seconds') {
      const seconds = Math.max(0, Math.floor(value))
      return `${Math.floor(seconds / 86400)} ${t('ngày', 'days')} ${Math.floor(seconds / 3600) % 24} ${t('giờ', 'hours')} ${Math.floor(seconds / 60) % 60} ${t('phút', 'minutes')}`
    }
    return `${number(value)}${row.unit === 'percent' ? '%' : row.unit === 'mhz' ? ' MHz' : ''}`
  }
  return [
    `ZM AI TOOL — ${t('Thông tin máy tính', 'Computer information')}`,
    `${label('collectedAt')}: ${info.collectedAt}`,
    ...info.sections.flatMap(section => [
      '', `[${label(section.id)}${section.name == null ? '' : ` · ${section.name}`}]`,
      ...section.rows.map(row => `${row.id === 'package' ? row.name || label(row.id) : label(row.id)}: ${valueText(row)}`),
    ]),
  ].join('\n')
}

function browserSystemInfo(): SystemInfo {
  const memory = (navigator as Navigator & { deviceMemory?: number }).deviceMemory
  let renderer: string | null = null
  try {
    const gl = document.createElement('canvas').getContext('webgl')
    const debug = gl?.getExtension('WEBGL_debug_renderer_info')
    if (gl) renderer = String(gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER))
    gl?.getExtension('WEBGL_lose_context')?.loseContext()
  } catch { /* Some browsers disable WebGL or hardware identification. */ }
  return {
    collectedAt: new Date().toISOString(),
    sections: [
      { id: 'browser', rows: [
        { id: 'userAgent', value: navigator.userAgent }, { id: 'platform', value: navigator.platform },
        { id: 'language', value: navigator.languages.join(', ') }, { id: 'timezone', value: Intl.DateTimeFormat().resolvedOptions().timeZone },
        { id: 'logicalCores', value: navigator.hardwareConcurrency || null },
        { id: 'estimatedMemory', value: memory ? memory * 1024 ** 3 : null, unit: 'bytes' },
        { id: 'online', value: navigator.onLine }, { id: 'cookiesEnabled', value: navigator.cookieEnabled },
        { id: 'secureContext', value: window.isSecureContext }, { id: 'renderer', value: renderer },
      ] },
      { id: 'display', rows: [
        { id: 'resolution', value: `${screen.width} × ${screen.height}` },
        { id: 'availableResolution', value: `${screen.availWidth} × ${screen.availHeight}` },
        { id: 'viewport', value: `${window.innerWidth} × ${window.innerHeight}` },
        { id: 'pixelRatio', value: window.devicePixelRatio }, { id: 'colorDepth', value: screen.colorDepth },
      ] },
    ],
  }
}

export default function SystemInfoPanel() {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [info, setInfo] = useState<SystemInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setFailed(false)
    setInfo(null)
    void api.systemInfo(controller.signal)
      .then(result => {
        if (!controller.signal.aborted) setInfo({ ...result, sections: [...result.sections, ...browserSystemInfo().sections] })
      })
      .catch(() => { if (!controller.signal.aborted) setFailed(true) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [revision])

  const report = info ? formatSystemInfo(info, locale) : ''
  return <div className="cfg-log-panel" aria-busy={loading}>
    <div className="cfg-log-actions">
      <h3 className="mr-auto self-center">{t('Thông tin máy tính', 'Computer information')}</h3>
      <button type="button" className="cfg-primary" disabled={!report || loading}
        onClick={() => void copyText(report).catch(() => {})}>
        {t('Sao chép toàn bộ', 'Copy all')}
      </button>
      <button type="button" className="cfg-secondary" disabled={loading} onClick={() => setRevision(value => value + 1)}>
        {t('Làm mới', 'Refresh')}
      </button>
    </div>
    <p className="cfg-hint" role="status">{loading
      ? t('Đang thu thập thông tin máy tính…', 'Collecting computer information…')
      : failed ? t('Không tải được thông tin máy tính. Hãy bấm Làm mới để thử lại.', 'Could not load computer information. Click Refresh to retry.')
        : t('Sao chép toàn bộ báo cáo bên dưới để gửi hỗ trợ.', 'Copy the complete report below to send to support.')}</p>
    <textarea className="cfg-log-pre cfg-system-report" readOnly spellCheck={false}
      aria-label={t('Báo cáo thông tin máy tính', 'Computer information report')} value={report} />
  </div>
}
