/* Visibility + motion-preference hooks.
 *
 * Both exist for the same reason: an animation that runs when nobody is looking
 * is worse than no animation. The console had two independent autoplays racing
 * on one page — the observatory (~17s) and the replay (~15s) — both starting at
 * mount. By the time a presenter reached either, it had already finished, and
 * the memory build from empty (the whole point) was never seen.
 */
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

/** Ref + whether the element is currently on screen past `threshold`. */
export function useInView<T extends Element>(threshold = 0.5) {
  const ref = useRef<T | null>(null)
  // Never gate content behind a missing API: if the browser cannot observe,
  // open as visible rather than silently never playing. Decided at init rather
  // than written from inside the effect, which would cascade a second render.
  const [inView, setInView] = useState(() => typeof IntersectionObserver === 'undefined')

  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver(
      (entries) => setInView(entries[0]?.isIntersecting ?? false),
      { threshold },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [threshold])

  return [ref, inView] as const
}

/* matchMedia is an external store, so subscribe to it as one. Mirroring it into
 * component state via an effect would write state on mount for every consumer. */
const MOTION_Q = '(prefers-reduced-motion: reduce)'
const subscribeMotion = (onChange: () => void) => {
  const mq = window.matchMedia(MOTION_Q)
  mq.addEventListener('change', onChange)
  return () => mq.removeEventListener('change', onChange)
}
const motionSnapshot = () => window.matchMedia(MOTION_Q).matches

/** Tracks the OS "reduce motion" setting, live. */
export function usePrefersReducedMotion() {
  return useSyncExternalStore(subscribeMotion, motionSnapshot, () => false)
}
