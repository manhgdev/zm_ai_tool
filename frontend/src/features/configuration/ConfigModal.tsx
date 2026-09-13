import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { AppConfig, CloudProviderId, SystemChecks } from '@/features/project/project.types'
import { api } from '@/features/project/project.api'
import ProgressPopup from '@/shared/components/ProgressPopup'
import LicensePage from '@/features/license/LicensePage'
import type { LicenseStatus } from '@/features/license/license.api'
import { localize, useLocale } from '@/app/i18n'
import { copyText } from '@/shared/lib/clipboard'
import { toast } from 'sonner'
import { IconEye, IconEyeOff } from '@/shared/components/Icons'
import './ConfigModal.css'

import {
  type InstallKind, type Section, type CloudTab, type UpdateDialog, type CloudDraft,
  PROVIDERS, PROVIDER_PRESET_MODELS,
  installLabel, nextAutoInstall, emptyCloud, providerKeyPlaceholder,
} from './configModal.helpers'

interface ElKeySlot {
  savedIndex: number | null
  value: string
  visible?: boolean
  edited?: boolean
}

type Props = {
  open: boolean
  onClose: () => void
  initialSection?: Section
  forceSetup?: boolean
  onSetupReady?: () => void
  onSaved?: () => void
  licenseStatus?: LicenseStatus
  onLicenseStatusChange?: (status: LicenseStatus) => void
}

