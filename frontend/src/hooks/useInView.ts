/* Visibility + motion-preference hooks.
 *
 * Both exist for the same reason: an animation that runs when nobody is looking
 * is worse than no animation. The console had two independent autoplays racing
 * on one page — the observatory (~17s) and the replay (~15s) — both starting at
 * mount. By the time a presenter reached either, it had already finished, and
 * the memory build from empty (the whole point) was never seen.
 */
import { useEffect, useRef, useState } from 'react'

/** Ref + whether the element is currently on screen past `threshold`. */
export function useInView<T extends Element>(threshold = 0.5) {
  const ref = useRef<T | null>(null)
  const [inView, setInView] = useState(false)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    // Never gate content behind a missing API: if the browser can't observe,
    // treat it as visible rather than silently never playing.
    if (typeof IntersectionObserver === 'undefined') {
      setInView(true)
      return
    }
    const io = new IntersectionObserver(
      (entries) => setInView(entries[0]?.isIntersecting ?? false),
      { threshold },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [threshold])

  return [ref, inView] as const
}

/** Tracks the OS "reduce motion" setting, live. */
export function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(false)
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    setReduced(mq.matches)
    const onChange = () => setReduced(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return reduced
}
