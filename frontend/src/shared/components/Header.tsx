import { useEffect, useRef, useState } from 'react'
import type { HardwareInfo } from '@/features/project/project.types'
import type { LicenseStatus } from '@/features/license/license.api'
import {
  IconBook,
  IconCam,
  IconDownload,
  IconFilm,
  IconBatch,
  IconGear,
  IconLayers,
  IconLogo,
  IconMic,
  IconVideo,
  IconWand,
} from '@/shared/components/Icons'
import './Header.css'
import { translate, type AppLocale } from '@/app/i18n'

export type AppMode = 'clone' | 'live-preview' | 'tts' | 'download' | 'film' | 'batch' | 'flow' | 'chat' | 'automation' | 'renders' | 'cleaner' | 'srt-image' | 'srt-export' | 'drawing' | 'license'

function IconSun({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
    </svg>
  )
}

function IconMoon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
    </svg>
  )
}

const NAV: {
  id: AppMode | 'tools' | 'config' | 'clone-menu' | 'batch-menu'
  label: 'nav.clone' | 'nav.batch' | 'nav.flow' | 'nav.chat' | 'nav.automation' | 'nav.livePreview' | 'nav.renders' | 'nav.tts' | 'nav.tools' | 'nav.settings'
  Icon: typeof IconCam
  mode?: AppMode
  action?: 'config' | 'tools' | 'clone-menu' | 'batch-menu'
}[] = [
  { id: 'clone-menu', label: 'nav.clone', Icon: IconCam, action: 'clone-menu' },
  { id: 'batch-menu', label: 'nav.batch', Icon: IconBatch, action: 'batch-menu' },
  { id: 'flow', label: 'nav.flow', Icon: IconLayers, mode: 'flow' },
  { id: 'renders', label: 'nav.renders', Icon: IconVideo, mode: 'renders' },
  { id: 'tts', label: 'nav.tts', Icon: IconMic, mode: 'tts' },
  { id: 'tools', label: 'nav.tools', Icon: IconWand, action: 'tools' },
  { id: 'config', label: 'nav.settings', Icon: IconGear, action: 'config' },
]

const HARDWARE_SHORT: Record<string, string> = { cpu: 'CPU', cuda: 'GPU', metal: 'GPU' }

function IconMenu({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  )
}

type Props = {
  hardware: HardwareInfo
  dark: boolean
  mode?: AppMode
  onModeChange?: (mode: AppMode) => void
  onToggleTheme: () => void
  onOpenConfig?: () => void
  /** TTS mobile: ☰ thay logo — mở sidebar trái */
  onMenuClick?: () => void
  menuOpen?: boolean
  licenseStatus?: LicenseStatus
  locale: AppLocale
  onLocaleChange: (locale: AppLocale) => void
  onOpenLicense?: () => void
}

