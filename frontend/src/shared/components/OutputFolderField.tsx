import { useEffect, useMemo, useState } from 'react'
import { localize, useLocale } from '@/app/i18n'
import './OutputFolderField.css'

let desktopOutputRootRequest: Promise<string> | undefined

export function normalizeWebOutputName(value: string, appFolder: string) {
  const trimmed = value.trim()
  if (trimmed === appFolder) return ''
  if (!/^(?:[A-Za-z]:[\\/]|[\\/])/.test(trimmed)) return value
  // Absolute path: strip any known prefix (any user's home/Downloads/ZM_AI_TOOL/appFolder)
  const normalized = trimmed.replace(/\\/g, '/')
  const idx = normalized.toLowerCase().indexOf('/zm_ai_tool/')
  if (idx >= 0) {
    const after = normalized.slice(idx + '/zm_ai_tool/'.length)
    const folder = appFolder.replace(/^[\/\\]+|[\/\\]+$/g, '')
    return after.startsWith(folder + '/') ? after.slice(folder.length + 1) : after
  }
  return ''
}

/** Keep the selected directory recognizable when a narrow panel hides its full path. */
export function compactOutputPrefix(prefix: string) {
  const normalized = prefix.replace(/\\/g, '/').replace(/\/+$/, '')
  const parts = normalized.split('/').filter(Boolean)
  const tail = parts.slice(-2).join('/')
  return tail ? `…/${tail}/` : prefix
}

function loadDesktopOutputRoot() {
  if (!desktopOutputRootRequest) {
    desktopOutputRootRequest = fetch('/api/config')
      .then(async (response) => response.ok ? response.json() as Promise<{ desktopOutputRoot?: string }> : null)
      .then((config) => String(config?.desktopOutputRoot || ''))
      .catch(() => '')
  }
  return desktopOutputRootRequest
}

type Props = {
  isDesktopApp: boolean
  value: string
  onChange: (value: string) => void
  onChoose?: () => string | null | undefined | Promise<string | null | undefined | void>
  onSave?: () => void | Promise<void>
  defaultPath: string
  appFolder: string
  label?: string
  disabled?: boolean
  webFolderOnly?: boolean
}

