import { useRef, useState } from 'react'
import { localize, useLocale } from '@/app/i18n'
import { studioApi } from './studio.api'

type Props = {
  isDesktopApp: boolean
  onSelect: (paths: string[]) => void | Promise<void>
  onError: (message: string) => void
  onBusyChange?: (busy: boolean) => void
  multiple?: boolean
  recursive?: boolean
  disabled?: boolean
  className?: string
}

const videoAccept = '.mp4,.mov,.mkv,.webm,.avi,.m4v'

export function VideoSourcePicker({ isDesktopApp, onSelect, onError, onBusyChange, multiple = false, recursive = true, disabled, className }: Props) {
  const { locale } = useLocale()
  const t = (vi: string, en: string) => localize(locale, vi, en)
  const fileInput = useRef<HTMLInputElement>(null)
  const folderInput = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState('')

  async function select(folder: boolean, files?: File[]) {
    setBusy(true)
    onBusyChange?.(true)
    onError('')
    try {
      if (!files) {
        const paths = folder ? [(await studioApi.pickFolder()).path].filter(Boolean) : (await studioApi.pickVideos()).paths
        if (paths.length) await onSelect(multiple ? paths : paths.slice(0, 1))
      } else {
        const videos = (files || []).filter(file => /\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(file.name)
          && (!folder || recursive || file.webkitRelativePath.split('/').length <= 2))
        if (!videos.length) throw new Error('VIDEO_UPLOAD_UNSUPPORTED')
        const selected = multiple ? videos : videos.slice(0, 1)
        for (const [index, file] of selected.entries()) {
          setProgress(`${index + 1}/${selected.length} · ${file.name}`)
          // Keep completed selections in the form/queue if a later upload fails.
          const result = await studioApi.uploadVideo(file)
          await onSelect([result.path])
        }
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      onError(message === 'VIDEO_UPLOAD_UNSUPPORTED'
        ? t('Không có video phù hợp. Hỗ trợ MP4, MOV, MKV, WEBM, AVI và M4V.', 'No matching videos. Supported formats: MP4, MOV, MKV, WEBM, AVI and M4V.')
        : message === 'VIDEO_UPLOAD_EMPTY'
          ? t('File video trống. Hãy chọn lại file.', 'The video file is empty. Choose another file.')
          : t(`Không thêm được video: ${message}`, `Could not add video: ${message}`))
    } finally {
      setBusy(false)
      onBusyChange?.(false)
      setProgress('')
    }
  }

  function choose(folder: boolean) {
    if (isDesktopApp) void select(folder)
    else (folder ? folderInput : fileInput).current?.click()
  }

  return <>
    <input ref={fileInput} type="file" hidden accept={videoAccept} multiple={multiple} onChange={event => {
      const files = Array.from(event.currentTarget.files || [])
      event.currentTarget.value = ''
      if (files.length) void select(false, files)
    }} />
    {multiple && <input ref={folderInput} type="file" hidden multiple {...{ webkitdirectory: '' }} onChange={event => {
      const files = Array.from(event.currentTarget.files || [])
      event.currentTarget.value = ''
      if (files.length) void select(true, files)
    }} />}
    <button type="button" className={className} disabled={disabled || busy} onClick={() => choose(false)}>{multiple ? t('Thêm file', 'Add files') : t('Chọn', 'Choose')}</button>
    {multiple && <button type="button" className={className} disabled={disabled || busy} onClick={() => choose(true)}>{t('Thêm thư mục', 'Add folder')}</button>}
    {busy && <span role="status" aria-live="polite">{t('Đang thêm video…', 'Adding videos…')} {progress}</span>}
  </>
}
