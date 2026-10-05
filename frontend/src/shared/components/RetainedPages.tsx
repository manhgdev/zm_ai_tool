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
  return <>
    {pages.map(page => <div key={page} data-tool-page={page} hidden={page !== active}
      style={{ display: page === active ? 'contents' : 'none' }}>
      {children(page, page === active)}
    </div>)}
    {transient.includes(active) && <div key="transient" style={{ display: 'contents' }}>{children(active, true)}</div>}
  </>
}
