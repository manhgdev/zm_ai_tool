/** Domain types dùng chung cho TTS Studio + các panel con. */

export type Voice = {
  id: string
  name: string
  type?: string
  engine?: string
  mode?: string
  available?: boolean
  previewUrl?: string
  description?: string
  gender?: string
  language?: string
  accent?: string
  age?: string
  style?: string
  category?: string
  tags?: string[]
  favorite?: boolean
}

export type EngineStatus = {
  id?: string
  name?: string
  local?: boolean
  installed?: boolean
  ready?: boolean
  loaded?: boolean
  loadState?: string
  device?: string
  model?: string
  mode?: string
  models?: Array<{
    id: string
    name: string
    status: string
    format: string
    device: string
    bilingual: boolean
    features: string
    speed: string
    selectable?: boolean
    selected?: boolean
  }>
  version?: string
  message?: string
  presetCount?: number
  installHint?: string
  cloneRequiresPytorch?: boolean
}

export type HistoryItem = {
  id: string
  title?: string
  voice?: string
  voiceName?: string
  engine?: string
  duration?: number
  createdAt?: string
  audioUrl?: string
  mp3Url?: string
  srtUrl?: string
  zipUrl?: string
  text?: string
}
