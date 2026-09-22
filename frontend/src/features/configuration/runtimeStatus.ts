import { localize } from '@/app/i18n'
import type { InstallStatus } from '@/features/project/project.api'

type Locale = Parameters<typeof localize>[0]
const stages: Record<string, [string, string]> = {
  detect_hardware: ['Nhận diện phần cứng', 'Detecting hardware'],
  prepare_python: ['Chuẩn bị Python riêng', 'Preparing managed Python'],
  resolve: ['Xác định dependency phù hợp', 'Resolving compatible dependencies'],
  download: ['Đang tải package', 'Downloading packages'],
  install: ['Đang cài package (tải nền tiếp tục)', 'Installing packages (downloads continue)'],
  download_packages: ['Đang tải song song các nhóm (tiến độ theo nhóm)', 'Downloading groups in parallel (group progress)'],
  install_packages: ['Đang cài dependency', 'Installing dependencies'],
  probe: ['Kiểm tra runtime', 'Testing runtime'],
  activate: ['Kích hoạt runtime', 'Activating runtime'],
  rollback: ['Khôi phục runtime trước', 'Restoring previous runtime'],
}
const errors: Record<string, [string, string]> = {
  NETWORK_TIMEOUT: ['Máy chủ phản hồi quá lâu. Có thể thử lại từ cache.', 'Server timed out. Retry using cached downloads.'],
  FILE_ACCESS_DENIED: ['Windows từ chối ghi file. Kiểm tra quyền thư mục hoặc Protection history; không tắt antivirus.', 'Windows denied file access. Check folder permissions or Protection history; do not disable antivirus.'],
  DNS_FAILED: ['Không tìm thấy máy chủ tải. Kiểm tra DNS và kết nối.', 'Download host could not be resolved. Check DNS and connectivity.'],
  PROXY_AUTH_FAILED: ['Proxy yêu cầu xác thực. Kiểm tra cấu hình proxy.', 'Proxy authentication is required. Check proxy settings.'],
  TLS_CERTIFICATE_FAILED: ['Chứng chỉ HTTPS không hợp lệ. Kiểm tra ngày giờ, proxy và CA; không tắt xác minh TLS.', 'HTTPS certificate validation failed. Check clock, proxy and CA; do not disable TLS verification.'],
  HTTP_UNAUTHORIZED: ['Máy chủ yêu cầu xác thực. Xem chi tiết tải.', 'The download server requires authentication. See download details.'],
  HTTP_FORBIDDEN: ['Máy chủ từ chối tải (403). Kiểm tra quyền truy cập hoặc proxy.', 'Download denied (403). Check access or proxy settings.'],
  HTTP_NOT_FOUND: ['Package không tồn tại tại URL đã khóa (404). Cần cập nhật ứng dụng.', 'The locked package URL was not found (404). Update the application.'],
  HTTP_RATE_LIMITED: ['Máy chủ giới hạn lượt tải. Chờ rồi thử lại.', 'The server rate-limited downloads. Wait and retry.'],
  INDEX_UNAVAILABLE: ['Máy chủ package tạm thời không khả dụng.', 'The package server is temporarily unavailable.'],
  DOWNLOAD_INTERRUPTED: ['Tải gián đoạn. Lần thử lại sẽ dùng file tải dở nếu máy chủ hỗ trợ.', 'Download interrupted. Retry resumes the partial file when supported.'],
  DEPENDENCY_RESOLUTION_FAILED: ['Không tìm được bộ dependency tương thích. Xem log resolver.', 'Compatible dependencies could not be resolved. See the resolver log.'],
  CHECKSUM_MISMATCH: ['Checksum file tải không đúng; chỉ file hỏng đã bị loại bỏ.', 'Download checksum mismatch; only the corrupt file was removed.'],
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
  const active = ['download_packages', 'download', 'install', 'resolve'].includes(status.stage || '') ? status.currentPackage : ''
  const bytes = status.downloadedBytes == null ? '' : `${(status.downloadedBytes / 1048576).toFixed(1)}${status.totalBytes == null ? '' : '/' + (status.totalBytes / 1048576).toFixed(1)} MiB`
  const speed = status.speedBytesPerSecond ? `${(status.speedBytesPerSecond / 1048576).toFixed(1)} MiB/s` : ''
  const eta = status.etaSeconds == null ? '' : `${localize(locale, 'Còn khoảng', 'About')} ${Math.ceil(status.etaSeconds / 60)} ${localize(locale, 'phút', 'min')}`
  return [localize(locale, ...stage), profile, active, bytes, speed, eta].filter(Boolean).join(' · ')
}

export function runtimeErrorText(status: InstallStatus, locale: Locale): string {
  const label = errors[status.errorCode || '']
  return label ? `${localize(locale, ...label)} [${status.errorCode}]` : localize(locale, 'Cài đặt thất bại. Xem log chi tiết.', 'Installation failed. See the diagnostic log.')
}
