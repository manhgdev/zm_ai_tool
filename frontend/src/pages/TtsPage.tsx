import TtsStudio from '@/features/tts/TtsStudio'

type Props = {
  voices: { id: string; name: string }[]
  voicesLoaded: boolean
  onBack: () => void
  onRefreshVoices?: (lang?: string, force?: boolean) => void
  isDesktopApp?: boolean
  sideOpen?: boolean
  onSideOpenChange?: (open: boolean) => void
  onOpenSetup?: () => void
}

export default function TtsPage({
  voices,
  voicesLoaded,
  onBack,
  onRefreshVoices,
  isDesktopApp,
  sideOpen,
  onSideOpenChange,
  onOpenSetup,
}: Props) {
  return (
    <TtsStudio
      voices={voices}
      voicesLoaded={voicesLoaded}
      onBack={onBack}
      onRefreshVoices={onRefreshVoices}
      isDesktopApp={isDesktopApp}
      sideOpen={sideOpen}
      onSideOpenChange={onSideOpenChange}
      onOpenSetup={onOpenSetup}
    />
  )
}
