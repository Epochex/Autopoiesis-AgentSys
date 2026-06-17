import { useEffect, useRef, useState } from 'react'
import type { StageTelemetry } from '../types'
import { formatPreciseDurationMs } from '../utils/time'

interface AnimatedDurationCounterProps {
  durationMs?: number | null
  mode: StageTelemetry['mode']
  animationKey: string
  degradedReason?: string
}

export function AnimatedDurationCounter({
  durationMs,
  mode,
  animationKey,
  degradedReason,
}: AnimatedDurationCounterProps) {
  const frameRef = useRef<number | null>(null)
  const [display, setDisplay] = useState(() => ({
    animationKey,
    valueMs: durationMs && durationMs > 0 ? 0 : durationMs ?? 0,
  }))

  useEffect(() => {
    if (frameRef.current !== null) {
      window.cancelAnimationFrame(frameRef.current)
      frameRef.current = null
    }

    if (
      mode === 'reserved' ||
      mode === 'timestamp' ||
      mode === 'status' ||
      durationMs === null ||
      durationMs === undefined
    ) {
      return
    }

    const safeDuration = Math.max(durationMs, 0)
    const animationWindow = Math.min(1800, Math.max(420, safeDuration * 1.6))
    const startedAt = performance.now()

    const tick = (now: number) => {
      const progress = Math.min(1, (now - startedAt) / animationWindow)
      setDisplay({
        animationKey,
        valueMs: safeDuration * progress,
      })
      if (progress < 1) {
        frameRef.current = window.requestAnimationFrame(tick)
      }
    }

    frameRef.current = window.requestAnimationFrame(tick)

    return () => {
      if (frameRef.current !== null) {
        window.cancelAnimationFrame(frameRef.current)
        frameRef.current = null
      }
    }
  }, [animationKey, durationMs, mode])

  const displayMs = display.animationKey === animationKey ? display.valueMs : 0

  if (mode === 'reserved') {
    return (
      <div className="phase-timer is-reserved">
        <span>action timer</span>
        <strong>manual boundary</strong>
      </div>
    )
  }

  if (degradedReason && (mode === 'duration' || mode === 'gate')) {
    return (
      <div className="phase-timer is-degraded">
        <span>action timer</span>
        <strong>gateway drift</strong>
        <small>{degradedReason}</small>
      </div>
    )
  }

  if (mode === 'timestamp' || mode === 'status') {
    return (
      <div className="phase-timer">
        <span>action timer</span>
        <strong>{formatPreciseDurationMs(0)}</strong>
        <small>instant checkpoint</small>
      </div>
    )
  }

  if (durationMs === null || durationMs === undefined) {
    return (
      <div className="phase-timer is-pending">
        <span>action timer</span>
        <strong>pending</strong>
        <small>telemetry unavailable</small>
      </div>
    )
  }

  return (
    <div className="phase-timer">
      <span>action timer</span>
      <strong>{formatPreciseDurationMs(displayMs)}</strong>
      <small>0.000 s {'->'} measured transition</small>
    </div>
  )
}
