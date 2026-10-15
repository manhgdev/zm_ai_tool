import type { AppLocale } from '@/app/i18n'
import { cn } from '@/shared/lib/cn'
import './LocaleSwitch.css'

type Props = {
  locale: AppLocale
  onChange: (locale: AppLocale) => void
  ariaLabel: string
  className?: string
}

/** Always shows both VI and EN side by side. */
export function LocaleSwitch({ locale, onChange, ariaLabel, className }: Props) {
  return (
    <div className={cn('locale-switch', className)} role="group" aria-label={ariaLabel}>
      <button
        type="button"
        className={cn('locale-switch-btn', locale === 'vi' && 'is-active')}
        aria-pressed={locale === 'vi'}
        onClick={() => onChange('vi')}
      >
        VI
      </button>
      <button
        type="button"
        className={cn('locale-switch-btn', locale === 'en' && 'is-active')}
        aria-pressed={locale === 'en'}
        onClick={() => onChange('en')}
      >
        EN
      </button>
    </div>
  )
}