export default function ConfigModal({
  open,
  onClose,
  initialSection = 'cloud',
  forceSetup = false,
  onSetupReady,
  onSaved,
  licenseStatus,
  onLicenseStatusChange,
}: Props) {
  const { locale } = useLocale()
  const t = (vietnamese: string, english: string) => localize(locale, vietnamese, english)
  const systemCheckText = (id: string, value: string | undefined, kind: 'detail' | 'hint' | 'installLabel') => {
    if (!value) return ''
    if (id !== 'ollama') return value
    if (kind === 'detail' && value === 'chưa cài') return t('chưa cài', 'Not installed yet')
    if (kind === 'hint' && value === 'Dịch local (tuỳ chọn).') return t('Dịch local (tuỳ chọn).', 'Local translation (optional).')
    if (kind === 'installLabel' && /^Tải Ollama \((.+)\)$/.test(value)) {
      return value.replace(/^Tải Ollama \((.+)\)$/, (_, os: string) => `Download Ollama (${os})`)
    }
    return value
  }
  const [section, setSection] = useState<Section>(initialSection)
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
  const [checks, setChecks] = useState<SystemChecks | null>(null)
  const [checksLoading, setChecksLoading] = useState(false)
  const [checksErr, setChecksErr] = useState('')
  const [installing, setInstalling] = useState<string | null>(null)
  const [installProgressMinimized, setInstallProgressMinimized] = useState(false)
  const [installPopupError, setInstallPopupError] = useState('')
  const [installLog, setInstallLog] = useState('')
  const [pendingRestart, setPendingRestart] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [logText, setLogText] = useState('')
  const [logPath, setLogPath] = useState('')
  const [logLoading, setLogLoading] = useState(false)
  const [logErr, setLogErr] = useState('')
  const [logCopied, setLogCopied] = useState(false)
  const [updateChecking, setUpdateChecking] = useState(false)
  const [updateDialog, setUpdateDialog] = useState<UpdateDialog | null>(null)
  const autoSetupLock = useRef(false)
  const autoAttempted = useRef<Set<string>>(new Set())
  const restartRequested = useRef(false)
  const [outputRoot, setOutputRoot] = useState('')
  const [outputRootDefault, setOutputRootDefault] = useState('')
  const [outputRootSaving, setOutputRootSaving] = useState(false)

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
        detail: t('Gói đúng nền tảng sẽ được tải trước khi cài.', 'The platform-specific package will be downloaded before installation.'),
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

  const downloadUpdate = async () => {
    try {
      await api.installAppUpdate()
      setUpdateDialog({ kind: 'downloading', title: t('Đang tải cập nhật', 'Downloading update'), detail: t('Đang tải gói cài đặt…', 'Downloading the installation package…'), progress: 0 })
    } catch (error) {
      setUpdateDialog({ kind: 'error', title: t('Không thể tải cập nhật', 'Could not download update'), detail: error instanceof Error ? error.message : t('Vui lòng thử lại sau.', 'Please try again later.') })
    }
  }

  const applyUpdate = async () => {
    try {
      await api.applyAppUpdate()
      setUpdateDialog({ kind: 'applying', title: t('Đang cài cập nhật', 'Installing update'), detail: t('Đang giải nén và chuẩn bị bản mới…', 'Extracting and preparing the new version…'), progress: 0 })
    } catch (error) {
      setUpdateDialog({ kind: 'error', title: t('Không thể cài cập nhật', 'Could not install update'), detail: error instanceof Error ? error.message : t('Vui lòng thử lại sau.', 'Please try again later.') })
    }
  }

  useEffect(() => {
    const isApplying = updateDialog?.kind === 'applying'
    if (updateDialog?.kind !== 'downloading' && !isApplying) return
    let cancelled = false
    const poll = async () => {
      try {
        const state = await api.getAppUpdateStatus()
        if (cancelled) return
        if (state.phase === 'error') {
          setUpdateDialog({ kind: 'error', title: isApplying ? t('Không thể cài cập nhật', 'Could not install update') : t('Không thể tải cập nhật', 'Could not download update'), detail: state.error || state.message })
        } else if (state.phase === 'complete') {
          setUpdateDialog({ kind: 'complete', title: t('Cập nhật đã sẵn sàng', 'Update is ready'), detail: t('Thư mục bản mới đã được mở. Vui lòng chạy EXE trong thư mục đó để hoàn tất cập nhật.', 'The new version folder has opened. Please run the EXE inside to complete the update.'), progress: 100 })
        } else if (!isApplying && state.phase === 'ready') {
          setUpdateDialog({ kind: 'ready', title: t('Đã tải xong', 'Download complete'), detail: t('Gói cập nhật đã sẵn sàng để cài.', 'The update package is ready to install.'), progress: 100 })
        } else if (isApplying || state.phase === 'applying') {
          setUpdateDialog({ kind: 'applying', title: t('Đang cài cập nhật', 'Installing update'), detail: state.message || t('Đang giải nén và chuẩn bị bản mới…', 'Extracting and preparing the new version…'), progress: state.progress })
        } else {
          setUpdateDialog({ kind: 'downloading', title: t('Đang tải cập nhật', 'Downloading update'), detail: t('Đang tải gói cài đặt…', 'Downloading the installation package…'), progress: state.progress })
        }
      } catch (error) {
        if (!cancelled) setUpdateDialog({ kind: 'error', title: t('Mất kết nối cập nhật', 'Update connection failed'), detail: error instanceof Error ? error.message : t('Vui lòng thử lại sau.', 'Please try again later.') })
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 800)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [t, updateDialog?.kind])

  const loadLogs = useCallback(() => {
    setLogLoading(true)
    setLogErr('')
    void api
      .getAppLogs(1200)
      .then((r) => {
        setLogText(r.text || '(trống)')
        setLogPath(r.path || '')
      })
      .catch((e: Error) => {
        setLogErr(e.message || 'Không đọc được log')
        setLogText('')
      })
      .finally(() => setLogLoading(false))
  }, [])

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
    setSection(initialSection)
    // Load current outputRoot from /api/ui-preferences and resolved default from /api/config
    api.getLocalePreference().then((p) => {
      setOutputRoot((p as unknown as { outputRoot?: string | null }).outputRoot || '')
    }).catch(() => { })
    api.getConfig().then((cfg) => {
      setOutputRootDefault((cfg as unknown as { desktopOutputRoot?: string }).desktopOutputRoot || '')
    }).catch(() => { })
  }, [open, initialSection])

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
    if (section === 'setup' || forceSetup) loadChecks(false, false)
    if (section === 'logs') loadLogs()
  }, [open, section, forceSetup, loadChecks, loadLogs])

  useEffect(() => {
    if (!open || section !== 'setup') return
    let cancelled = false
    let errCount = 0
    let timerId: number

    const syncInstall = async () => {
      try {
        const st = await api.installStatus()
        if (cancelled) return
        errCount = 0  // reset on success
        if (st.running && st.kind) {
          setInstalling(st.kind)
          setMsg(`Đang cài ${installLabel(st.kind)}…`)
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
  }, [open, section])

  const cur = draft[tab]
  const canClose = !forceSetup || !!checks?.ok

  const installAction = useCallback(async (kind: InstallKind) => {
    setInstalling(kind)
    setInstallProgressMinimized(false)
    setInstallPopupError('')
    setInstallLog('')
    setChecksErr('')
    const onLog = (log: string) => setInstallLog(log)
    try {
      const result = kind === 'ai_runtime'
        ? await api.installAiRuntime(onLog)
        : kind === 'ocr_cuda'
          ? await api.installOcrCuda(onLog)
          : kind === 'demucs_cuda'
            ? await api.installDemucsCuda(onLog)
            : await api.installNvm(onLog)
      const doneMsg = result.detail || result.message || 'Hoàn thành'
      setInstallLog((prev) => prev ? `${prev}\n\n✓ ${doneMsg}` : `✓ ${doneMsg}`)
      setMsg(doneMsg)
      if (result.needsRestart) setPendingRestart(true)
      loadChecks(true, false)
      autoSetupLock.current = false
      // Giữ popup hiện tối thiểu 1.5s để user thấy kết quả
      await new Promise((r) => window.setTimeout(r, 1500))
    } catch (e) {
      const message = e instanceof Error
        ? e.message
        : kind === 'ai_runtime'
          ? 'Cài gói AI thất bại'
          : kind === 'ocr_cuda'
            ? 'Cài GPU OCR thất bại'
            : kind === 'demucs_cuda'
              ? 'Cài Demucs thất bại'
              : 'Cài NVM + Node.js LTS thất bại'
      setChecksErr(message)
      setInstallPopupError(message)
      // ponytail: giữ lock=true khi fail — tránh auto-retry vô tận.
      // User phải bấm nút thủ công để thử lại.
    } finally {
      setInstalling(null)
    }
  }, [loadChecks])


  const restartApp = useCallback(async () => {
    if (restartRequested.current) return
    restartRequested.current = true
    setPendingRestart(false)
    setRestarting(true)
    setChecksErr('')
    try {
      await api.restartApp()
    } catch (e) {
      restartRequested.current = false
      setChecksErr(e instanceof Error ? e.message : 'Không khởi động lại được app')
      autoSetupLock.current = false
    } finally {
      setRestarting(false)
    }
  }, [])

  useEffect(() => {
    if (!open || section !== 'setup') return
    if (checksLoading || installing || restarting || !checks) return
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
    installing,
    restarting,
    pendingRestart,
    installAction,
    onSetupReady,
  ])

  useEffect(() => {
    if (!forceSetup || !pendingRestart || installing || restarting) return
    void restartApp()
  }, [forceSetup, pendingRestart, installing, restarting, restartApp])

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
                ? `Đang cài ${installLabel(installing)}…`
                : forceSetup && !checks?.ok
                  ? 'Cài đủ thành phần bắt buộc để bắt đầu'
                  : t('Thiết lập hệ thống · Cloud AI · ElevenLabs', 'System settings · Cloud AI · ElevenLabs')}
            </p>
          </div>
          <div className="cfg-head-actions">
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
                className={section === 'license' ? 'active' : undefined}
                onClick={() => setSection('license')}
              >
                {t('Kích hoạt', 'Activate')}
              </button>
              <button
                type="button"
                className={section === 'output' ? 'active' : undefined}
                onClick={() => setSection('output')}
              >
                {t('Thư mục đầu ra', 'Output folder')}
              </button>
              <button
                type="button"
                className={section === 'logs' ? 'active' : undefined}
                onClick={() => setSection('logs')}
              >
                Log
              </button>
            </>
          ) : null}
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

        {loading && section !== 'setup' && section !== 'logs' ? (
          <p className="cfg-msg">Đang tải…</p>
        ) : section === 'setup' ? (
          <div className="cfg-body cfg-setup">
            <div className="cfg-setup-bar">
              <div className="cfg-setup-info">
                <strong>
                  {installing
                    ? `Đang cài ${installLabel(installing)}…`
                    : checks?.summary || (checksLoading ? 'Đang tải…' : '—')}
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
                    const done = (checks?.items || []).some(
                      (it) =>
                        it.ok &&
                        (it.install === a.id ||
                          (a.id === 'demucs_cuda' && it.id === 'demucs') ||
                          (a.id === 'ocr_cuda' && it.id === 'ocr_cuda')),
                    )
                    return done ? (
                      <span key={a.id} className="cfg-check-installed cfg-setup-chip">
                        {a.label} ✓
                      </span>
                    ) : (
                      <button
                        key={a.id}
                        type="button"
                        className="cfg-check-install cfg-check-install-sm"
                        disabled={!!installing}
                        onClick={() => {
                          autoSetupLock.current = false
                          autoAttempted.current.clear()
                          void installAction(a.id as 'ocr_cuda' | 'demucs_cuda')
                        }}
                      >
                        {installing === a.id ? '…' : a.label}
                      </button>
                    )
                  })
                  : null}
                <button
                  type="button"
                  className="cfg-secondary cfg-setup-refresh"
                  disabled={checksLoading || !!installing}
                  onClick={() => { autoAttempted.current.clear(); loadChecks(true, false) }}
                >
                  {checksLoading ? '…' : t('Kiểm tra lại', 'Check again')}
                </button>
              </div>
            </div>
            {checksErr ? <p className="cfg-msg cfg-msg-err">{checksErr}</p> : null}
            {pendingRestart ? (
              <p className="cfg-msg cfg-msg-restart">
                Đã cài gói cần reload — cài tiếp các mục còn lại rồi bấm{' '}
                <strong>Khởi động lại</strong>.
              </p>
            ) : null}
            {msg && section === 'setup' ? <p className="cfg-msg">{msg}</p> : null}
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
                        {it.id === 'ai_runtime_diarization'
                          ? t('Sherpa-ONNX (Tách người nói)', 'Sherpa-ONNX (Speaker diarization)')
                          : it.name}
                        {it.required ? (
                          <em className="cfg-req">{t('bắt buộc', 'required')}</em>
                        ) : (
                          <em className="cfg-opt">{t('tuỳ chọn', 'optional')}</em>
                        )}
                      </div>
                    </div>
                    <div className="cfg-check-action">
                      {it.ok ? (
                        ['ai_runtime', 'ai_runtime_ocr', 'ai_runtime_vieneu', 'ocr_cuda', 'demucs_cuda', 'nvm'].includes(it.install) ? (
                          <span className="cfg-check-installed">{t('Đã cài', 'Installed')}</span>
                        ) : null
                      ) : ['ai_runtime', 'ai_runtime_ocr', 'ai_runtime_vieneu', 'ocr_cuda', 'demucs_cuda', 'nvm'].includes(it.install) ? (
                        <button
                          type="button"
                          className="cfg-check-install"
                          disabled={!!installing}
                          onClick={() => {
                            autoSetupLock.current = false
                            autoAttempted.current.clear()
                            // ai_runtime_ocr / ai_runtime_vieneu → cùng endpoint ai_runtime
                            const kind = it.install.startsWith('ai_runtime')
                              ? 'ai_runtime'
                              : it.install as 'ocr_cuda' | 'demucs_cuda' | 'nvm'
                            void installAction(kind)
                          }}
                        >
                          {installing === it.install || (it.install.startsWith('ai_runtime') && installing === 'ai_runtime')
                            ? 'Đang cài…'
                            : it.installLabel ||
                            (it.install.startsWith('ai_runtime')
                              ? t('Cài gói AI', 'Install AI packages')
                              : it.install === 'demucs_cuda'
                                ? checks?.device?.install?.demucsLabel || t('Cài Demucs GPU', 'Install Demucs (GPU)')
                                : checks?.device?.install?.ocrLabel || t('Cài OCR CUDA', 'Install OCR (CUDA)'))}
                        </button>
                      ) : it.install && it.install.startsWith('http') ? (
                        <a
                          className="cfg-check-link"
                          href={it.install}
                          target="_blank"
                          rel="noreferrer"
                          title={systemCheckText(it.id, it.installLabel, 'installLabel') || it.install}
                        >
                          {systemCheckText(it.id, it.installLabel, 'installLabel') || t('Tải', 'Download')}
                        </a>
                      ) : null}
                    </div>
                  </div>
                  <div className="cfg-check-body">
                    {it.detail ? <div className="cfg-check-detail">{systemCheckText(it.id, it.detail, 'detail')}</div> : null}
                    {!it.ok && it.hint ? <div className="cfg-check-hint">{systemCheckText(it.id, it.hint, 'hint')}</div> : null}
                    {!it.ok && it.install && !it.install.startsWith('http') && !['ai_runtime', 'ai_runtime_ocr', 'ai_runtime_vieneu', 'ocr_cuda', 'demucs_cuda', 'nvm'].includes(it.install) ? (
                      <code className="cfg-check-cmd" title={it.installLabel || 'Chạy trong terminal'}>
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
                    const isPreset = presets.some((m) => m.id === cur.model)
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
                          {presets.map((m) => (
                            <option key={m.id} value={m.id}>
                              {localize(locale, m.labelVi, m.labelEn)}
                            </option>
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
            <div className="cfg-output-current">
              <span className="cfg-output-current-label">{t('Hiện tại', 'Current path')}</span>
              <code className="cfg-output-current-path" title={outputRootDefault}>
                {outputRootDefault || t('Đang tải…', 'Loading…')}
              </code>
            </div>
            <div className="cfg-output-root">
              <label className="cfg-output-root-label" htmlFor="cfg-output-root-input">
                {t('Thư mục đầu ra', 'Output folder')}
              </label>
              <div className="cfg-output-root-row">
                <input
                  id="cfg-output-root-input"
                  className="cfg-output-root-input"
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
                  title={t('Chọn thư mục bằng hộp thoại', 'Browse for folder')}
                  onClick={async () => {
                    try {
                      const res = await fetch('/api/system/pick-folder', { method: 'POST' })
                      if (!res.ok) return
                      const data = await res.json() as { ok: boolean; path: string }
                      if (data.ok && data.path) {
                        setOutputRoot(data.path)
                      }
                    } catch { /* dialog cancelled */ }
                  }}
                >
                  <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ width: '1em', height: '1em', verticalAlign: '-0.15em' }}><path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" /></svg>
                  {' '}{t('Chọn', 'Browse')}
                </button>
              </div>
              <div className="cfg-output-root-actions">
                <button
                  id="cfg-output-root-save"
                  type="button"
                  className="cfg-primary"
                  disabled={outputRootSaving}
                  onClick={async () => {
                    const raw = outputRoot.trim()
                    if (raw && !raw.match(/^([A-Za-z]:[/\\]|\/)/)) {
                      toast.error(t('Phải là đường dẫn tuyệt đối', 'Must be an absolute path'))
                      return
                    }
                    setOutputRootSaving(true)
                    try {
                      await api.saveOutputRoot(raw)
                      // Refresh displayed current path
                      const cfg = await api.getConfig()
                      setOutputRootDefault((cfg as unknown as { desktopOutputRoot?: string }).desktopOutputRoot || '')
                      toast.success(t('Đã lưu — áp dụng từ lần xuất tiếp theo', 'Saved — applies from next export'))
                    } catch {
                      toast.error(t('Lỗi lưu thư mục đầu ra', 'Failed to save output folder'))
                    } finally {
                      setOutputRootSaving(false)
                    }
                  }}
                >
                  {outputRootSaving ? '…' : t('Lưu', 'Save')}
                </button>
                <button
                  type="button"
                  className="cfg-secondary"
                  disabled={outputRootSaving || !outputRoot}
                  onClick={async () => {
                    setOutputRootSaving(true)
                    try {
                      await api.saveOutputRoot('')
                      setOutputRoot('')
                      const cfg = await api.getConfig()
                      setOutputRootDefault((cfg as unknown as { desktopOutputRoot?: string }).desktopOutputRoot || '')
                      toast.success(t('Đã đặt lại về mặc định', 'Reset to default'))
                    } catch {
                      toast.error(t('Lỗi', 'Error'))
                    } finally {
                      setOutputRootSaving(false)
                    }
                  }}
                >
                  {t('Đặt lại mặc định', 'Reset to default')}
                </button>
              </div>
            </div>

            {outputRootDefault ? (() => {
              const sep = outputRootDefault.includes('\\') ? '\\' : '/'
              const p = (sub: string) => outputRootDefault.replace(/[\\/]+$/, '') + sep + sub.replace(/\//g, sep)
              const rows: [string, string][] = [
                ['Clone',         p('clone')],
                ['Review',        p('review')],
                ['Flow/Video',    p('flow' + sep + 'video')],
                ['Flow/Ảnh',      p('flow' + sep + 'image')],
                ['TTS',           p('text-to-speech')],
                [t('Tải video','Download'), p('download-video')],
                [t('Phụ đề xuất','Subs export'), p('subtitles' + sep + 'export')],
                [t('Phụ đề ảnh','Subs image'),  p('subtitles' + sep + 'image-video')],
                ['Drawing',       p('drawing')],
                ['Cleaner',       p('cleaner')],
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
                        <code className="cfg-subfolder-path">{full}</code>
                      </div>
                    ))}
                  </div>
                </div>
              )
            })() : null}
          </div>
        ) : section === 'license' ? (
          licenseStatus && onLicenseStatusChange ? <LicensePage status={licenseStatus} embedded onStatusChange={onLicenseStatusChange} /> : null
        ) : section === 'logs' ? (
          <div className="cfg-log-panel">
            <p className="cfg-hint">
              {t(
                'Lỗi job (Dịch / Lồng tiếng / Xuất), warm-models, crash hook. Copy gửi AI để sửa.',
                'Job errors (translation, dubbing, export), warm-models, and crash hooks. Copy this for AI troubleshooting.',
              )}
              {logPath ? (
                <>
                  {' '}
                  File: <code className="cfg-log-path">{logPath}</code>
                </>
              ) : null}
            </p>
            {logErr ? <p className="cfg-msg cfg-msg-err">{logErr}</p> : null}
            <pre className="cfg-log-pre" tabIndex={0}>
              {logLoading ? t('Đang tải…', 'Loading…') : logText || t('(trống)', '(empty)')}
            </pre>
            <div className="cfg-log-actions">
              <button type="button" className="cfg-secondary" disabled={logLoading} onClick={() => loadLogs()}>
                {logLoading ? t('Đang tải…', 'Loading…') : t('Tải lại', 'Reload')}
              </button>
              <button
                type="button"
                className="cfg-secondary"
                disabled={!logText || logLoading}
                onClick={() => {
                  void copyText(logText).then(() => {
                    setLogCopied(true)
                    window.setTimeout(() => setLogCopied(false), 1600)
                  })
                }}
              >
                {logCopied ? t('Đã copy', 'Copied') : 'Copy log'}
              </button>
              <button
                type="button"
                className="cfg-secondary"
                disabled={logLoading}
                onClick={() => {
                  if (!window.confirm(t('Xóa toàn bộ file log?', 'Delete all log files?'))) return
                  void api.clearAppLogs().then(() => {
                    loadLogs()
                    toast.success(t('Đã xóa log.', 'Logs deleted.'))
                  }).catch((e: Error) => {
                    setLogErr(e.message)
                    toast.error(e.message)
                  })
                }}
              >
                {t('Xóa log', 'Delete logs')}
              </button>
            </div>
          </div>
        ) : null}

        {msg && section !== 'logs' ? <p className="cfg-msg">{msg}</p> : null}

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
              {pendingRestart ? (
                <button
                  type="button"
                  className="cfg-secondary cfg-restart-btn"
                  disabled={restarting || !!installing}
                  onClick={() => void restartApp()}
                >
                  {restarting ? 'Đang khởi động lại…' : 'Khởi động lại'}
                </button>
              ) : null}
              <button
                type="button"
                className="cfg-primary"
                disabled={checksLoading || !checks?.ok}
                onClick={() => {
                  if (checks?.ok) onSetupReady?.()
                  else loadChecks(true, false)
                }}
              >
                {checks?.ok ? t('Bắt đầu', 'Start') : checksLoading ? t('Đang chuẩn bị…', 'Preparing…') : t('Thử lại', 'Retry')}
              </button>
            </>
          ) : section === 'logs' || section === 'license' || section === 'output' ? (
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
          active={Boolean(installing || installPopupError)}
          minimized={installProgressMinimized}
          running={Boolean(installing)}
          title={
            installPopupError
              ? 'Cài đặt thất bại'
              : installing === 'ai_runtime'
                ? 'Đang cài gói AI'
                : installing === 'ocr_cuda'
                  ? 'Đang cài GPU OCR'
                  : installing === 'demucs_cuda'
                    ? 'Đang cài Demucs'
                    : 'Đang cài NVM + Node.js LTS'
          }
          message={
            installing
              ? `Đang cài ${installLabel(installing)}. Vui lòng không tắt ứng dụng.`
              : installPopupError || undefined
          }
          progress={installing ? 35 : 0}
          error={installPopupError || null}
          log={installLog || undefined}
          onMinimize={() => {
            if (installing) setInstallProgressMinimized(true)
            else setInstallPopupError('')
          }}
          onRestore={() => setInstallProgressMinimized(false)}
        />
      </div>
      {updateDialog ? (
        <div
          className="cfg-update-layer"
          role="presentation"
          onMouseDown={() => updateDialog.kind !== 'downloading' && updateDialog.kind !== 'applying' && setUpdateDialog(null)}
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
            <div className="cfg-update-copy">
              <h3>{updateDialog.title}</h3>
              <p>{updateDialog.detail}</p>
            </div>
            {updateDialog.kind === 'downloading' || updateDialog.kind === 'applying' ? (
              <>
                <div className="cfg-update-progress-label">
                  <span>{updateDialog.kind === 'applying' ? t('Tiến trình cài đặt', 'Installation progress') : t('Tiến trình tải', 'Download progress')}</span>
                  <strong>{updateProgress}%</strong>
                </div>
                <div
                  className="cfg-update-progress"
                  role="progressbar"
                  aria-label={updateDialog.kind === 'applying' ? t('Tiến trình cài đặt cập nhật', 'Update installation progress') : t('Tiến trình tải cập nhật', 'Update download progress')}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={updateProgress}
                >
                  <span style={{ width: `${updateProgress}%` }} />
                </div>
              </>
            ) : null}
            <div className="cfg-update-actions">
              {updateDialog.kind === 'available' ? <button type="button" className="primary" onClick={() => void downloadUpdate()}>{t('Tải cập nhật', 'Download update')}</button> : null}
              {updateDialog.kind === 'ready' ? <button type="button" className="primary" onClick={() => void applyUpdate()}>{t('Cài cập nhật', 'Install update')}</button> : null}
              {updateDialog.kind !== 'downloading' && updateDialog.kind !== 'applying' ? <button type="button" onClick={() => setUpdateDialog(null)}>{t('Đóng', 'Close')}</button> : null}
            </div>
          </section>
        </div>
      ) : null}
    </div>,
    document.body,
  )
}
