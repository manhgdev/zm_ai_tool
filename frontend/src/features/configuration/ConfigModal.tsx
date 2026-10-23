import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { AppConfig, CloudProviderId, SystemChecks } from '@/features/project/project.types'
import { api, InstallCancelledError, type InstallStatus } from '@/features/project/project.api'
import ProgressPopup from '@/shared/components/ProgressPopup'
import { ConfirmDialog } from '@/shared/components/ConfirmDialog'
import LicensePage from '@/features/license/LicensePage'
import SystemInfoPanel from './SystemInfoPanel'
import type { LicenseStatus } from '@/features/license/license.api'
import { localize, useLocale } from '@/app/i18n'
import { copyText } from '@/shared/lib/clipboard'
import { toast } from 'sonner'
import { IconEye, IconEyeOff } from '@/shared/components/Icons'
import { LocaleSwitch } from '@/shared/components/LocaleSwitch'
import './ConfigModal.css'
import { setupLabel, setupDetail } from './setupLabels'
import { runtimeStatusText, runtimeErrorText } from './runtimeStatus'

import {
  type InstallKind, type Section, type CloudTab, type UpdateDialog, type CloudDraft,
  PROVIDERS, PROVIDER_PRESET_MODELS,
  nextAutoInstall, emptyCloud, providerKeyPlaceholder, installLabel, normalizeInstallKind,
  isExclusiveInstallKind, packageInstallBusy,
} from './configModal.helpers'

interface ElKeySlot {
  savedIndex: number | null
  value: string
  visible?: boolean
  edited?: boolean
}

type DiscoveredModel = {
  id: string
  label?: string
  free?: boolean
  capabilities?: string[]
}

type Props = {
  open: boolean
  onClose: () => void
  section: Section
  onSectionChange: (section: Section) => void
  forceSetup?: boolean
  onSetupReady?: () => void
  onSaved?: () => void
  licenseStatus?: LicenseStatus
  taskAccessRequired?: boolean
  onLicenseStatusChange?: (status: LicenseStatus) => void
}

