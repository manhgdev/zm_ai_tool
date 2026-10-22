import { useState, type ReactNode } from 'react'

/** Keep opened tools alive so requests, file inputs and SSE subscriptions survive navigation. */
export default function RetainedPages<T extends string>({ active, transient, children }: {
  active: T
  transient: readonly T[]
  children: (page: T, visible: boolean) => ReactNode
}) {
  const [opened, setOpened] = useState<T[]>([])
  const pages = opened.includes(active) || transient.includes(active) ? opened : [...opened, active]
  if (pages !== opened) setOpened(pages)
  // Give WebView a real layout box on first paint, not nested display: contents.
  const pageStyle = { minHeight: 0, minWidth: 0, gridTemplateRows: 'minmax(0, 1fr)', overflow: 'hidden' }
  return <div style={{ ...pageStyle, display: 'grid', gridArea: '2 / 1', height: '100%' }}>
    {pages.map(page => <div key={page} data-tool-page={page} hidden={page !== active}
      style={{ ...pageStyle, display: page === active ? 'grid' : 'none' }}>
      {children(page, page === active)}
    </div>)}
    {transient.includes(active) && <div key="transient" style={{ ...pageStyle, display: 'grid' }}>{children(active, true)}</div>}
  </div>
}