export default function Header({
  hardware,
  dark,
  mode = 'clone',
  onModeChange,
  onToggleTheme,
  onOpenConfig,
  onMenuClick,
  menuOpen = false,
  licenseStatus,
  locale,
  onLocaleChange,
  onOpenLicense,
}: Props) {
  const t = (key: Parameters<typeof translate>[1], values?: Record<string, string | number>) => translate(locale, key, values)
  const hardwareDisplay = HARDWARE_SHORT[hardware.accel] ?? hardware.accel.toUpperCase()
  const showTtsMenu = mode === 'tts' && typeof onMenuClick === 'function'
  const [openMenu, setOpenMenu] = useState<null | 'clone' | 'batch' | 'tools'>(null)
  const cloneRef = useRef<HTMLDivElement>(null)
  const batchRef = useRef<HTMLDivElement>(null)
  const toolsRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!openMenu) return
    const close = (event: MouseEvent) => {
      const node = event.target as Node
      if (cloneRef.current?.contains(node) || batchRef.current?.contains(node) || toolsRef.current?.contains(node)) return
      setOpenMenu(null)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenMenu(null)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [openMenu])

  return (
    <header className={`header${showTtsMenu ? ' header--tts' : ''}`}>
      <div className="brand">
        {showTtsMenu && (
          <button
            type="button"
            className={`header-menu-btn${menuOpen ? ' is-open' : ''}`}
            onClick={onMenuClick}
            aria-label={menuOpen ? t('header.closeTtsMenu') : t('header.openTtsMenu')}
            aria-expanded={menuOpen}
            title="Menu Text to Speech"
          >
            <IconMenu size={22} />
          </button>
        )}
        <button
          type="button"
          className="brand-home"
          onClick={() => onModeChange?.('clone')}
          title={t('nav.clone')}
          aria-label={t('nav.clone')}
        >
          <span className="header-logo-wrap" aria-hidden="true">
            <IconLogo />
          </span>
          <span className="brand-text">
            <strong>ZM AI TOOL</strong>
            <span>{t('brand.tagline')}</span>
          </span>
        </button>
      </div>
      <nav className="nav" aria-label="Chính">
        {NAV.map((item) => {
          if (item.action === 'clone-menu') {
            return (
              <div key={item.id} className="nav-tools" ref={cloneRef}>
                <button
                  type="button"
                  className={mode === 'clone' || mode === 'film' ? 'active' : undefined}
                  title={t(item.label)}
                  aria-haspopup="menu"
                  aria-expanded={openMenu === 'clone'}
                  onClick={() => setOpenMenu((cur) => (cur === 'clone' ? null : 'clone'))}
                >
                  <item.Icon size={16} />
                  <span>{t(item.label)}</span>
                </button>
                {openMenu === 'clone' ? (
                  <div className="nav-tools-menu" role="menu">
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'clone' ? 'active' : undefined}
                      onClick={() => {
                        setOpenMenu(null)
                        onModeChange?.('clone')
                      }}
                    >
                      <IconCam size={16} />
                      <span>{t('nav.cloneVideo')}</span>
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'film' ? 'active' : undefined}
                      onClick={() => {
                        setOpenMenu(null)
                        onModeChange?.('film')
                      }}
                    >
                      <IconFilm size={16} />
                      <span>{t('nav.review')}</span>
                    </button>
                  </div>
                ) : null}
              </div>
            )
          }
          if (item.action === 'batch-menu') {
            return (
              <div key={item.id} className="nav-tools" ref={batchRef}>
                <button
                  type="button"
                  className={mode === 'batch' || mode === 'automation' ? 'active' : undefined}
                  title={t(item.label)}
                  aria-haspopup="menu"
                  aria-expanded={openMenu === 'batch'}
                  onClick={() => setOpenMenu((cur) => (cur === 'batch' ? null : 'batch'))}
                >
                  <item.Icon size={16} />
                  <span>{t(item.label)}</span>
                </button>
                {openMenu === 'batch' ? (
                  <div className="nav-tools-menu" role="menu">
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'batch' ? 'active' : undefined}
                      onClick={() => { setOpenMenu(null); onModeChange?.('batch') }}
                    >
                      <IconBatch size={16} />
                      <span>{t('nav.batch')}</span>
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'automation' ? 'active' : undefined}
                      onClick={() => { setOpenMenu(null); onModeChange?.('automation') }}
                    >
                      <IconWand size={16} />
                      <span>{t('nav.automation')}</span>
                    </button>
                  </div>
                ) : null}
              </div>
            )
          }
          if (item.action === 'tools') {
            return (
              <div key={item.id} className="nav-tools" ref={toolsRef}>
                <button
                  type="button"
                  className={mode === 'download' || mode === 'cleaner' || mode === 'srt-image' || mode === 'srt-export' || mode === 'drawing' || mode === 'chat'
                    ? 'active'
                    : undefined}
                  title={t(item.label)}
                  aria-haspopup="menu"
                  aria-expanded={openMenu === 'tools'}
                  onClick={() => setOpenMenu((cur) => (cur === 'tools' ? null : 'tools'))}
                >
                  <item.Icon size={16} />
                  <span>{t(item.label)}</span>
                </button>
                {openMenu === 'tools' ? (
                  <div className="nav-tools-menu" role="menu">
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'download' ? 'active' : undefined}
                      onClick={() => {
                        setOpenMenu(null)
                        onModeChange?.('download')
                      }}
                    >
                      <IconDownload size={16} />
                      <span>{t('tools.downloadVideo')}</span>
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'cleaner' ? 'active' : undefined}
                      onClick={() => {
                        setOpenMenu(null)
                        onModeChange?.('cleaner')
                      }}
                    >
                      <IconWand size={16} />
                      <span>{t('tools.cleanVideo')}</span>
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'srt-image' ? 'active' : undefined}
                      onClick={() => {
                        setOpenMenu(null)
                        onModeChange?.('srt-image')
                      }}
                    >
                      <IconVideo size={16} />
                      <span>{t('tools.srtImage')}</span>
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'srt-export' ? 'active' : undefined}
                      onClick={() => {
                        setOpenMenu(null)
                        onModeChange?.('srt-export')
                      }}
                    >
                      <IconBook size={16} />
                      <span>{t('tools.exportSubtitles')}</span>
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'drawing' ? 'active' : undefined}
                      onClick={() => { setOpenMenu(null); onModeChange?.('drawing') }}
                    >
                      <IconWand size={16} />
                      <span>{t('tools.drawing')}</span>
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      className={mode === 'chat' ? 'active' : undefined}
                      onClick={() => {
                        setOpenMenu(null)
                        onModeChange?.('chat')
                      }}
                    >
                      <IconBook size={16} />
                      <span>{t('nav.chat')}</span>
                    </button>
                  </div>
                ) : null}
              </div>
            )
          }
          const active =
            item.mode != null
              ? mode === item.mode
              : item.action === 'config'
                ? false
                : false
          return (
            <button
              key={item.id}
              type="button"
              className={active ? 'active' : undefined}
              title={t(item.label)}
              onClick={() => {
                if (item.action === 'config') {
                  onOpenConfig?.()
                  return
                }
                if (item.mode) onModeChange?.(item.mode)
              }}
            >
              <item.Icon size={16} />
              <span>{t(item.label)}</span>
            </button>
          )
        })}
      </nav>
      <div className="hw" title={hardware.label}>
        <select className="locale-select" value={locale} onChange={(event) => onLocaleChange(event.target.value as AppLocale)} aria-label={t('header.interfaceLanguage')}>
          <option value="vi">VI</option>
          <option value="en">EN</option>
        </select>
        {licenseStatus && (
          <button
            type="button"
            className={`license-expiry${licenseStatus.remainingDay !== -1 && licenseStatus.remainingDay <= 7 ? ' is-warning' : ''}`}
            onClick={onOpenLicense}
            title={licenseStatus.expiresAt ? t('header.expires', { date: new Date(licenseStatus.expiresAt).toLocaleString(locale === 'vi' ? 'vi-VN' : 'en-US') }) : undefined}
          >
            {licenseStatus.remainingDay === -1 ? t('header.unlimited') : t('header.daysLeft', { count: licenseStatus.remainingDay })}
          </button>
        )}
        <span className="dot" aria-hidden="true" />
        <span className="hw-usage">{hardwareDisplay}</span>
        <button
          type="button"
          className="theme-toggle"
          onClick={onToggleTheme}
          title={dark ? t('header.switchLight') : t('header.switchDark')}
          aria-label={dark ? 'Light mode' : 'Dark mode'}
        >
          {dark ? <IconSun size={15} /> : <IconMoon size={15} />}
        </button>
      </div>
    </header>
  )
}
