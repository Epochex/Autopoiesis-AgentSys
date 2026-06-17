import { useEffect, useRef, useState } from 'react'

const easeOut = (t: number) => 1 - Math.pow(1 - t, 3)

export function CountUp({ value, dur = 1100 }: { value: number; dur?: number }) {
  const [n, setN] = useState(0)
  const from = useRef(0)
  useEffect(() => {
    const start = performance.now()
    const a = from.current
    let raf = 0
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / dur)
      setN(Math.round(a + (value - a) * easeOut(p)))
      if (p < 1) raf = requestAnimationFrame(tick)
      else from.current = value
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [value, dur])
  return <>{n.toLocaleString('en-US')}</>
}

export function ConfidenceRing({ value, size = 92 }: { value: number; size?: number }) {
  const r = size / 2 - 7
  const circ = 2 * Math.PI * r
  const [dash, setDash] = useState(circ)
  useEffect(() => {
    const id = requestAnimationFrame(() => setDash(circ * (1 - value)))
    return () => cancelAnimationFrame(id)
  }, [value, circ])
  return (
    <svg width={size} height={size} className="ring">
      <circle cx={size / 2} cy={size / 2} r={r} className="ring-track" />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        className="ring-arc"
        strokeDasharray={circ}
        strokeDashoffset={dash}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
      <text x="50%" y="52%" className="ring-num">
        {value.toFixed(2)}
      </text>
    </svg>
  )
}
