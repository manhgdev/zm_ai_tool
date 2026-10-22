import { toast } from 'sonner'
import { loadLocale, localize } from '@/app/i18n'

export interface CopyTextOptions {
  /** Custom success message (Vietnamese/English) */
  successMessage?: string
  /** Custom error message */
  errorMessage?: string
  /** Whether to show toast notification (defaults to true) */
  notify?: boolean
}

/** Copy text in browsers and native webviews, including denied Clipboard API contexts. */
export async function copyText(text: string, options?: CopyTextOptions | string): Promise<void> {
  const opts: CopyTextOptions = typeof options === 'string' ? { successMessage: options } : (options ?? {})
  const shouldNotify = opts.notify !== false
  const locale = loadLocale()

  const defaultSuccess = localize(locale, 'Đã sao chép vào clipboard.', 'Copied to clipboard.')
  const defaultError = localize(locale, 'Không thể sao chép vào clipboard.', 'Could not copy to clipboard.')

  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text)
        if (shouldNotify) {
          toast.success(opts.successMessage || defaultSuccess)
        }
        return
      } catch {
        // Native webviews can expose the API but deny it; use the DOM fallback.
      }
    }

    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.readOnly = true
    textarea.style.position = 'fixed'
    textarea.style.inset = '0 auto auto -9999px'
    textarea.style.opacity = '0'
    const previous = document.activeElement as HTMLElement | null
    // A modal makes the rest of the document inert, so its copy target must stay inside it.
    const container = previous?.closest('dialog[open]') || document.querySelector('dialog[open]') || document.body
    container.appendChild(textarea)
    try {
      textarea.focus()
      textarea.select()
      textarea.setSelectionRange(0, textarea.value.length)
      if (!document.execCommand('copy')) throw new Error('COPY_FAILED')
      if (shouldNotify) {
        toast.success(opts.successMessage || defaultSuccess)
      }
    } finally {
      textarea.remove()
      previous?.focus()
    }
  } catch (err) {
    if (shouldNotify) {
      toast.error(opts.errorMessage || defaultError)
    }
    throw err
  }
}