export default function ConfigModal({
  open,
  onClose,
  section,
  onSectionChange: setSection,
  forceSetup = false,
  onSetupReady,
  onSaved,
  licenseStatus,
  taskAccessRequired = false,
  onLicenseStatusChange,
}: Props) {
  const { locale, setLocale } = useLocale()
  const localeRef = useRef(locale)
  localeRef.current = locale
  const t = (vietnamese: string, english: string) => localize(locale, vietnamese, english)
  const packageName = (kind: string) => installLabel(kind, locale)
  const [draft, setDraft] = useState<CloudDraft>(emptyCloud)
  /** Mỗi ô 1 key; savedIndex tham chiếu key cũ trên server, value là key mới người dùng nhập */
  const [elSlots, setElSlots] = useState<ElKeySlot[]>([{ savedIndex: null, value: '', visible: false, edited: false }])
  const [rawElKeys, setRawElKeys] = useState<string[]>([])
  const [elSavedCount, setElSavedCount] = useState(0)
  const [elDirty, setElDirty] = useState(false)
  const [cloudKeySlots, setCloudKeySlots] = useState<Record<CloudProviderId, ElKeySlot[]>>(() =>
    Object.fromEntries(PROVIDERS.map((id) => [id, [{ savedIndex: null, value: '', visible: false, edited: false }]])) as Record<CloudProviderId, ElKeySlot[]>
  )
  const [rawCloudKeys, setRawCloudKeys] = useState<Record<CloudProviderId, string[]>>(() =>
    Object.fromEntries(PROVIDERS.map((id) => [id, [] as string[]])) as unknown as Record<CloudProviderId, string[]>
  )
  const [cloudKeysDirty, setCloudKeysDirty] = useState<Record<CloudProviderId, boolean>>(() =>
    Object.fromEntries(PROVIDERS.map((id) => [id, false])) as Record<CloudProviderId, boolean>
  )
  const [customModelTabs, setCustomModelTabs] = useState<Record<CloudProviderId, boolean>>(() =>
    Object.fromEntries(PROVIDERS.map((id) => [id, false])) as Record<CloudProviderId, boolean>
  )
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const [tab, setTab] = useState<CloudTab>('openai')
  const [discoveredModels, setDiscoveredModels] = useState<Partial<Record<CloudProviderId, DiscoveredModel[]>>>({})
  const [modelsLoading, setModelsLoading] = useState(false)
  const [checks, setChecks] = useState<SystemChecks | null>(null)
  const [checksLoading, setChecksLoading] = useState(false)
  const [checksErr, setChecksErr] = useState('')
  const [installing, setInstalling] = useState<string | null>(null)
  const [installQueue, setInstallQueue] = useState<InstallKind[]>([])
  const [installProgressMinimized, setInstallProgressMinimized] = useState(false)
  const [installPopupError, setInstallPopupError] = useState('')
  const [driverStatus, setDriverStatus] = useState<InstallStatus>({ running: false })
  const [driverConfirm, setDriverConfirm] = useState<'update' | 'accept_eula' | 'reboot' | null>(null)
  const [driverSubmitting, setDriverSubmitting] = useState(false)
  const [installStatusReady, setInstallStatusReady] = useState(false)
  const observedDriverDone = useRef('')
  const driverWaiting = driverStatus.stage === 'driver_eula' || driverStatus.rebootRequired
  const gpuChoice = Boolean(driverStatus.driverRepairAvailable || driverStatus.cpuRuntimeAvailable)
  const [installLog, setInstallLog] = useState('')
  const [diagnosticLog, setDiagnosticLog] = useState('')
  const [diagnosticLogLoading, setDiagnosticLogLoading] = useState(false)
  const [installProgress, setInstallProgress] = useState(0)
  const [installMessage, setInstallMessage] = useState('')
  const [pendingRestart, setPendingRestart] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [updateChecking, setUpdateChecking] = useState(false)
  const [updateDialog, setUpdateDialog] = useState<UpdateDialog | null>(null)
  const [updateMinimized, setUpdateMinimized] = useState(false)
  const autoSetupLock = useRef(false)
  const autoAttempted = useRef<Set<string>>(new Set())
  const restartRequested = useRef(false)
  const observedInstallError = useRef('')
  const installActiveRef = useRef<InstallKind | null>(null)
  const installQueueRef = useRef<InstallKind[]>([])
  const installCancelRef = useRef(false)
  const pendingRestartRef = useRef(false)
  pendingRestartRef.current = pendingRestart
  const installBusy = Boolean(installing || installQueue.length)
  const exclusiveInstall = isExclusiveInstallKind(installing)
  const [outputRoot, setOutputRoot] = useState('')
  const savedOutputRoot = useRef<string | null>(null)
  const [outputRootDefault, setOutputRootDefault] = useState('')
  const [outputRootSaving, setOutputRootSaving] = useState(false)
  const cleanupLabels: Record<string, [string, string]> = {
    data_srt_image: ['Data · SRT ảnh', 'Data · SRT images'],
    data_srt_export: ['Data · SRT video', 'Data · SRT video'],
    data_automation: ['Data · file tác vụ Automation', 'Data · Automation job files'],
    public_upload: ['Public · file upload', 'Public · uploads'],
    data_tts_temp: ['Data · file TTS tạm', 'Data · temporary TTS files'],
    data_review: ['Data · cache Review', 'Data · Review cache'],
    data_artifacts: ['Data · cache xử lý', 'Data · processing cache'],
    data_capcut: ['Data · phản hồi CapCut lưu tạm', 'Data · cached CapCut responses'],
    data_voice_cache: ['Data · cache giọng nói', 'Data · voice cache'],
    data_cleaner: ['Data · kết quả làm sạch video', 'Data · cleaned video outputs'],
    data_legacy_tts: ['Data · kết quả TTS cũ', 'Data · legacy TTS outputs'],
    data_cache: ['Data cache', 'Data cache'], data_temp: ['Data tạm', 'Temporary data'], data_logs: ['Log data', 'Data logs'],
    public_exports: ['Public exports', 'Public exports'], public_tts: ['Public TTS', 'Public TTS'], public_flow: ['Public Flow', 'Public Flow'],
    public_downloads: ['Public downloads', 'Public downloads'], public_cleaner: ['Public cleaner', 'Public cleaner'],
  }
  const [cleanupTargets, setCleanupTargets] = useState<Record<string, { bytes: number; items: number; path?: string; selectable?: boolean; unreadable?: boolean }>>({})
  const [cleanupSelected, setCleanupSelected] = useState<string[]>([])
  const [cleanupRunning, setCleanupRunning] = useState(false)
  const [cleanupConfirm, setCleanupConfirm] = useState(false)
  const [cleanupView, setCleanupView] = useState<'selectable' | 'protected'>('selectable')
  const cleanupAvailable = Object.keys(cleanupTargets).filter(key => !cleanupTargets[key].unreadable && (cleanupTargets[key].selectable ?? Object.hasOwn(cleanupLabels, key)))

  useEffect(() => {
    if (!open || section !== 'cleanup') return
    api.cleanupInventory().then(result => {
      setCleanupTargets(result.targets)
      setCleanupSelected([])
    }).catch(() => toast.error(t('Không đọc được dung lượng dọn dẹp.', 'Could not read cleanup inventory.')))
  }, [open, section])

  const runCleanup = async () => {
    if (!cleanupSelected.length || cleanupRunning) return
    setCleanupConfirm(false)
    setCleanupRunning(true)
    try {
      const result = await api.cleanup(cleanupSelected)
      if (!result.ok || result.errors?.length) {
        toast.error(t('Chưa dọn được hết các mục đã chọn.', 'Some selected items could not be cleaned.'), { description: result.errors?.join('; ') })
      } else {
        toast.success(t('Đã dọn dẹp dữ liệu đã chọn.', 'Selected data cleaned.'))
      }
      try {
        const next = await api.cleanupInventory()
        setCleanupTargets(next.targets)
      } catch {
        toast.error(t('Không cập nhật được dung lượng sau khi dọn.', 'Could not refresh storage usage after cleanup.'))
      }
    } catch (error) {
      toast.error(String(error).includes('CLEANUP_JOBS_BUSY') ? t('Hãy chờ tác vụ đang chạy hoàn tất trước khi dọn dẹp.', 'Wait for running tasks to finish before cleanup.') : String(error))
    } finally { setCleanupRunning(false) }
  }

  useEffect(() => {
    if (!open || section !== 'cloud') return
    let cancelled = false
    setModelsLoading(true)
    fetch(`/api/chat/models?provider=${encodeURIComponent(tab)}&includePaid=true&refresh=true`)
      .then(async (response) => {
        if (!response.ok) throw new Error('model discovery failed')
        const data = await response.json() as { models?: unknown }
        const rows = Array.isArray(data.models) ? data.models.flatMap((item) => {
          if (!item || typeof item !== 'object') return []
          const row = item as Record<string, unknown>
          const id = String(row.id || '').trim()
          const capabilities = Array.isArray(row.capabilities) ? row.capabilities.map(String) : []
          if (capabilities.length && !capabilities.includes('text')) return []
          return id ? [{ id, label: String(row.label || id), free: row.free === true, capabilities }] : []
        }) : []
        if (!cancelled) setDiscoveredModels((current) => ({ ...current, [tab]: rows }))
      })
      .catch(() => {
        if (!cancelled) setDiscoveredModels((current) => ({ ...current, [tab]: [] }))
      })
      .finally(() => {
        if (!cancelled) setModelsLoading(false)
      })
    return () => { cancelled = true }
  }, [open, section, tab])

  const checkForUpdate = async () => {
    setUpdateChecking(true)
    try {
      const result = await api.checkAppUpdate()
      if (!result.desktop || !result.supported) {
        setUpdateDialog({
          kind: 'info',
          title: t('Không thể cập nhật tại đây', 'Updates are unavailable here'),
          detail: t('Cập nhật chỉ áp dụng cho bản APP macOS/Windows đã đóng gói.', 'Updates are available only in the packaged macOS/Windows APP.'),
        })
        return
      }
      if (!result.updateAvailable) {
        setUpdateDialog({
          kind: 'info',
          title: result.releaseAvailable ? t('Chưa có gói phù hợp', 'No compatible package yet') : t('Đã là phiên bản mới nhất', 'You are up to date'),
          detail: result.releaseAvailable
            ? t('Bản phát hành chưa có gói đúng cho thiết bị này.', 'The release has no package for this device yet.')
            : t(`Bạn đang dùng v${result.currentVersion}.`, `You are using v${result.currentVersion}.`),
        })
        return
      }
      setUpdateDialog({
        kind: 'available',
        title: t(`Đã có bản v${result.latestVersion}`, `Version ${result.latestVersion} is available`),
        detail: t('Tải và cài tự động. Ứng dụng sẽ đóng rồi tự mở lại sau khi cập nhật.', 'Download and install automatically. The app will close and reopen after updating.'),
      })
    } catch (error) {
      setUpdateDialog({
        kind: 'error',
        title: t('Không thể kiểm tra cập nhật', 'Could not check for updates'),
        detail: error instanceof Error ? error.message : t('Vui lòng thử lại sau.', 'Please try again later.'),
      })
    } finally {
      setUpdateChecking(false)
    }
  }

  const updateApplyStarted = useRef(false)
  const updateCancelRequested = useRef(false)
  const updateDownloadStarted = useRef(false)
  const updateRetryAfterCancel = useRef(false)

  const downloadUpdate = async () => {
    if (updateDownloadStarted.current) return
    updateDownloadStarted.current = true
    updateCancelRequested.current = false
    updateApplyStarted.current = false
    try {
      const started = await api.installAppUpdate()
      if (started.busy || started.ok === false) {
        updateDownloadStarted.current = false
        // Previous cancel/download still winding down — wait, then auto-retry once.
        updateRetryAfterCancel.current = true
        setUpdateDialog({
          kind: 'cancelling',
          title: t('Đang chờ hủy cập nhật trước', 'Waiting for previous cancel'),
          detail: started.message || t('Đợi tải cũ dừng xong rồi tự thử lại.', 'Waiting for the previous download to stop, then retrying automatically.'),
          progress: 0,
        })
        return
      }
      updateRetryAfterCancel.current = false
      setUpdateDialog({ kind: 'downloading', title: t('Đang tải cập nhật', 'Downloading update'), detail: t('Đang tải gói cài đặt…', 'Downloading the installation package…'), progress: 0 })
    } catch (error) {
      updateDownloadStarted.current = false
      updateRetryAfterCancel.current = false
      setUpdateDialog({ kind: 'error', title: t('Không thể tải cập nhật', 'Could not download update'), detail: error instanceof Error ? error.message : t('Vui lòng thử lại sau.', 'Please try again later.') })
    }
  }

  const applyUpdate = useCallback(async () => {
    if (updateApplyStarted.current || updateCancelRequested.current) return
    updateApplyStarted.current = true
    const text = (vi: string, en: string) => localize(localeRef.current, vi, en)
    setUpdateDialog({ kind: 'applying', title: text('Đang cài cập nhật', 'Installing update'), detail: text('Ứng dụng sẽ đóng để cài đặt và tự mở lại khi hoàn tất.', 'The app will close for installation and reopen automatically when finished.'), progress: 0 })
    try {
      await api.applyAppUpdate()
      // The detached updater now owns installation and relaunch; API disconnect is expected.
    } catch (error) {
      updateApplyStarted.current = false
      updateDownloadStarted.current = false
      setUpdateDialog({ kind: 'error', title: text('Không thể cài cập nhật', 'Could not install update'), detail: error instanceof Error ? error.message : text('Vui lòng thử lại sau.', 'Please try again later.') })
    }
  }, [])

  const cancelUpdate = useCallback(async () => {
    if (updateCancelRequested.current) return
    updateCancelRequested.current = true
    updateRetryAfterCancel.current = false
    const text = (vi: string, en: string) => localize(localeRef.current, vi, en)
    setUpdateDialog((current) => ({
      kind: 'cancelling',
      title: text('Đang hủy cập nhật', 'Cancelling update'),
      detail: text('Đang dừng tải, ứng dụng vẫn hoạt động.', 'Stopping the download; the app will remain open.'),
      progress: current?.progress ?? 0,
    }))
    try {
      await api.cancelAppUpdate()
    } catch (error) {
      updateCancelRequested.current = false
      toast.error(String(error))
      return
    }
    updateDownloadStarted.current = false
    updateApplyStarted.current = false
  }, [])

  useEffect(() => {
    if (updateDialog?.kind !== 'downloading' && updateDialog?.kind !== 'cancelling') return
    let cancelled = false
    let timer: number | undefined
    const text = (vi: string, en: string) => localize(localeRef.current, vi, en)
    const poll = async () => {
      try {
        const state = await api.getAppUpdateStatus()
        if (cancelled) return
        if (state.phase === 'error') {
          updateDownloadStarted.current = false
          updateRetryAfterCancel.current = false
          updateCancelRequested.current = false
          setUpdateDialog({ kind: 'error', title: text('Không thể tải cập nhật', 'Could not download update'), detail: state.error || state.message })
          return
        }
        if (state.phase === 'cancelling') {
          setUpdateDialog({
            kind: 'cancelling',
            title: text('Đang hủy cập nhật', 'Cancelling update'),
            detail: text('Đang dừng tải, ứng dụng vẫn hoạt động.', 'Stopping the download; the app will remain open.'),
            progress: state.progress,
          })
          if (!cancelled) timer = window.setTimeout(() => void poll(), 300)
          return
        }
        if (state.phase === 'cancelled' || (!state.running && updateDialog.kind === 'cancelling' && state.phase !== 'downloading' && state.phase !== 'checking' && state.phase !== 'ready')) {
          const shouldRetry = updateRetryAfterCancel.current
          updateRetryAfterCancel.current = false
          updateCancelRequested.current = false
          updateDownloadStarted.current = false
          updateApplyStarted.current = false
          if (shouldRetry) {
            setUpdateDialog({ kind: 'downloading', title: text('Đang tải cập nhật', 'Downloading update'), detail: text('Đang tải gói cài đặt…', 'Downloading the installation package…'), progress: 0 })
            // Allow downloadUpdate() to run again after cancel finished.
            void (async () => {
              try {
                updateDownloadStarted.current = true
                const started = await api.installAppUpdate()
                if (started.busy || started.ok === false) {
                  updateDownloadStarted.current = false
                  updateRetryAfterCancel.current = true
                  setUpdateDialog({
                    kind: 'cancelling',
                    title: text('Đang chờ hủy cập nhật trước', 'Waiting for previous cancel'),
                    detail: started.message || text('Đợi tải cũ dừng xong rồi tự thử lại.', 'Waiting for the previous download to stop, then retrying automatically.'),
                    progress: 0,
                  })
                  return
                }
                setUpdateDialog({ kind: 'downloading', title: text('Đang tải cập nhật', 'Downloading update'), detail: text('Tải xong sẽ tự cài và mở lại ứng dụng.', 'The update will install automatically and reopen the app.'), progress: 0 })
              } catch (error) {
                updateDownloadStarted.current = false
                setUpdateDialog({ kind: 'error', title: text('Không thể tải cập nhật', 'Could not download update'), detail: error instanceof Error ? error.message : text('Vui lòng thử lại sau.', 'Please try again later.') })
              }
            })()
            return
          }
          setUpdateDialog(null)
          return
        }
        if (updateCancelRequested.current && state.phase !== 'ready' && state.phase !== 'applying') {
          // User asked to cancel; ignore download progress until cancelled/idle.
          if (!cancelled) timer = window.setTimeout(() => void poll(), 300)
          return
        }
        if (state.phase === 'ready') {
          await applyUpdate()
          return
        }
        if (state.phase === 'applying') {
          setUpdateDialog({ kind: 'applying', title: text('Đang cài cập nhật', 'Installing update'), detail: text('Ứng dụng sẽ tự mở lại sau khi cài xong.', 'The app will reopen automatically after installation.'), progress: 0 })
          return
        }
        if (state.phase === 'complete') {
          updateDownloadStarted.current = false
          setUpdateDialog({ kind: 'info', title: text('Đã là phiên bản mới nhất', 'You are up to date'), detail: state.message })
          return
        }
        setUpdateDialog({ kind: 'downloading', title: text('Đang tải cập nhật', 'Downloading update'), detail: text('Tải xong sẽ tự cài và mở lại ứng dụng.', 'The update will install automatically and reopen the app.'), progress: state.progress })
      } catch (error) {
        if (!cancelled) {
          updateDownloadStarted.current = false
          updateRetryAfterCancel.current = false
          setUpdateDialog({ kind: 'error', title: text('Mất kết nối cập nhật', 'Update connection failed'), detail: error instanceof Error ? error.message : text('Vui lòng thử lại sau.', 'Please try again later.') })
        }
        return
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 800)
    }
    void poll()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [applyUpdate, updateDialog?.kind])

  useEffect(() => {
    if (updateDialog?.kind !== 'downloading' && updateDialog?.kind !== 'cancelling') {
      setUpdateMinimized(false)
    }
  }, [updateDialog?.kind])

  const loadChecks = useCallback((refresh = false, deep = false) => {
    setChecksLoading(true)
    setChecksErr('')
    void (async () => {
      for (let attempt = 0; attempt < 60; attempt += 1) {
        const result = await api.systemChecks(refresh && attempt === 0, deep)
        if (!result.loading) return result
        await new Promise((resolve) => window.setTimeout(resolve, 500))
      }
      throw new Error('Ứng dụng chuẩn bị quá lâu. Vui lòng mở lại APP.')
    })()
      .then(setChecks)
      .catch((e: Error) => {
        setChecksErr(e.message || 'Không kiểm tra được hệ thống')
        setChecks(null)
      })
      .finally(() => setChecksLoading(false))
  }, [forceSetup])

  useEffect(() => {
    if (!open) return
    // Load current outputRoot from /api/ui-preferences and resolved default from /api/config
    api.getLocalePreference().then((p) => {
      const saved = (p as unknown as { outputRoot?: string | null }).outputRoot || ''
      savedOutputRoot.current = saved
      setOutputRoot(saved)
    }).catch(() => { })
    api.getConfig().then((cfg) => {
      setOutputRootDefault((cfg as unknown as { desktopOutputRoot?: string }).desktopOutputRoot || '')
    }).catch(() => { })
  }, [open])

  useEffect(() => {
    if (!open) return
    setLoading(true)
    setMsg('')
    void api
      .getConfig()
      .then((cfg: AppConfig) => {
        const next = emptyCloud()
        for (const id of PROVIDERS) {
          const c = cfg.cloud?.[id]
          if (!c) continue
          next[id] = {
            apiKey: c.apiKey || '',
            apiKeys: c.apiKeys || '', keyCount: c.keyCount || 0,
            baseUrl: c.baseUrl || next[id].baseUrl,
            model: c.model || next[id].model,
            apiKeySet: !!c.apiKeySet,
            label: c.label || next[id].label,
          }
        }
        setDraft(next)
        setRawCloudKeys(
          Object.fromEntries(
            PROVIDERS.map((id) => {
              const c = cfg.cloud?.[id]
              const keys = c?.rawKeys?.length
                ? c.rawKeys
                : (c?.apiKeys ? c.apiKeys.split(',').map((k) => k.trim()).filter(Boolean) : (c?.apiKey ? [c.apiKey] : []))
              return [id, keys]
            })
          ) as unknown as Record<CloudProviderId, string[]>
        )
        setCloudKeySlots(
          Object.fromEntries(
            PROVIDERS.map((id) => {
              const c = cfg.cloud?.[id]
              const count = c?.apiKeySet ? Math.max(1, Number(c.keyCount || 0)) : 0
              return [
                id,
                count > 0
                  ? Array.from({ length: count }, (_, i) => ({ savedIndex: i, value: '', visible: false, edited: false }))
                  : [{ savedIndex: null, value: '', visible: false, edited: false }],
              ]
            })
          ) as Record<CloudProviderId, ElKeySlot[]>
        )
        setCloudKeysDirty(Object.fromEntries(PROVIDERS.map((id) => [id, false])) as Record<CloudProviderId, boolean>)
        const el = cfg.tts?.elevenlabs
        const n = el?.apiKeySet ? Math.max(1, Number(el?.keyCount || 0)) : 0
        setElSavedCount(n)
        const elKeys = el?.rawKeys?.length
          ? el.rawKeys
          : (el?.apiKeys ? el.apiKeys.split(',').map((k) => k.trim()).filter(Boolean) : [])
        setRawElKeys(elKeys)
        if (n > 0) {
          setElSlots(Array.from({ length: n }, (_, i) => ({ savedIndex: i, value: '', visible: false, edited: false })))
        } else {
          setElSlots([{ savedIndex: null, value: '', visible: false, edited: false }])
        }
        setElDirty(false)
      })
      .catch((e: Error) => setMsg(e.message || 'Không tải được cấu hình'))
      .finally(() => setLoading(false))
  }, [open])

  useEffect(() => {
    if (!open) return
    const onHardwareChange = () => loadChecks(true, false)
    window.addEventListener('zm-hardware-mode-changed', onHardwareChange)
    return () => window.removeEventListener('zm-hardware-mode-changed', onHardwareChange)
  }, [open, loadChecks])

  useEffect(() => {
    if (!open) return
    if (section === 'setup' || section === 'logs' || forceSetup) loadChecks(false, false)
  }, [open, section, forceSetup, loadChecks])

  useEffect(() => {
    if (!open || section !== 'logs' || forceSetup || !checks?.ok) return
    let cancelled = false
    let timer: number | undefined
    const loadDiagnosticLog = async () => {
      setDiagnosticLogLoading(true)
      try {
        const result = await api.getAppLogs(200)
        if (!cancelled) setDiagnosticLog(result.text || '')
      } catch {
        if (!cancelled) setDiagnosticLog('')
      } finally {
        if (!cancelled) setDiagnosticLogLoading(false)
      }
      if (!cancelled) timer = window.setTimeout(() => void loadDiagnosticLog(), 3000)
    }
    void loadDiagnosticLog()
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [open, section, forceSetup, checks?.ok])

  useEffect(() => {
    if (!open || (section !== 'setup' && section !== 'logs')) return
    let cancelled = false
    let errCount = 0
    let timerId: number

    const syncInstall = async () => {
      try {
        const st = await api.installStatus()
        if (cancelled) return
        errCount = 0  // reset on success
        setDriverStatus(st)
        if (st.log || st.error || st.diagnostics) {
          setInstallLog((previous) => {
            let next = st.log || previous
            for (const chunk of [st.diagnostics, st.error]) {
              const text = String(chunk || '').trim()
              if (text && !next.includes(text)) next = next ? `${next}\n${text}` : text
            }
            return next
          })
        }
        setInstallStatusReady(true)
        if (st.running && st.kind) {
          setInstalling(normalizeInstallKind(st.kind))
          setInstallProgress(Number(st.progress) || 1)
          setInstallMessage(runtimeStatusText(st, locale))
        } else {
          if (!installActiveRef.current && !installQueueRef.current.length) setInstalling(null)
          if ((st.kind === 'nvidia_driver' || st.kind === 'cpu_runtime') && !st.error) {
            setInstallPopupError('')
            setInstallMessage(runtimeStatusText(st, locale))
            if (st.needsRestart) setPendingRestart(true)
            const completionId = st.operationId || String(st.startedAt || '')
            if (st.ok && !st.rebootRequired && st.stage !== 'driver_eula' && observedDriverDone.current !== completionId) {
              observedDriverDone.current = completionId
              setChecksErr('')
              setMsg(st.computeMode === 'cpu' ? localize(locale, 'Cài đặt hoàn tất. Đang dùng CPU.', 'Installation complete. Using CPU.') : localize(locale, 'Cài đặt hoàn tất.', 'Installation complete.'))
              loadChecks(true, false)
            }
          }
          if (st.error && st.error !== observedInstallError.current) {
            observedInstallError.current = st.error
            const message = runtimeErrorText(st, locale)
            setInstallPopupError(message)
            setChecksErr(message)
          }
        }
      } catch {
        /* backend chưa sẵn sàng — backoff */
        errCount++
      }
      if (cancelled) return
      // backoff: 2s → 4s → 8s → dừng sau 5 lỗi liên tiếp
      if (errCount >= 5) return
      const delay = errCount > 0 ? Math.min(2000 * Math.pow(2, errCount - 1), 16000) : 2000
      timerId = window.setTimeout(() => void syncInstall(), delay)
    }

    void syncInstall()
    return () => {
      cancelled = true
      window.clearTimeout(timerId)
    }
  }, [open, section, locale, loadChecks])

  const runDriverAction = async (direct?: 'cpu') => {
    const action = direct || driverConfirm
    if (!action || driverSubmitting) return
    setDriverConfirm(null)
    setDriverSubmitting(true)
    try {
      const status = await api.updateNvidiaDriver(action)
      setDriverStatus(status)
      setInstallPopupError('')
      setChecksErr('')
      observedInstallError.current = ''
      setInstallProgressMinimized(false)
      if (status.running) {
        setInstalling(action === 'cpu' ? 'cpu_runtime' : 'nvidia_driver')
        setInstallProgress(1)
        setInstallMessage(action === 'cpu' ? t('Đang cài runtime CPU…', 'Installing the CPU runtime…') : t('Đang kiểm tra driver đồ họa…', 'Checking the graphics driver…'))
      }
    } catch (error) {
      const code = error instanceof Error ? error.message : 'DRIVER_UPDATE_FAILED'
      setInstallPopupError(runtimeErrorText({ running: false, errorCode: code }, localeRef.current))
    } finally {
      setDriverSubmitting(false)
    }
  }

  const cur = draft[tab]
  const canClose = !forceSetup || !!checks?.ok
  // Package failures live in install.log — always prefer that on the Log tab.
  const packageLogText = [installLog, installPopupError || checksErr].filter(Boolean).join('\n')
  const showPackageLog = Boolean(packageLogText) || forceSetup || !checks?.ok
  const visibleLog = showPackageLog ? packageLogText : diagnosticLog

  const mergeInstallLog = useCallback((...chunks: Array<string | undefined | null>) => {
    setInstallLog((previous) => {
      let next = previous
      for (const chunk of chunks) {
        const text = String(chunk || '').trim()
        if (!text) continue
        if (!next) next = text
        else if (!next.includes(text)) next = `${next}\n${text}`
      }
      return next
    })
  }, [])

  const restartApp = useCallback(async () => {
    if (restartRequested.current) return
    restartRequested.current = true
    setPendingRestart(false)
    pendingRestartRef.current = false
    setRestarting(true)
    setChecksErr('')
    try {
      await api.restartApp()
    } catch (e) {
      restartRequested.current = false
      setPendingRestart(true)
      pendingRestartRef.current = true
      setChecksErr(e instanceof Error ? e.message : localize(localeRef.current, 'Không khởi động lại được app', 'Could not restart the app'))
      autoSetupLock.current = false
    } finally {
      setRestarting(false)
    }
  }, [])

  const runInstallJob = useCallback(async (kind: InstallKind) => {
    installActiveRef.current = kind
    setInstalling(kind)
    setInstallProgressMinimized(false)
    observedInstallError.current = ''
    setInstallPopupError('')
    setInstallLog('')
    setInstallProgress(1)
    setMsg('')
    setInstallMessage(localize(localeRef.current, 'Đang chuẩn bị cài đặt…', 'Preparing installation…'))
    setChecksErr('')
    let lastInstallError = ''
    const onStatus = (status: InstallStatus) => {
      setDriverStatus(status)
      if (status.log || status.error || status.diagnostics) {
        mergeInstallLog(status.log, status.diagnostics, status.error)
      }
      if (typeof status.progress === 'number') setInstallProgress(status.progress)
      setInstallMessage(runtimeStatusText(status, localeRef.current))
      if (status.error) lastInstallError = runtimeErrorText(status, localeRef.current)
    }
    try {
      const result =
        kind === 'moss_tts_nano_runtime' ? await api.installResource('moss_tts_nano', onStatus)
        : kind === 'whisper' || kind === 'ocr' || kind === 'diarization' || kind === 'demucs'
          ? await api.installResource(kind, onStatus)
        : kind === 'vieneu' || kind === 'ai_runtime_vieneu'
          ? await api.installResource('vieneu', onStatus)
        : kind === 'ai_runtime' || kind === 'ai_runtime_ocr'
          ? await api.installAiRuntime(onStatus)
        : kind === 'ocr_cuda' ? await api.installOcrCuda(onStatus)
        : kind === 'demucs_cuda' ? await api.installDemucsCuda(onStatus)
        : kind === 'nvm' ? await api.installNvm(onStatus)
        : (() => { throw new Error(`Unknown install kind: ${kind}`) })()
      if (installCancelRef.current) throw new InstallCancelledError()
      const doneMsg = localize(localeRef.current, 'Cài đặt hoàn tất.', 'Installation complete.')
      setInstallProgress(100)
      setInstallMessage(doneMsg)
      setInstallLog((prev) => {
        const line = `✓ ${installLabel(kind, localeRef.current)} · ${doneMsg}`
        return prev ? `${prev}\n\n${line}` : line
      })
      setMsg(doneMsg)
      if (result.needsRestart) {
        setPendingRestart(true)
        pendingRestartRef.current = true
      }
      loadChecks(true, false)
      autoSetupLock.current = false
      await new Promise((r) => window.setTimeout(r, 800))
      return true
    } catch (e) {
      if (e instanceof InstallCancelledError || installCancelRef.current) {
        const cancelledMsg = localize(localeRef.current, 'Đã hủy cài đặt.', 'Installation cancelled.')
        setInstallMessage(cancelledMsg)
        setMsg(cancelledMsg)
        setInstallPopupError('')
        setChecksErr('')
        observedInstallError.current = ''
        autoSetupLock.current = false
        return false
      }
      const raw = e instanceof Error ? e.message : String(e)
      mergeInstallLog(raw)
      const message = lastInstallError || runtimeErrorText({ running: false, error: raw }, localeRef.current)
      setChecksErr(message)
      setInstallPopupError(message)
      observedInstallError.current = message
      // ponytail: dừng hàng đợi khi fail — tránh auto-retry vô tận.
      installQueueRef.current = []
      setInstallQueue([])
      return false
    } finally {
      installActiveRef.current = null
      setInstalling(null)
    }
  }, [loadChecks, mergeInstallLog])

  const drainInstallQueue = useCallback(async () => {
    while (!installCancelRef.current) {
      const next = installQueueRef.current[0]
      if (!next) break
      installQueueRef.current = installQueueRef.current.slice(1)
      setInstallQueue(installQueueRef.current)
      const ok = await runInstallJob(next)
      if (!ok) break
    }
    if (installCancelRef.current) {
      installQueueRef.current = []
      setInstallQueue([])
      installCancelRef.current = false
    }
  }, [runInstallJob])

  const installAction = useCallback(async (kind: InstallKind) => {
    if (installCancelRef.current) installCancelRef.current = false
    if (packageInstallBusy(kind, installActiveRef.current, installQueueRef.current)) return
    if (isExclusiveInstallKind(installActiveRef.current) || (isExclusiveInstallKind(kind) && installActiveRef.current)) {
      return
    }
    if (installActiveRef.current) {
      installQueueRef.current = [...installQueueRef.current, kind]
      setInstallQueue(installQueueRef.current)
      setInstallProgressMinimized(false)
      setInstallMessage(localize(
        localeRef.current,
        `Đã xếp hàng ${installLabel(kind, localeRef.current)} (còn ${installQueueRef.current.length})`,
        `Queued ${installLabel(kind, localeRef.current)} (${installQueueRef.current.length} waiting)`,
      ))
      return
    }
    const ok = await runInstallJob(kind)
    if (!ok) return
    await drainInstallQueue()
  }, [drainInstallQueue, runInstallJob])

  const cancelInstall = useCallback(async () => {
    installCancelRef.current = true
    installQueueRef.current = []
    setInstallQueue([])
    setInstallMessage(localize(localeRef.current, 'Đang hủy cài đặt…', 'Cancelling installation…'))
    try {
      await api.cancelInstall()
    } catch {
      /* pollInstall sẽ nhận INSTALL_CANCELLED khi worker dừng */
    }
  }, [])

  const rollbackAiRuntime = useCallback(async () => {
    setChecksErr('')
    try {
      const result = await api.rollbackAiRuntime()
      setInstallPopupError('')
      setMsg(localize(locale, 'Đã khôi phục runtime trước.', 'Previous runtime restored.'))
      if (result.needsRestart) setPendingRestart(true)
      await loadChecks(true, false)
    } catch (e) {
      setChecksErr(e instanceof Error ? e.message : localize(locale, 'Không thể khôi phục runtime.', 'Could not restore the runtime.'))
    }
  }, [loadChecks, locale])

  useEffect(() => {
    if (!open || section !== 'setup') return
    if (checksLoading || installBusy || restarting || !checks || !installStatusReady || driverWaiting || driverStatus.error || driverStatus.kind === 'nvidia_driver' || driverStatus.kind === 'cpu_runtime') return
    if (autoSetupLock.current) return
    const shouldAuto = forceSetup || !checks.ok
    if (!shouldAuto) return

    const run = async () => {
      if (forceSetup && checks.ok) {
        onSetupReady?.()
        return
      }
      const next = nextAutoInstall(checks)
      if (next) {
        // Skip if this kind was already auto-attempted — avoids infinite loop
        // when install succeeds but the check item remains !ok.
        if (autoAttempted.current.has(next)) return
        autoAttempted.current.add(next)
        autoSetupLock.current = true
        await installAction(next)
        return
      }
    }

    void run()
  }, [
    open,
    forceSetup,
    section,
    checks,
    checksLoading,
    installBusy,
    restarting,
    pendingRestart,
    driverStatus,
    driverWaiting,
    installStatusReady,
    installAction,
    onSetupReady,
  ])

  useEffect(() => {
    // Auto-relaunch after the last queued package finishes when runtime needs a restart.
    if (!pendingRestart || installing || installQueue.length || restarting) return
    void restartApp()
  }, [pendingRestart, installing, installQueue.length, restarting, restartApp])

  function tryClose() {
    if (!canClose) return
    onClose()
  }

  function toggleElSlotVisibility(index: number) {
    setElSlots((prev) =>
      prev.map((slot, i) => (i === index ? { ...slot, visible: !slot.visible } : slot))
    )
  }

  function setElSlot(index: number, value: string) {
    setElSlots((prev) =>
      prev.map((slot, i) => (i === index ? { ...slot, value, edited: true } : slot))
    )
    setElDirty(true)
  }

  function addElSlot() {
    setElSlots((prev) => [...prev, { savedIndex: null, value: '', visible: false, edited: false }])
    setElDirty(true)
  }

  function removeElSlot(index: number) {
    setElSlots((prev) => {
      const next = prev.filter((_, i) => i !== index)
      return next.length > 0 ? next : [{ savedIndex: null, value: '', visible: false, edited: false }]
    })
    setElDirty(true)
  }

  function toggleCloudKeyVisibility(index: number) {
    setCloudKeySlots((all) => ({
      ...all,
      [tab]: all[tab].map((slot, i) =>
        i === index ? { ...slot, visible: !slot.visible } : slot
      ),
    }))
  }

  function setCloudKeySlot(index: number, value: string) {
    setCloudKeySlots((all) => ({
      ...all,
      [tab]: all[tab].map((slot, i) => (i === index ? { ...slot, value, edited: true } : slot)),
    }))
    setCloudKeysDirty((all) => ({ ...all, [tab]: true }))
  }

  function addCloudKeySlot() {
    setCloudKeySlots((all) => ({
      ...all,
      [tab]: [...all[tab], { savedIndex: null, value: '', visible: false, edited: false }],
    }))
    setCloudKeysDirty((all) => ({ ...all, [tab]: true }))
  }

  function removeCloudKeySlot(index: number) {
    setCloudKeySlots((all) => {
      const next = all[tab].filter((_, i) => i !== index)
      return {
        ...all,
        [tab]: next.length > 0 ? next : [{ savedIndex: null, value: '', visible: false, edited: false }],
      }
    })
    setCloudKeysDirty((all) => ({ ...all, [tab]: true }))
  }

  async function onSave() {
    setSaving(true)
    setMsg('')
    try {
      const cloud: Record<string, { apiKey?: string; apiKeys?: string; keys?: string[]; baseUrl?: string; model?: string }> =
        {}
      for (const id of PROVIDERS) {
        const d = draft[id]
        let keysPayload: { keys?: string[]; apiKeys?: string } = {}
        if (cloudKeysDirty[id]) {
          const keysToSend: string[] = []
          for (const slot of cloudKeySlots[id]) {
            const val = slot.value.trim()
            if (val) {
              keysToSend.push(val)
            } else if (slot.savedIndex !== null && !slot.edited) {
              keysToSend.push(`__keep:${slot.savedIndex}__`)
            }
          }
          keysPayload = {
            keys: keysToSend,
            apiKeys: keysToSend.join(','),
          }
        }
        cloud[id] = {
          baseUrl: d.baseUrl,
          model: d.model,
          ...keysPayload,
        }
      }
      const body: {
        cloud: typeof cloud
        tts?: { elevenlabs: { apiKeys?: string; keys?: string[] } }
      } = { cloud }

      if (elDirty) {
        const keysToSend: string[] = []
        for (const slot of elSlots) {
          const val = slot.value.trim()
          if (val) {
            keysToSend.push(val)
          } else if (slot.savedIndex !== null && !slot.edited) {
            keysToSend.push(`__keep:${slot.savedIndex}__`)
          }
        }
        body.tts = {
          elevenlabs: {
            keys: keysToSend,
            apiKeys: keysToSend.join(','),
          },
        }
      }

      const cfg = await api.saveConfig(body)
      const next = emptyCloud()
      for (const id of PROVIDERS) {
        const c = cfg.cloud?.[id]
        if (!c) continue
        next[id] = {
          apiKey: c.apiKey || '',
          apiKeys: c.apiKeys || '',
          keyCount: c.keyCount || 0,
          baseUrl: c.baseUrl || next[id].baseUrl,
          model: c.model || next[id].model,
          apiKeySet: !!c.apiKeySet,
          label: c.label || next[id].label,
        }
      }
      setDraft(next)
      setRawCloudKeys(
        Object.fromEntries(
          PROVIDERS.map((id) => {
            const c = cfg.cloud?.[id]
            const keys = c?.rawKeys?.length
              ? c.rawKeys
              : (c?.apiKeys ? c.apiKeys.split(',').map((k) => k.trim()).filter(Boolean) : (c?.apiKey ? [c.apiKey] : []))
            return [id, keys]
          })
        ) as unknown as Record<CloudProviderId, string[]>
      )
      setCloudKeySlots(
        Object.fromEntries(
          PROVIDERS.map((id) => {
            const c = cfg.cloud?.[id]
            const count = c?.apiKeySet ? Math.max(1, Number(c.keyCount || 0)) : 0
            return [
              id,
              count > 0
                ? Array.from({ length: count }, (_, i) => ({ savedIndex: i, value: '', visible: false, edited: false }))
                : [{ savedIndex: null, value: '', visible: false, edited: false }],
            ]
          })
        ) as Record<CloudProviderId, ElKeySlot[]>
      )
      setCloudKeysDirty(Object.fromEntries(PROVIDERS.map((id) => [id, false])) as Record<CloudProviderId, boolean>)
      const el = cfg.tts?.elevenlabs
      const n = el?.apiKeySet ? Math.max(1, Number(el?.keyCount || 0)) : 0
      setElSavedCount(n)
      const elKeys = el?.rawKeys?.length
        ? el.rawKeys
        : (el?.apiKeys ? el.apiKeys.split(',').map((k) => k.trim()).filter(Boolean) : [])
      setRawElKeys(elKeys)
      if (n > 0) {
        setElSlots(Array.from({ length: n }, (_, i) => ({ savedIndex: i, value: '', visible: false, edited: false })))
      } else {
        setElSlots([{ savedIndex: null, value: '', visible: false, edited: false }])
      }
      setElDirty(false)
      const hasSavedKeys = Boolean(el?.apiKeySet)
      setMsg(
        elDirty && hasSavedKeys
          ? t('Đã lưu. Đang tải lại danh sách giọng…', 'Saved. Reloading voices…')
          : t('Đã lưu.', 'Saved.')
      )
      toast.success(t('Đã lưu cấu hình.', 'Settings saved.'))
      onSaved?.()
    } catch (e) {
      const err = e instanceof Error ? e.message : t('Lưu thất bại', 'Save failed')
      setMsg(err)
      toast.error(err)
    } finally {
      setSaving(false)
    }
  }

  const updateProgress = Math.max(0, Math.min(100, Math.round(Number(updateDialog?.progress) || 0)))

  if (!open) return null

  return createPortal(
    <div
      className="cfg-overlay"
      role="presentation"
    >
      <div
        className={`cfg-modal cfg-modal-wide${section === 'setup' ? ' cfg-modal-setup' : ''}`}
        role="dialog"
        aria-modal
        aria-label={t('Cấu hình', 'Settings')}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="cfg-head">
          <div>
            <h2>{t('Cấu hình', 'Settings')}</h2>
            <p>
              {installing
                ? t(`Đang cài ${packageName(installing)}…`, `Installing ${packageName(installing)}…`)
                : forceSetup && !checks?.ok
                  ? t('Cài đủ thành phần bắt buộc để bắt đầu', 'Install the required components to get started')
                  : t('Thiết lập hệ thống · Cloud AI · ElevenLabs', 'System settings · Cloud AI · ElevenLabs')}
            </p>
            {section === 'setup' && <p>{t('Chỉ FFmpeg và FFprobe là package bắt buộc. Các gói AI cài khi cần dùng tính năng tương ứng.', 'Only FFmpeg and FFprobe are required packages. Install AI packages when you need the corresponding feature.')}</p>}
          </div>
          <div className="cfg-head-actions">
            <LocaleSwitch
              locale={locale}
              onChange={setLocale}
              ariaLabel={t('Ngôn ngữ giao diện', 'Interface language')}
            />
            <button type="button" className="cfg-update" disabled={updateChecking} onClick={() => void checkForUpdate()}>
              {updateChecking ? t('Đang kiểm tra…', 'Checking…') : t('Kiểm tra cập nhật', 'Check for updates')}
            </button>
            {canClose ? (
              <button type="button" className="cfg-close" onClick={tryClose} aria-label={t('Đóng', 'Close')}>
                ×
              </button>
            ) : null}
          </div>
        </header>

        <div className="cfg-section-tabs">
          <button
            type="button"
            className={section === 'setup' ? 'active' : undefined}
            onClick={() => setSection('setup')}
          >
            {t('Thiết lập', 'Settings')}
            {checks && !checks.ok ? (
              <span className="cfg-dot cfg-dot-warn" title="Thiếu dependency" />
            ) : checks?.ok ? (
              <span className="cfg-dot" title={t('Sẵn sàng', 'Ready')} />
            ) : null}
          </button>
          {!forceSetup ? (
            <>
              <button
                type="button"
                className={section === 'cloud' ? 'active' : undefined}
                onClick={() => setSection('cloud')}
              >
                {t('Cloud AI', 'Cloud AI')}
              </button>
              <button
                type="button"
                className={section === 'tts' ? 'active' : undefined}
                onClick={() => setSection('tts')}
              >
                ElevenLabs
                {elSavedCount > 0 ? <span className="cfg-dot" title="Đã có key" /> : null}
              </button>
              <button
                type="button"
                className={section === 'output' ? 'active' : undefined}
                onClick={() => setSection('output')}
              >
                {t('Thư mục đầu ra', 'Output folder')}
              </button>
              <button type="button" className={section === 'cleanup' ? 'active' : undefined} onClick={() => setSection('cleanup')}>
                {t('Dọn dẹp', 'Cleanup')}
              </button>
              <button
                type="button"
                className={section === 'license' ? 'active' : undefined}
                onClick={() => setSection('license')}
              >
                {t('Quyền sử dụng', 'Access')}
              </button>
            </>
          ) : null}
          <button
            type="button"
            className={section === 'logs' ? 'active' : undefined}
            onClick={() => setSection('logs')}
          >
            {licenseStatus?.valid ? t('Log', 'Log') : t('Log cài đặt', 'Install log')}
          </button>
          <button
            type="button"
            className={section === 'computer' ? 'active' : undefined}
            onClick={() => setSection('computer')}
          >
            {t('Thông tin máy tính', 'Computer information')}
          </button>
        </div>

        {section === 'cloud' && (
          <div className="cfg-tabs">
            {PROVIDERS.map((id) => (
              <button
                key={id}
                type="button"
                className={tab === id ? 'active' : undefined}
                onClick={() => setTab(id)}
              >
                {draft[id].label}
                {draft[id].apiKeySet ? <span className="cfg-dot" title="Đã có key" /> : null}
              </button>
            ))}
          </div>
        )}

        {loading && section !== 'setup' && section !== 'logs' && section !== 'computer' ? (
          <p className="cfg-msg">Đang tải…</p>
        ) : section === 'setup' ? (
          <div className="cfg-body cfg-setup">
            <div className="cfg-setup-bar">
              <div className="cfg-setup-info">
                <strong>
                  {installing
                    ? t(`Đang cài ${packageName(installing)}…`, `Installing ${packageName(installing)}…`)
                    : checksLoading ? t('Đang kiểm tra…', 'Checking…') : checks?.ok ? t('Đã sẵn sàng', 'Ready') : t('Cần cài thành phần bắt buộc', 'Required components need installation')}
                </strong>
                {checks ? (
                  <span className="cfg-setup-meta">
                    {checks.platform}
                    {checks.device?.accel
                      ? ` · ${String(checks.device.accel).toUpperCase()}`
                      : ''}
                    {checks.device?.gpuName ? ` · ${checks.device.gpuName}` : ''}
                    {checks.device?.vramMb ? ` · ${checks.device.vramMb} MB` : ''}
                  </span>
                ) : null}
              </div>
              <div className="cfg-setup-actions">
                {(checks?.device?.install?.actions?.length ?? 0) > 0
                  ? checks?.device?.install?.actions!.map((a) => {
                    const actionLabel = a.id === 'demucs_cuda'
                      ? t('Cài Demucs', 'Install Demucs') : t('Cài OCR GPU', 'Install OCR GPU')
                    const done = (checks?.items || []).some(
                      (it) =>
                        it.ok &&
                        (it.install === a.id ||
                          (a.id === 'demucs_cuda' && it.id === 'demucs') ||
                          (a.id === 'ocr_cuda' && it.id === 'ocr_cuda')),
                    )
                    return done ? (
                      <span key={a.id} className="cfg-check-installed cfg-setup-chip">
                        {actionLabel} ✓
                      </span>
                    ) : (
                      <button
                        key={a.id}
                        type="button"
                        className="cfg-check-install cfg-check-install-sm"
                        title={actionLabel}
                        aria-label={actionLabel}
                        disabled={exclusiveInstall || packageInstallBusy(a.id, installing, installQueue)}
                        onClick={() => {
                          autoSetupLock.current = false
                          autoAttempted.current.clear()
                          void installAction(a.id as 'ocr_cuda' | 'demucs_cuda')
                        }}
                      >
                        {installing === a.id
                          ? t('Đang cài…', 'Installing…')
                          : installQueue.includes(a.id as InstallKind)
                            ? t('Đã xếp hàng', 'Queued')
                            : t('Cài đặt', 'Install')}
                      </button>
                    )
                  })
                  : null}
                <button
                  type="button"
                  className="cfg-secondary cfg-setup-refresh"
                  disabled={checksLoading || exclusiveInstall}
                  onClick={() => { autoAttempted.current.clear(); loadChecks(true, false) }}
                >
                  {checksLoading ? '…' : t('Kiểm tra lại', 'Check again')}
                </button>
              </div>
            </div>
            {checksErr ? (
              <div className="cfg-msg cfg-msg-err">
                <p>{checksErr}</p>
                {installPopupError ? (
                  <button type="button" className="cfg-secondary" disabled={installBusy} onClick={() => void rollbackAiRuntime()}>
                    {t('Khôi phục runtime trước', 'Restore previous runtime')}
                  </button>
                ) : null}
              </div>
            ) : null}
            {pendingRestart ? (
              <p className="cfg-msg cfg-msg-restart">
                {restarting
                  ? t('Đang khởi động lại ứng dụng…', 'Restarting the application…')
                  : installBusy
                    ? t('Sẽ tự khởi động lại sau khi cài xong hàng đợi.', 'Will restart automatically after the install queue finishes.')
                    : t('Đã cài xong. Đang khởi động lại ứng dụng…', 'Installation complete. Restarting the application…')}
              </p>
            ) : null}
            {msg && !installBusy && !installPopupError && section === 'setup' ? <p className="cfg-msg">{msg}</p> : null}
            <ul className="cfg-check-list">
              {(checks?.items || []).filter((it) => it.id !== 'device' && it.id !== 'httpx').map((it) => (
                <li
                  key={it.id}
                  className={`cfg-check-item ${it.ok ? 'ok' : it.required ? 'bad' : 'warn'}`}
                >
                  <div className="cfg-check-top">
                    <div className="cfg-check-title-group">
                      <span className="cfg-check-status" aria-hidden>
                        {it.ok ? '✓' : it.required ? '!' : '·'}
                      </span>
                      <div className="cfg-check-name">
                        {setupLabel(locale, it.id, 'name')}
                        {it.required ? (
                          <em className="cfg-req">{t('bắt buộc', 'required')}</em>
                        ) : (
                          <em className="cfg-opt">{t('tuỳ chọn', 'optional')}</em>
                        )}
                      </div>
                    </div>
                    <div className="cfg-check-action">
                      {it.ok ? (
                        <span className="cfg-check-installed">{t('Sẵn sàng', 'Ready')}</span>
                      ) : ['ai_runtime', 'ai_runtime_ocr', 'ai_runtime_vieneu', 'whisper', 'ocr', 'diarization', 'vieneu', 'demucs', 'moss_tts_nano_runtime', 'ocr_cuda', 'demucs_cuda', 'nvm'].includes(it.install) ? (
                        <button
                          type="button"
                          className="cfg-check-install"
                          disabled={exclusiveInstall || packageInstallBusy(it.install, installing, installQueue)}
                          onClick={() => {
                            autoSetupLock.current = false
                            autoAttempted.current.clear()
                            const kind = (
                              it.id === 'ai_runtime_vieneu' ? 'ai_runtime_vieneu'
                              : it.install.startsWith('ai_runtime') ? 'ai_runtime'
                              : it.install
                            ) as InstallKind
                            void installAction(kind)
                          }}
                        >
                          {packageInstallBusy(it.install, installing, installQueue)
                            ? (installing === it.install
                              || (it.install.startsWith('ai_runtime') && installing === 'ai_runtime')
                              || (it.install === 'whisper' && installing === 'whisper')
                                ? t('Đang cài…', 'Installing…')
                                : t('Đã xếp hàng', 'Queued'))
                            : t('Cài đặt', 'Install')}

                        </button>
                      ) : it.install && it.install.startsWith('http') ? (
                        <a
                          className="cfg-check-link"
                          href={it.install}
                          target="_blank"
                          rel="noreferrer"
                          title={t('Tải từ trang chính thức', 'Download from official site')}
                        >
                          {t('Tải', 'Download')}
                        </a>
                      ) : null}
                    </div>
                  </div>
                  <div className="cfg-check-body">
                    {!it.ok && <div className="cfg-check-detail">{t('Cần kiểm tra hoặc cài đặt', 'Needs checking or installation')}</div>}
                    {it.detail && <details><summary>{t('Chi tiết kỹ thuật', 'Technical details')}</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{setupDetail(locale, it)}</pre></details>}
                    {!it.ok && <div className="cfg-check-hint">{setupLabel(locale, it.id, 'hint')}</div>}
                    {!it.ok && it.install && !it.install.startsWith('http') && !['ai_runtime', 'ai_runtime_ocr', 'ai_runtime_vieneu', 'whisper', 'ocr', 'diarization', 'vieneu', 'demucs', 'moss_tts_nano_runtime', 'ocr_cuda', 'demucs_cuda', 'nvm'].includes(it.install) ? (
                      <code className="cfg-check-cmd" title={t('Lệnh kỹ thuật', 'Technical command')}>
                        {it.install}
                      </code>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ) : section === 'cloud' ? (
          <div className="cfg-body">
            <div className="cfg-el-grid">
              {cloudKeySlots[tab].map((slot, index) => {
                const isSaved = slot.savedIndex !== null && !slot.edited
                const rawKey = slot.savedIndex !== null ? (rawCloudKeys[tab]?.[slot.savedIndex] || '') : ''
                const displayValue = slot.edited
                  ? slot.value
                  : slot.visible && isSaved
                    ? rawKey
                    : slot.value
                const placeholder = isSaved && !slot.visible
                  ? t('••••••••  — nhập để thay', '•••••••• — enter to replace')
                  : providerKeyPlaceholder(tab)

                return (
                  <div className="cfg-el-row" key={`${tab}-${index}`}>
                    <label>
                      <span>
                        {t('Key', 'Key')} {index + 1}
                        {isSaved ? t(' (đã lưu)', ' (saved)') : ''}
                      </span>
                      <div className="cfg-el-input-wrap">
                        <input
                          type={slot.visible ? 'text' : 'password'}
                          autoComplete="off"
                          placeholder={placeholder}
                          value={displayValue}
                          onChange={(e) => setCloudKeySlot(index, e.target.value)}
                        />
                        <button
                          type="button"
                          className="cfg-el-eye cfg-el-visibility"
                          onClick={() => toggleCloudKeyVisibility(index)}
                          title={slot.visible ? t('Ẩn key', 'Hide key') : t('Xem full key', 'Show full key')}
                          aria-label={
                            slot.visible
                              ? t(`Ẩn API key ${index + 1}`, `Hide API key ${index + 1}`)
                              : t(`Xem full API key ${index + 1}`, `Show full API key ${index + 1}`)
                          }
                        >
                          {slot.visible ? <IconEyeOff size={15} /> : <IconEye size={15} />}
                        </button>
                      </div>
                    </label>
                    <button
                      type="button"
                      className="cfg-el-remove"
                      disabled={cloudKeySlots[tab].length <= 1 && !slot.value && slot.savedIndex === null}
                      aria-label={t(`Xóa API key ${index + 1}`, `Remove API key ${index + 1}`)}
                      title={t('Xóa ô', 'Remove slot')}
                      onClick={() => removeCloudKeySlot(index)}
                    >
                      ×
                    </button>
                  </div>
                )
              })}
            </div>
            <button type="button" className="cfg-el-add" onClick={addCloudKeySlot}>
              {t('+ Thêm key', '+ Add key')}
            </button>
            {tab === 'opencode' && <p className="cfg-hint">
              {t('Lấy API key tại ', 'Get an API key from ')}
              <a href="https://opencode.ai/auth" target="_blank" rel="noreferrer">OpenCode</a>.
              {' '}{t('Danh sách chỉ gồm model dùng Chat Completions. Quyền dùng model free phụ thuộc chính sách OpenCode và tài khoản của bạn.', 'Only Chat Completions models are listed. Free model access depends on OpenCode policy and your account.')}
            </p>}

            <div className="cfg-cloud-panels">
              <section className="cfg-cloud-panel">
                <h3>{t('API Dịch', 'Translation API')}</h3>
                <label>
                  <span>{t('Base URL', 'Base URL')}</span>
                  <input
                    type="text"
                    value={cur.baseUrl}
                    onChange={(e) => setDraft((d) => ({ ...d, [tab]: { ...d[tab], baseUrl: e.target.value } }))}
                  />
                </label>
                <label className="cfg-cloud-model">
                  <span>Model</span>
                  {(() => {
                    const presets = PROVIDER_PRESET_MODELS[tab] || []
                    const liveModels = discoveredModels[tab] || []
                    const modelOptions: DiscoveredModel[] = liveModels.length
                      ? liveModels
                      : presets.map((m) => ({ id: m.id, label: localize(locale, m.labelVi, m.labelEn) }))
                    const isPreset = modelOptions.some((m) => m.id === cur.model)
                    const isCustom = (!isPreset && !cur.model) || !!customModelTabs[tab]
                    return (
                      <>
                        <select
                          className="cfg-cloud-model-select"
                          value={isCustom ? '__custom__' : cur.model}
                          onChange={(e) => {
                            const val = e.target.value
                            if (val === '__custom__') {
                              setCustomModelTabs((prev) => ({ ...prev, [tab]: true }))
                            } else {
                              setCustomModelTabs((prev) => ({ ...prev, [tab]: false }))
                              setDraft((d) => ({ ...d, [tab]: { ...d[tab], model: val } }))
                            }
                          }}
                        >
                          {liveModels.length ? (
                            <>
                              <optgroup label={t('Model free dùng được', 'Free models available')}>
                                {modelOptions.filter((m) => m.free !== false).map((m) => (
                                  <option key={m.id} value={m.id}>{m.label || m.id}</option>
                                ))}
                              </optgroup>
                              <optgroup label={t('Model cần credit', 'Models requiring credit')}>
                                {modelOptions.filter((m) => m.free === false).map((m) => (
                                  <option key={m.id} value={m.id}>{m.label || m.id} — {t('cần credit', 'credit required')}</option>
                                ))}
                              </optgroup>
                            </>
                          ) : modelOptions.map((m) => (
                            <option key={m.id} value={m.id}>{m.label || m.id}</option>
                          ))}
                          {!isPreset && cur.model ? (
                            <option value={cur.model}>
                              {cur.model} ({t('Hiện tại', 'Current')})
                            </option>
                          ) : null}
                          <option value="__custom__">
                            {t('Tùy chỉnh khác… (tự nhập)', 'Custom model… (enter manually)')}
                          </option>
                        </select>
                        {isCustom ? (
                          <input
                            type="text"
                            className="cfg-cloud-model-custom"
                            placeholder={t('Nhập tên model tùy chỉnh…', 'Enter custom model name…')}
                            value={cur.model}
                            onChange={(e) => setDraft((d) => ({ ...d, [tab]: { ...d[tab], model: e.target.value } }))}
                            autoFocus={customModelTabs[tab]}
                          />
                        ) : null}
                      </>
                    )
                  })()}
                </label>
                {modelsLoading && <p className="cfg-hint">{t('Đang tải model thật…', 'Loading live models…')}</p>}
                {!modelsLoading && discoveredModels[tab]?.length ? (
                  <p className="cfg-hint">{t('Danh sách lấy trực tiếp từ provider; model không free được ghi “cần credit”.', 'Live provider catalogue; non-free models are marked “credit required”.')}</p>
                ) : null}
              </section>
            </div>
            <p className="cfg-hint">
              {t(
                'Nhiều key sẽ tự động luân phiên khi dịch. Để trống ô đã lưu = giữ nguyên; gõ key mới = thay / thêm.',
                'Multiple keys rotate automatically during translation. Leave a saved field empty to keep it; enter a new key to replace or add one.',
              )}
            </p>
            <p className="cfg-hint">
              {t('API key, Base URL và model dịch được lưu tại ', 'The API key, base URL, and translation model are stored in ')}
              <code>backend/data/app_config.json</code>.
            </p>
          </div>
        ) : section === 'tts' ? (
          <div className="cfg-body">
            <div className="cfg-el-grid">
              {elSlots.map((slot, i) => {
                const isSaved = slot.savedIndex !== null && !slot.edited
                const rawKey = slot.savedIndex !== null ? (rawElKeys[slot.savedIndex] || '') : ''
                const displayValue = slot.edited
                  ? slot.value
                  : slot.visible && isSaved
                    ? rawKey
                    : slot.value
                const placeholder = isSaved && !slot.visible
                  ? t('••••••••  — nhập để thay', '•••••••• — enter to replace')
                  : 'sk_…'

                return (
                  <div key={i} className="cfg-el-row">
                    <label>
                      <span>
                        {t('Key', 'Key')} {i + 1}
                        {isSaved ? t(' (đã lưu)', ' (saved)') : ''}
                      </span>
                      <div className="cfg-el-input-wrap">
                        <input
                          type={slot.visible ? 'text' : 'password'}
                          autoComplete="off"
                          placeholder={placeholder}
                          value={displayValue}
                          onChange={(e) => setElSlot(i, e.target.value)}
                        />
                        <button
                          type="button"
                          className="cfg-el-eye cfg-el-visibility"
                          onClick={() => toggleElSlotVisibility(i)}
                          title={slot.visible ? t('Ẩn key', 'Hide key') : t('Xem full key', 'Show full key')}
                          aria-label={
                            slot.visible
                              ? t(`Ẩn key ${i + 1}`, `Hide key ${i + 1}`)
                              : t(`Xem full key ${i + 1}`, `Show full key ${i + 1}`)
                          }
                        >
                          {slot.visible ? <IconEyeOff size={15} /> : <IconEye size={15} />}
                        </button>
                      </div>
                    </label>
                    <button
                      type="button"
                      className="cfg-el-remove"
                      onClick={() => removeElSlot(i)}
                      disabled={elSlots.length <= 1 && !slot.value && slot.savedIndex === null}
                      title={t('Xóa ô', 'Remove slot')}
                      aria-label={t(`Xóa key ${i + 1}`, `Remove key ${i + 1}`)}
                    >
                      ×
                    </button>
                  </div>
                )
              })}
            </div>
            <button type="button" className="cfg-el-add" onClick={addElSlot}>
              {t('+ Thêm key', '+ Add key')}
            </button>
            <p className="cfg-hint">
              {t(
                'Giọng ElevenLabs ở sidebar. Nhiều key → xoay khi 401/429. Để trống ô đã lưu = giữ nguyên; gõ key mới = thay / thêm.',
                'ElevenLabs voices are available in the sidebar. Multiple keys rotate after 401/429. Leave a saved field empty to keep it; enter a new key to replace or add one.',
              )}
            </p>
          </div>
        ) : section === 'output' ? (
          <div className="cfg-body cfg-output-section">
            <p className="cfg-hint">
              {t(
                'Tất cả tính năng xuất file sẽ lưu vào thư mục này theo cấu trúc con. Để trống = dùng mặc định theo nền tảng.',
                'All export features save files here in subfolders. Leave empty to use the platform default.',
              )}
            </p>
            <div className="cfg-output-field-row">
              <input
                id="cfg-output-root-input"
                className="cfg-output-field-input"
                type="text"
                value={outputRoot}
                placeholder={outputRootDefault || t('Đường dẫn tuyệt đối…', 'Absolute path…')}
                onChange={(e) => { setOutputRoot(e.target.value) }}
                spellCheck={false}
              />
              <button
                id="cfg-output-root-pick"
                type="button"
                className="cfg-secondary"
                title={t('Chọn thư mục', 'Browse')}
                onClick={async () => {
                  try {
                    const res = await fetch('/api/system/pick-folder', { method: 'POST' })
                    if (!res.ok) return
                    const data = await res.json() as { ok: boolean; path: string }
                    if (data.ok && data.path) setOutputRoot(data.path)
                  } catch { /* cancelled */ }
                }}
              >
                {t('Chọn', 'Browse')}
              </button>
              <button
                id="cfg-output-root-save"
                type="button"
                className="cfg-primary"
                disabled={outputRootSaving}
                onClick={async () => {
                  const raw = outputRoot.trim()
                  if (raw === savedOutputRoot.current) return
                  if (raw && !raw.match(/^([A-Za-z]:[/\\]|\/)/)) {
                    toast.error(t('Phải là đường dẫn tuyệt đối', 'Must be an absolute path'))
                    return
                  }
                  setOutputRootSaving(true)
                  try {
                    await api.saveOutputRoot(raw)
                    savedOutputRoot.current = raw
                    const cfg = await api.getConfig()
                    setOutputRootDefault((cfg as unknown as { desktopOutputRoot?: string }).desktopOutputRoot || '')
                    toast.success(t('Đã lưu', 'Saved'))
                  } catch {
                    toast.error(t('Lỗi lưu thư mục đầu ra', 'Failed to save'))
                  } finally {
                    setOutputRootSaving(false)
                  }
                }}
              >
                {outputRootSaving ? '…' : t('Lưu', 'Save')}
              </button>
              {outputRoot ? (
                <button
                  type="button"
                  className="cfg-secondary"
                  disabled={outputRootSaving}
                  title={t('Đặt lại mặc định', 'Reset to default')}
                  onClick={async () => {
                    if (savedOutputRoot.current === '') { setOutputRoot(''); return }
                    setOutputRootSaving(true)
                    try {
                      await api.saveOutputRoot('')
                      savedOutputRoot.current = ''
                      setOutputRoot('')
                      const cfg = await api.getConfig()
                      setOutputRootDefault((cfg as unknown as { desktopOutputRoot?: string }).desktopOutputRoot || '')
                      toast.success(t('Đã đặt lại mặc định', 'Reset to default'))
                    } catch {
                      toast.error(t('Lỗi', 'Error'))
                    } finally {
                      setOutputRootSaving(false)
                    }
                  }}
                >↺</button>
              ) : null}
            </div>

            {outputRootDefault ? (() => {
              const sep = outputRootDefault.includes('\\') ? '\\' : '/'
              const p = (sub: string) => outputRootDefault.replace(/[\\/]+$/, '') + sep + sub.replace(/\//g, sep)
              const rows: [string, string][] = [
                ['Video Clone',   p('clone')],
                ['Film Review',   p('review')],
                ['Flow', p('flow')],
                ['TTS',           p('text-to-speech')],
                [t('Tải video','Download video'), p('download-video')],
                [t('Xuất SRT','SRT export'), p('subtitles' + sep + 'export')],
                [t('SRT Image / ghép media','SRT Image / media compose'), p('media-compose')],
                [t('Vẽ tay','Drawing'),       p('drawing')],
                [t('Làm sạch video','Video cleaner'),       p('cleaner')],
                ['Batch',         p('batch')],
                ['Automation',    p('automation')],
              ]
              return (
                <div className="cfg-output-subfolders">
                  <p className="cfg-output-subfolders-title">{t('Thư mục theo tính năng', 'Folders by feature')}</p>
                  <div className="cfg-subfolder-grid">
                    {rows.map(([label, full]) => (
                      <div key={label} className="cfg-subfolder-item" title={full}>
                        <span className="cfg-subfolder-label">{label}</span>
                        {label === 'Flow' ? (
                          <ul className="cfg-subfolder-children">
                            {[[t('Video', 'Video'), 'video'], [t('Ảnh', 'Images'), 'image'], ['Series', 'series']].map(([name, folder]) => (
                              <li key={folder} title={p(`flow/${folder}`)}>
                                <span>{name}</span>
                                <code className="cfg-subfolder-path">{p(`flow/${folder}`)}</code>
                              </li>
                            ))}
                          </ul>
                        ) : (
                        <code className="cfg-subfolder-path">{full}</code>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )
            })() : null}
          </div>
        ) : section === 'cleanup' ? (
          <div className="cfg-body">
            <h3>{t('Dọn dẹp dữ liệu', 'Clean up data')}</h3>
            <label className="cfg-cleanup-all"><input type="checkbox" checked={cleanupAvailable.length > 0 && cleanupSelected.length === cleanupAvailable.length} onChange={event => setCleanupSelected(event.target.checked ? cleanupAvailable : [])} /> {t('Chọn tất cả', 'Select all')}</label>
<div className="cfg-cleanup-tabs" role="tablist">
              <button type="button" role="tab" aria-selected={cleanupView === 'selectable'} className={cleanupView === 'selectable' ? 'active' : undefined} onClick={() => setCleanupView('selectable')}>{t('Có thể dọn', 'Can be cleaned')} <small>{cleanupAvailable.length}</small></button>
              <button type="button" role="tab" aria-selected={cleanupView === 'protected'} className={cleanupView === 'protected' ? 'active' : undefined} onClick={() => setCleanupView('protected')}>{t('Được giữ lại', 'Kept safe')} <small>{Object.keys(cleanupTargets).length - cleanupAvailable.length}</small></button>
            </div>
            <div className="cfg-cleanup-list">{(cleanupView === 'selectable' ? cleanupAvailable : Object.keys(cleanupTargets).filter(key => !cleanupAvailable.includes(key))).map(key => {
              const info = cleanupTargets[key]
              return <label key={key} className="cfg-cleanup-item" title={info.path}>
                <input type="checkbox" disabled={cleanupView !== 'selectable' || cleanupRunning} checked={cleanupSelected.includes(key)} onChange={event => setCleanupSelected(current => event.target.checked ? [...current, key] : current.filter(item => item !== key))} />
                <span>{cleanupLabels[key] ? t(...cleanupLabels[key]) : key.startsWith('output:') ? `${t('Đầu ra', 'Output')} · ${key.slice(7)}` : (info.path || key).split(/[\\/]/).slice(-2).join('/')}{cleanupView === 'protected' && <em> · {t('Được giữ lại', 'Protected')}</em>}<small className="cfg-cleanup-path">{info.path || t('Backend chưa cung cấp đường dẫn — khởi động lại backend để cập nhật.', 'The backend has not provided a path — restart it to update.')}</small></span>
                <small>{info.unreadable ? t('Không đọc được', 'Unreadable') : `${info.items} ${t('file', 'files')} · ${info.bytes < 1024 * 1024 ? `${(info.bytes / 1024).toFixed(1)} KB` : `${(info.bytes / 1024 / 1024).toFixed(1)} MB`}`}</small>
              </label>
            })}</div>
            <button type="button" className="cfg-primary" disabled={!cleanupSelected.length || cleanupRunning} onClick={() => setCleanupConfirm(true)}>{cleanupRunning ? t('Đang dọn…', 'Cleaning…') : t('Dọn phần đã chọn', 'Clean selected')}</button>
          </div>
        ) : section === 'license' ? (
          licenseStatus && onLicenseStatusChange ? <LicensePage status={licenseStatus} embedded accessRequired={taskAccessRequired} onStatusChange={onLicenseStatusChange} /> : null
        ) : section === 'computer' ? (
          <SystemInfoPanel />
        ) : section === 'logs' ? (
          <div className="cfg-log-panel">
            <h3>{showPackageLog ? t('Log cài đặt package', 'Package installation log') : t('Log lỗi hệ thống', 'System error log')}</h3>
            <pre className="cfg-log-pre" tabIndex={0}>
              {diagnosticLogLoading && !showPackageLog
                ? t('Đang tải log lỗi…', 'Loading error log…')
                : visibleLog || (showPackageLog ? t('Chưa có log cài đặt.', 'No installation log yet.') : t('Chưa có lỗi được ghi nhận.', 'No recorded errors.'))}
            </pre>
            <div className="flex flex-wrap gap-2">
              <button type="button" className="cfg-secondary" disabled={!visibleLog}
                onClick={() => void copyText(visibleLog)}>
                {showPackageLog ? t('Sao chép log cài đặt', 'Copy installation log') : t('Sao chép log lỗi', 'Copy error log')}
              </button>
              <button type="button" className="cfg-secondary" disabled={!visibleLog}
                onClick={async () => {
                  try {
                    if (showPackageLog) {
                      await api.clearInstallLog()
                      setInstallLog('')
                      setInstallPopupError('')
                      setChecksErr('')
                    } else {
                      await api.clearAppLogs()
                      setDiagnosticLog('')
                    }
                    toast.success(t('Đã xoá log', 'Log cleared'))
                  } catch {
                    toast.error(t('Không thể xoá log', 'Could not clear the log'))
                  }
                }}>
                {t('Xoá log', 'Clear log')}
              </button>
            </div>
            <p className="cfg-hint">
              {showPackageLog
                ? t('Log tải/cài package AI và traceback khi cài thất bại (install.log).', 'AI package download/install log and failure traceback (install.log).')
                : t('Chỉ giữ lỗi có ích từ toàn bộ source; log khởi động và warm model được bỏ qua.', 'Only actionable errors from the whole source are kept; startup and model-warm noise is omitted.')}
            </p>
          </div>
        ) : null}

        {msg && section !== 'logs' && section !== 'setup' && section !== 'computer' ? <p className="cfg-msg">{msg}</p> : null}

        <footer className="cfg-foot">
          {section === 'setup' ? (
            <>
              {canClose ? (
                <button type="button" className="cfg-secondary" onClick={tryClose}>
                  Đóng
                </button>
              ) : (
                <span className="cfg-foot-note">Ứng dụng đang tự chuẩn bị các thành phần cần thiết</span>
              )}
              {pendingRestart && !restarting ? (
                <button
                  type="button"
                  className="cfg-secondary cfg-restart-btn"
                  disabled={installBusy}
                  onClick={() => void restartApp()}
                >
                  {t('Khởi động lại ngay', 'Restart now')}
                </button>
              ) : null}
              <button
                type="button"
                className="cfg-primary"
                disabled={checksLoading || exclusiveInstall || restarting}
                onClick={() => {
                  if (checks?.ok) onSetupReady?.()
                  else loadChecks(true, false)
                }}
              >
                {checks?.ok ? t('Bắt đầu', 'Start') : checksLoading ? t('Đang chuẩn bị…', 'Preparing…') : t('Thử lại', 'Retry')}
              </button>
            </>
          ) : section === 'logs' || section === 'computer' || section === 'license' || section === 'output' || section === 'cleanup' ? (
            <button type="button" className="cfg-secondary" onClick={tryClose} disabled={!canClose}>
              {t('Đóng', 'Close')}
            </button>
          ) : (
            <>
              <button type="button" className="cfg-secondary" onClick={tryClose} disabled={!canClose}>
                Đóng
              </button>
              <button
                type="button"
                className="cfg-primary"
                disabled={saving || loading}
                onClick={onSave}
              >
                {saving ? 'Đang lưu…' : 'Lưu'}
              </button>
            </>
          )}
        </footer>
      </div>
      <div onClick={(e) => e.stopPropagation()}>
        <ProgressPopup
          wide={gpuChoice || driverStatus.kind === 'cpu_runtime' || driverStatus.kind === 'nvidia_driver'}
          collapseLog={gpuChoice || driverStatus.kind === 'cpu_runtime' || driverStatus.kind === 'nvidia_driver'}
          active={Boolean(installBusy || installPopupError || driverWaiting)}
          minimized={installProgressMinimized}
          running={Boolean(installing)}
          title={
            gpuChoice && !installing
              ? driverStatus.kind === 'cpu_runtime'
                ? t('Cài runtime CPU thất bại', 'CPU runtime setup failed')
                : driverStatus.errorCode === 'DRIVER_TOO_OLD'
                ? t('Driver GPU quá cũ', 'GPU driver is outdated')
                : t('Lỗi cài package GPU', 'GPU package setup failed')
              : driverWaiting
              ? t('Cập nhật driver đồ họa', 'Graphics driver update')
              : installPopupError
              ? t('Cài đặt thất bại', 'Installation failed')
              : installing === 'rollback'
                ? t('Khôi phục runtime trước', 'Restoring previous runtime')
              : installing === 'nvidia_driver'
                ? t('Đang cập nhật driver đồ họa', 'Updating the graphics driver')
              : installing
                ? (installQueue.length
                  ? t(
                    `Đang cài ${packageName(installing)} · còn ${installQueue.length}`,
                    `Installing ${packageName(installing)} · ${installQueue.length} queued`,
                  )
                  : t(`Đang cài ${packageName(installing)}`, `Installing ${packageName(installing)}`))
                : t('Cài đặt', 'Install')
          }
          message={
            driverWaiting
              ? runtimeStatusText(driverStatus, locale)
              : installing
              ? installMessage || t(
                `Đang cài ${packageName(installing)}. Vui lòng không tắt ứng dụng.`,
                `Installing ${packageName(installing)}. Please keep the app open.`,
              )
              : installPopupError || undefined
          }
          progress={installProgress}
          error={installPopupError || null}
          diskSpace={installPopupError ? driverStatus : undefined}
          log={installLog || undefined}
          onCancel={installing && !exclusiveInstall && !driverWaiting ? () => void cancelInstall() : undefined}
          details={<>
            {gpuChoice && !installing && driverStatus.kind !== 'cpu_runtime' && <p className="mb-3 text-sm text-muted-foreground">
              {driverStatus.errorCode === 'DRIVER_TOO_OLD'
                ? t('Driver hiện tại thấp hơn phiên bản tối thiểu. Cập nhật driver hoặc chọn chạy toàn bộ bằng CPU.', 'The driver is below the minimum version. Update it or run everything on the CPU.')
                : t('Chưa có bằng chứng driver quá cũ. Xem log, thử lại hoặc chuyển sang CPU. Lỗi mạng hay dung lượng ổ đĩa vẫn cần được xử lý khi dùng CPU.', 'There is no evidence that the driver is outdated. Check the log, retry, or switch to CPU. Network or disk-space errors still need to be resolved in CPU mode.')}
            </p>}
            {gpuChoice && <div className="mb-4 grid grid-cols-1 min-[600px]:grid-cols-2 gap-3">
              {driverStatus.kind !== 'cpu_runtime' && <div>
                {driverStatus.driverRepairAvailable ? <>
                <button type="button" disabled={Boolean(installing || driverSubmitting || !driverStatus.driverRepairAvailable)}
                  className="w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
                  onClick={() => setDriverConfirm('update')}>{t('Cập nhật driver và tiếp tục', 'Update driver and continue')}</button>
                <p className="mt-2 text-xs text-muted-foreground">{t('App tự tìm driver phù hợp. Xác nhận quyền khi Windows hỏi.', 'The app finds a compatible driver. Approve permission when Windows asks.')}</p>
                </> : <>
                  <button type="button" disabled={Boolean(installing || driverSubmitting)}
                    className="w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
                    onClick={() => void installAction(driverStatus.kind as InstallKind)}>{t('Thử cài lại package GPU', 'Retry GPU package setup')}</button>
                  <p className="mt-2 text-xs text-muted-foreground">{t('Giữ GPU và thử lại bước cài đặt. Không tự cập nhật driver.', 'Keep GPU mode and retry installation. No automatic driver update.')}</p>
                </>}
              </div>}
              <div>
                <button type="button" disabled={Boolean(installing || driverSubmitting || !driverStatus.cpuRuntimeAvailable)}
                  className="w-full rounded-md border border-border px-3 py-2 text-sm font-medium disabled:opacity-50"
                  onClick={() => void runDriverAction('cpu')}>{driverStatus.kind === 'cpu_runtime' ? t('Thử cài lại runtime CPU', 'Retry CPU runtime setup') : t('Bỏ qua GPU và tiếp tục', 'Skip GPU and continue')}</button>
                <p className="mt-2 text-xs text-muted-foreground">{t('Dùng CPU, không cập nhật driver. Các tác vụ AI có thể chậm hơn.', 'Use the CPU without updating the driver. AI tasks may run slower.')}</p>
              </div>
            </div>}
            {driverStatus.driverCheck || driverStatus.eula?.length ? (
            <details className="max-h-60 overflow-auto text-xs whitespace-pre-wrap">
              <summary>{t('Thông tin GPU và điều khoản', 'GPU details and terms')}</summary>
              {driverStatus.driverCheck?.gpus?.map((gpu, index) => (
                <p key={gpu.deviceInstanceId || index}>{gpu.name || 'NVIDIA'} · {t('Driver', 'Driver')}: {gpu.driver || t('Chưa đọc được', 'Unavailable')}</p>
              ))}
              {driverStatus.driverCheck?.minimumVersion && <p>{t('Driver tối thiểu', 'Minimum driver')}: {driverStatus.driverCheck.minimumVersion}</p>}
              {driverStatus.eula?.map((text, index) => <p key={index}>{text}</p>)}
            </details>
          ) : null}</>}
          actions={!installing && (gpuChoice || driverWaiting || driverStatus.kind === 'nvidia_driver' || driverStatus.kind === 'cpu_runtime') ? <>
            {driverStatus.driverRepairAvailable && driverStatus.errorCode !== 'DRIVER_TOO_OLD'
              && ['ai_runtime', 'ocr_cuda', 'demucs_cuda'].includes(driverStatus.kind || '') && <button type="button"
                disabled={driverSubmitting} className="text-xs disabled:opacity-50"
                onClick={() => void installAction(driverStatus.kind as InstallKind)}>{t('Thử cài lại package GPU', 'Retry GPU package setup')}</button>}
            {driverWaiting && <button type="button" disabled={driverSubmitting}
              className="rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
              onClick={() => setDriverConfirm(driverStatus.rebootRequired ? 'reboot' : driverStatus.stage === 'driver_eula' ? 'accept_eula' : 'update')}>
              {driverStatus.rebootRequired ? t('Khởi động lại máy', 'Restart computer') : t('Chấp nhận và tiếp tục', 'Accept and continue')}
            </button>}
            <button type="button" className="text-xs" onClick={() => driverWaiting ? setInstallProgressMinimized(true) : setInstallPopupError('')}>{t('Để sau', 'Later')}</button>
            {(gpuChoice || driverStatus.driverCheck || driverStatus.kind === 'nvidia_driver') && <button type="button" className="text-xs"
              onClick={() => void copyText(JSON.stringify({ ...driverStatus, log: installLog }, null, 2))}>
              {t('Sao chép chẩn đoán', 'Copy diagnostics')}
            </button>}
          </> : undefined}
          onMinimize={() => {
            if (installing || driverWaiting) setInstallProgressMinimized(true)
            else setInstallPopupError('')
          }}
          onRestore={() => setInstallProgressMinimized(false)}
        />
        <ConfirmDialog open={cleanupConfirm}
          danger
          title={t('Xóa dữ liệu đã chọn?', 'Delete selected data?')}
          message={t('Dữ liệu đã xóa không thể khôi phục. Key, kích hoạt và cấu hình được giữ nguyên.', 'Deleted data cannot be recovered. Keys, activation and settings are preserved.')}
          confirmLabel={t('Xóa dữ liệu', 'Delete data')}
          cancelLabel={t('Hủy', 'Cancel')}
          onConfirm={() => void runCleanup()}
          onCancel={() => setCleanupConfirm(false)} />
        <ConfirmDialog open={driverConfirm !== null}
          scrollMessage={driverConfirm === 'accept_eula'}
          title={driverConfirm === 'reboot' ? t('Khởi động lại Windows?', 'Restart Windows?') : t('Cập nhật driver NVIDIA?', 'Update the NVIDIA driver?')}
          message={driverConfirm === 'reboot'
            ? t('Hãy lưu công việc đang mở. Khi mở lại app sau khi khởi động máy, app sẽ kiểm tra và tiếp tục cài gói AI.', 'Save your open work. When you reopen the app after restarting, it will verify the driver and resume AI installation.')
            : driverConfirm === 'accept_eula'
              ? t('Bạn đồng ý với điều khoản driver hiển thị bên dưới và cho phép tiếp tục cài đặt?', 'Do you accept the driver terms shown below and allow installation to continue?') + '\n\n' + (driverStatus.eula || []).join('\n\n')
              : t('App sẽ tự tìm và cài driver NVIDIA qua Windows Update. Màn hình có thể nháy. Hãy xác nhận quyền quản trị khi Windows hỏi; app không tự khởi động lại máy.', 'The app will find and install an NVIDIA driver through Windows Update. Your screen may flicker. Approve the Windows administrator prompt; the app will not restart your PC automatically.')}
          confirmLabel={driverConfirm === 'reboot' ? t('Khởi động lại máy', 'Restart computer') : t('Xác nhận', 'Confirm')}
          cancelLabel={t('Để sau', 'Later')}
          onConfirm={() => void runDriverAction()} onCancel={() => setDriverConfirm(null)} />
      </div>
      {updateDialog && updateMinimized && (updateDialog.kind === 'downloading' || updateDialog.kind === 'cancelling') ? (
        <button
          type="button"
          className="cfg-update-pill"
          onClick={() => setUpdateMinimized(false)}
          title={t('Mở lại tiến độ cập nhật', 'Restore update progress')}
        >
          <span className="cfg-update-pill-dot" aria-hidden />
          <span className="cfg-update-pill-text">
            {updateDialog.kind === 'cancelling'
              ? t('Đang hủy cập nhật…', 'Cancelling update…')
              : t('Đang tải cập nhật…', 'Downloading update…')}
          </span>
          <span className="cfg-update-pill-pct">{updateProgress}%</span>
        </button>
      ) : null}
      {updateDialog && !updateMinimized ? (
        <div
          className="cfg-update-layer"
          role="presentation"
          onMouseDown={() => updateDialog.kind !== 'downloading' && updateDialog.kind !== 'cancelling' && updateDialog.kind !== 'applying' && setUpdateDialog(null)}
        >
          <section
            className={`cfg-update-dialog is-${updateDialog.kind}`}
            role="dialog"
            aria-modal="true"
            aria-live="polite"
            aria-label={updateDialog.title}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="cfg-update-status" aria-hidden="true">
              <span />
            </div>
            {(updateDialog.kind === 'downloading' || updateDialog.kind === 'cancelling') && (
              <button
                type="button"
                className="cfg-update-close"
                aria-label={t('Chạy nền', 'Run in background')}
                title={t('Chạy nền', 'Run in background')}
                onClick={() => setUpdateMinimized(true)}
              >
                ×
              </button>
            )}
            <div className="cfg-update-copy">
              <h3>{updateDialog.title}</h3>
              <p>{updateDialog.detail}</p>
            </div>
            {updateDialog.kind === 'downloading' || updateDialog.kind === 'cancelling' ? (
              <>
                <div className="cfg-update-progress-label">
                  <span>{updateDialog.kind === 'cancelling' ? t('Đang hủy', 'Cancelling') : t('Tiến trình tải', 'Download progress')}</span>
                  <strong>{updateProgress}%</strong>
                </div>
                <div
                  className="cfg-update-progress"
                  role="progressbar"
                  aria-label={t('Tiến trình tải cập nhật', 'Update download progress')}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={updateProgress}
                >
                  <span style={{ width: `${updateProgress}%` }} />
                </div>
              </>
            ) : null}
            <div className="cfg-update-actions">
              {updateDialog.kind === 'available' ? <button type="button" className="primary" onClick={() => void downloadUpdate()}>{t('Cập nhật', 'Update')}</button> : null}
              {updateDialog.kind === 'downloading' ? (
                <>
                  <button type="button" onClick={() => setUpdateMinimized(true)}>{t('Chạy nền', 'Run in background')}</button>
                  <button type="button" className="cfg-update-cancel" onClick={() => void cancelUpdate()}>{t('Huỷ', 'Cancel')}</button>
                </>
              ) : null}
              {updateDialog.kind !== 'downloading' && updateDialog.kind !== 'cancelling' && updateDialog.kind !== 'applying' ? <button type="button" onClick={() => setUpdateDialog(null)}>{t('Đóng', 'Close')}</button> : null}
            </div>
          </section>
        </div>
      ) : null}
    </div>,
    document.body,
  )
}
