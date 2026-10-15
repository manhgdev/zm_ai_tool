import { useEffect, useRef, type UIEvent, type WheelEvent } from 'react'

/**
 * Follow new log output at the bottom, but never fight the user:
 * wheel-up / scrollbar drag pauses follow; returning near the bottom resumes.
 */
export function useStickToBottomScroll(content: string | undefined, thresholdPx = 40) {
  const ref = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const holding = useRef(false)
  const programmatic = useRef(false)

  useEffect(() => {
    if (!content) stick.current = true
  }, [content])

  useEffect(() => {
    const node = ref.current
    if (!node || !content || !stick.current || holding.current) return
    programmatic.current = true
    node.scrollTop = node.scrollHeight
    requestAnimationFrame(() => {
      programmatic.current = false
    })
  }, [content])

  function syncStickFromElement(el: HTMLElement) {
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight
    stick.current = distance <= thresholdPx
  }

  function onScroll(event: UIEvent<HTMLDivElement>) {
    if (programmatic.current) return
    syncStickFromElement(event.currentTarget)
  }

  function onWheel(event: WheelEvent<HTMLDivElement>) {
    if (event.deltaY < 0) stick.current = false
  }

  function onPointerDown() {
    holding.current = true
    stick.current = false
  }

  function onPointerUp() {
    holding.current = false
    const el = ref.current
    if (el) syncStickFromElement(el)
  }

  return { ref, onScroll, onWheel, onPointerDown, onPointerUp }
}
