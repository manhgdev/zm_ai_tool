import { localize } from '@/app/i18n'
import type { InstallStatus } from '@/features/project/project.api'

type Locale = Parameters<typeof localize>[0]
const stages: Record<string, [string, string]> = {
  detect_hardware: ['Nhận diện phần cứng', 'Detecting hardware'],
  prepare_python: ['Chuẩn bị Python riêng', 'Preparing managed Python'],
  download_packages: ['Đang tải song song các nhóm (tiến độ theo nhóm)', 'Downloading groups in parallel (group progress)'],
  install_packages: ['Đang cài dependency', 'Installing dependencies'],
  probe: ['Kiểm tra runtime', 'Testing runtime'],
  activate: ['Kích hoạt runtime', 'Activating runtime'],
  rollback: ['Khôi phục runtime trước', 'Restoring previous runtime'],
}
const errors: Record<string, [string, string]> = {
  HARDWARE_UNSUPPORTED: ['Không xác định được cấu hình hỗ trợ.', 'Cannot identify a supported hardware configuration.'],
  DRIVER_TOO_OLD: ['Driver NVIDIA thiếu, quá cũ hoặc không hoạt động.', 'NVIDIA driver is missing, outdated or unavailable.'],
  DOWNLOAD_FAILED: ['Tải thất bại. Kiểm tra mạng hoặc proxy rồi thử lại.', 'Download failed. Check the network or proxy and retry.'],
  PYTHON_PREPARE_FAILED: ['Không chuẩn bị được Python riêng. Xem log chi tiết.', 'Could not prepare managed Python. See the diagnostic log.'],
  DEPENDENCY_INSTALL_FAILED: ['Cài dependency thất bại. Xem package và lệnh trong log.', 'Dependency installation failed. See the package and command in the log.'],
  RUNTIME_PROBE_FAILED: ['Runtime chưa vượt qua kiểm tra. Bản đang dùng được giữ nguyên.', 'Runtime validation failed. The active runtime was preserved.'],
  DISK_FULL: ['Không đủ dung lượng để cài song song runtime mới.', 'Not enough disk space for a parallel runtime installation.'],
  ACTIVATION_FAILED: ['Không chuyển được runtime. Xem log chi tiết.', 'Could not switch runtimes. See the diagnostic log.'],
  RUNTIME_BUSY: ['Đang có tác vụ cài hoặc khôi phục runtime.', 'Another runtime installation or rollback is running.'],
}

export function runtimeStatusText(status: InstallStatus, locale: Locale): string {
  const stage = stages[status.stage || '']
  if (!stage) return localize(locale, 'Đang chuẩn bị cài đặt…', 'Preparing installation…')
  const profile = status.runtimeProfile || status.runtimePack
  // Package commands and versions are already present in the diagnostic log.
  const downloading = status.stage === 'download_packages' ? status.currentPackage : ''
  return [localize(locale, ...stage), profile, downloading].filter(Boolean).join(' · ')
}

export function runtimeErrorText(status: InstallStatus, locale: Locale): string {
  const label = errors[status.errorCode || '']
  return label ? `${localize(locale, ...label)} [${status.errorCode}]` : localize(locale, 'Cài đặt thất bại. Xem log chi tiết.', 'Installation failed. See the diagnostic log.')
}
