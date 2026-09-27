import { useEffect, useState, type Dispatch, type SetStateAction } from 'react'

const remountState = new Map<string, unknown>()

/** Keep transient UI state while a top-level page is unmounted during navigation. */
export function useRemountState<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    if (remountState.has(key)) return remountState.get(key) as T
    return typeof initial === 'function' ? (initial as () => T)() : initial
  })

  useEffect(() => {
    remountState.set(key, value)
  }, [key, value])

  return [value, setValue]
}
