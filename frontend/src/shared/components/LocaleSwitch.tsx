import { localize, type AppLocale } from '@/app/i18n'
import { cn } from '@/shared/lib/cn'
import './LocaleSwitch.css'

type Props = {
  locale: AppLocale
  onChange: (locale: AppLocale) => void
  ariaLabel: string
  className?: string
  compactOnSmallScreen?: boolean
}

export function LocaleSwitch({ locale, onChange, ariaLabel, className, compactOnSmallScreen = false }: Props) {
  return (
    <div className={cn('locale-switch', compactOnSmallScreen && 'locale-switch-responsive', className)} role="group" aria-label={ariaLabel}>
      {compactOnSmallScreen && (
        <button
          type="button"
          className="locale-switch-btn locale-switch-toggle is-active"
          aria-label={localize(locale, 'Chuyển sang tiếng Anh', 'Switch to Vietnamese')}
          onClick={() => onChange(locale === 'vi' ? 'en' : 'vi')}
        >
          {locale.toUpperCase()}
        </button>
      )}
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