export function OutputFolderField({
  isDesktopApp,
  value,
  onChange,
  onChoose,
  onSave,
  defaultPath,
  appFolder,
  label,
  disabled = false,
  webFolderOnly = false,
}: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const [message, setMessage] = useState('')
  const [desktopOutputRoot, setDesktopOutputRoot] = useState('')
  useEffect(() => setMessage(''), [value])
  useEffect(() => {
    // ponytail: backend always returns desktopOutputRoot (web + desktop same machine)
    let active = true
    void loadDesktopOutputRoot()
      .then((outputRoot) => {
        if (active && outputRoot) setDesktopOutputRoot(outputRoot)
      })
      .catch(() => undefined)
    return () => { active = false }
  }, [])

  const webPathPrefix = useMemo(
    () => `/Downloads/ZM_AI_TOOL/${appFolder.replace(/^[/\\]+|[/\\]+$/g, '')}/`,
    [appFolder],
  )
  const appPath = useMemo(() => {
    const folderSuffix = appFolder.replace(/^[/\\]+|[/\\]+$/g, '')
    const defaultRoot = desktopOutputRoot
      ? `${desktopOutputRoot.replace(/[\\/]+$/, '')}/${folderSuffix}`
      : `Downloads/ZM_AI_TOOL/${folderSuffix}`
    const entered = value.trim()
    if (!entered || !/^(?:[A-Za-z]:[\\/]|[\\/])/.test(entered)) return { prefix: `${defaultRoot}/`, suffix: value }
    const normalized = entered.replace(/[\\/]+$/, '')
    if (/[\\/]$/.test(entered)) return { prefix: `${normalized}/`, suffix: '' }
    const separatorIndex = Math.max(normalized.lastIndexOf('/'), normalized.lastIndexOf('\\'))
    return separatorIndex < 0
      ? { prefix: `${defaultRoot}/`, suffix: value }
      : { prefix: normalized.slice(0, separatorIndex + 1), suffix: normalized.slice(separatorIndex + 1) }
  }, [appFolder, desktopOutputRoot, value])
  const webSuffix = useMemo(() => normalizeWebOutputName(value, appFolder), [appFolder, value])
  // Use real local path when backend tells us its output root (web + desktop)
  const hasLocalRoot = Boolean(desktopOutputRoot)
  const outputPrefix = (isDesktopApp || hasLocalRoot) ? appPath.prefix : webPathPrefix
  const compactPrefix = useMemo(() => compactOutputPrefix(outputPrefix), [outputPrefix])
  const outputSuffix = (isDesktopApp || hasLocalRoot) ? appPath.suffix : webSuffix

  useEffect(() => {
    // On pure web without a local root, normalise absolute paths out
    if ((isDesktopApp || hasLocalRoot) && webSuffix !== value) return
    if (!isDesktopApp && !hasLocalRoot && webSuffix !== value) onChange(webSuffix)
  }, [isDesktopApp, hasLocalRoot, onChange, value, webSuffix])

  function changeOutputSuffix(nextSuffix: string) {
    const suffix = nextSuffix.trimStart()
    onChange(isDesktopApp ? (suffix ? `${appPath.prefix}${suffix}` : '') : suffix)
  }

  async function save() {
    await onSave?.()
    setMessage(isDesktopApp
      ? t('Đã lưu thư mục.', 'Folder saved.')
      : webFolderOnly
        ? t('Đã lưu tên thư mục con.', 'Subfolder name saved.')
        : t('Đã lưu tên thư mục con / tên đầu ra.', 'Subfolder / output name saved.'))
  }

  async function chooseOutputFolder() {
    const selected = await onChoose?.()
    if (!isDesktopApp || !selected) return
    onChange(`${selected.replace(/[\\/]+$/, '')}/`)
    // ponytail: auto-save on picker select — no need to press Lưu manually
    await onSave?.()
    setMessage(t('Đã lưu thư mục.', 'Folder saved.'))
  }

  return (
    <label className="output-folder-field">
      <span className="output-folder-label">
        {label || t('Thư mục lưu', 'Save folder')}
      </span>
      <div className={`output-folder-row ${isDesktopApp ? 'is-app' : 'is-web-editable'} ${onChoose ? 'has-choose' : ''}`}>
        <div className="output-folder-combined">
          <span className="output-folder-prefix" aria-disabled="true" title={outputPrefix}>
            <span className="output-folder-prefix-full">{outputPrefix}</span>
            <span className="output-folder-prefix-short" aria-hidden="true">{compactPrefix}</span>
          </span>
          <input
            aria-label={t('Tên thư mục hoặc tệp đầu ra', 'Output subfolder or file name')}
            className="output-folder-suffix"
            type="text"
            value={outputSuffix}
            onChange={(event) => changeOutputSuffix(event.target.value)}
            placeholder={defaultPath}
            title={t('Nhập tên thư mục con hoặc tên tệp sau đường dẫn cố định.', 'Enter a subfolder or file name after the fixed path.')}
            disabled={disabled}
            spellCheck={false}
          />
        </div>
        {isDesktopApp || onChoose ? (
          <>
            <button type="button" disabled={disabled || !onChoose} onClick={() => void chooseOutputFolder()} title={t('Chọn thư mục', 'Choose folder')}>
              <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" /></svg>
              {t('Chọn', 'Choose')}
            </button>
            <button type="button" disabled={disabled || !value.trim()} onClick={() => void save()} title={t(isDesktopApp ? 'Lưu thư mục' : 'Lưu tên đầu ra', isDesktopApp ? 'Save folder' : 'Save output name')}>
              {t('Lưu', 'Save')}
            </button>
          </>
        ) : (
          <>
            <button type="button" disabled={disabled || !value.trim()} onClick={() => void save()} title={t('Lưu tên đầu ra', 'Save output name')}>
              {t('Lưu', 'Save')}
            </button>
          </>
        )}
      </div>
      {message ? <span className="output-folder-hint">{message}</span> : null}
    </label>
  )
}
