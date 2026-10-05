import { localize } from '@/app/i18n'
import type { InstallStatus } from '@/features/project/project.api'

type Locale = Parameters<typeof localize>[0]
const stages: Record<string, [string, string]> = {
  driver_check: ['Đang kiểm tra driver đồ họa', 'Checking the graphics driver'],
  driver_uac: ['Hãy xác nhận quyền quản trị trong cửa sổ Windows', 'Please approve the Windows administrator prompt'],
  driver_search: ['Đang tìm driver NVIDIA phù hợp', 'Finding a compatible NVIDIA driver'],
  driver_download: ['Đang tải driver qua Windows Update', 'Downloading the driver through Windows Update'],
  driver_install: ['Đang cài driver. Màn hình có thể nháy; không tắt máy.', 'Installing the driver. The screen may flicker; do not shut down.'],
  driver_eula: ['Cần chấp nhận điều khoản driver để tiếp tục', 'Accept the driver terms to continue'],
  driver_reboot: ['Cần khởi động lại Windows để tiếp tục', 'Restart Windows to continue'],
  driver_verify: ['Đang kiểm tra driver sau cập nhật', 'Verifying the updated driver'],
  driver_resume: ['Đang tiếp tục cài gói AI', 'Resuming AI package installation'],
  driver_reconnect: ['Đang chờ tác vụ driver Windows kết thúc', 'Waiting for Windows driver maintenance to finish'],
  driver_unknown: ['Chưa xác định được kết quả cập nhật. Không bắt đầu cài thêm.', 'The update result is unknown. No additional installation will start.'],
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
  DRIVER_TOO_OLD: ['Driver NVIDIA hiện tại quá cũ để chạy gói AI.', 'The installed NVIDIA driver is too old for the AI runtime.'],
  NVIDIA_SMI_NOT_FOUND: ['Windows nhận diện NVIDIA nhưng thiếu công cụ kiểm tra driver.', 'Windows detected NVIDIA, but its driver management tool is missing.'],
  NVIDIA_DRIVER_UNAVAILABLE: ['Driver NVIDIA không phản hồi hoặc không hoạt động.', 'The NVIDIA driver is unavailable or not working.'],
  GPU_PROBE_FAILED: ['Chưa kiểm tra được GPU. Không thể kết luận driver quá cũ.', 'The GPU could not be checked. This does not mean the driver is outdated.'],
  NVIDIA_DEVICE_NOT_FOUND: ['Không tìm thấy GPU NVIDIA để cập nhật.', 'No NVIDIA GPU was found to update.'],
  GPU_JOBS_BUSY: ['Hãy chờ các tác vụ hiện tại hoàn tất rồi thử lại.', 'Wait for current jobs to finish, then retry.'],
  DRIVER_UPDATE_NOT_FOUND: ['Windows Update chưa có driver NVIDIA phù hợp cho máy này. Sao chép chẩn đoán để được hỗ trợ.', 'Windows Update has no compatible NVIDIA driver for this PC. Copy the diagnostics for support.'],
  DRIVER_UAC_CANCELLED: ['Bạn chưa cấp quyền cập nhật driver. Có thể thử lại.', 'Driver update permission was declined. You can retry.'],
  DRIVER_UPDATE_UNAVAILABLE: ['Không thể tự cập nhật driver trong môi trường này.', 'Automatic driver updates are unavailable in this environment.'],
  DRIVER_CONFIRMATION_EXPIRED: ['Xác nhận đã hết hạn hoặc trạng thái đã đổi. Hãy thử lại.', 'Confirmation expired or the state changed. Please retry.'],
  DRIVER_SEARCH_FAILED: ['Windows Update không tìm được bản cập nhật. Kiểm tra mạng hoặc chính sách quản trị.', 'Windows Update search failed. Check connectivity or administrator policy.'],
  DRIVER_DOWNLOAD_FAILED: ['Không tải được driver. Kiểm tra mạng rồi thử lại.', 'The driver could not be downloaded. Check connectivity and retry.'],
  DRIVER_INSTALL_BUSY: ['Windows đang cài bản cập nhật khác. Hãy chờ rồi thử lại.', 'Windows is installing another update. Wait and retry.'],
  DRIVER_CANDIDATE_CHANGED: ['Driver đề xuất đã thay đổi. Chưa cài; hãy thử lại.', 'The proposed driver changed. Nothing was installed; please retry.'],
  DRIVER_INSTALL_FAILED: ['Windows chưa cài thành công tất cả driver. Xem chẩn đoán.', 'Windows did not install all drivers successfully. See diagnostics.'],
  DRIVER_UPDATE_FAILED: ['Cập nhật driver thất bại. Runtime đang dùng được giữ nguyên.', 'Driver update failed. The current runtime was preserved.'],
  DRIVER_REBOOT_REQUIRED: ['Cần khởi động lại Windows trước khi tiếp tục.', 'Restart Windows before continuing.'],
  DRIVER_STATE_UNKNOWN: ['Chưa xác định được kết quả cập nhật driver. Không chạy thêm bộ cài.', 'The driver update result is unknown. Another installer will not be started.'],
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
  const downloadedBytes = Number(status.downloadedBytes)
  const totalBytes = Number(status.totalBytes)
  const bytes = Number.isFinite(downloadedBytes) && Number.isFinite(totalBytes) && (downloadedBytes > 0 || totalBytes > 0)
    ? `${(downloadedBytes / 1048576).toFixed(1)}/${(totalBytes / 1048576).toFixed(1)} MiB`
    : ''
  const speed = status.speedBytesPerSecond ? `${(status.speedBytesPerSecond / 1048576).toFixed(1)} MiB/s` : ''
  const eta = status.etaSeconds == null ? '' : `${localize(locale, 'Còn khoảng', 'About')} ${Math.ceil(status.etaSeconds / 60)} ${localize(locale, 'phút', 'min')}`
  return [localize(locale, ...stage), profile, active, bytes, speed, eta].filter(Boolean).join(' · ')
}

export function runtimeErrorText(status: InstallStatus, locale: Locale): string {
  const label = errors[status.errorCode || '']
  return label ? `${localize(locale, ...label)} [${status.errorCode}]` : localize(locale, 'Cài đặt thất bại. Xem log chi tiết.', 'Installation failed. See the diagnostic log.')
}
