import { useEffect, useState } from 'react'

/* One place that answers "does this viewer want motion?".
 *
 * CSS can honour the setting on its own, but SVG SMIL (`<animateMotion>`)
 * cannot be stopped from a stylesheet — so anything driven by SMIL has to ask
 * in JS and simply not render the animation. That is the reason this exists
 * as a module rather than living only in media queries. */

const QUERY = '(prefers-reduced-motion: reduce)'

export function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia(QUERY).matches
  )
}

/** Subscribed: a viewer who changes the setting should see it take effect. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(prefersReducedMotion)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia(QUERY)
    const onChange = () => setReduced(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return reduced
}
