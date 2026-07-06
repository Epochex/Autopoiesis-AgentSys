import { useEffect, useRef, useState } from 'react'

const easeOut = (t: number) => 1 - Math.pow(1 - t, 3)

const GLYPHS = 'ｱｲｳｴｵｶｷ0123456789#%&@<>/\\▚▞░▒▓ΞΛΣ'

// Left-to-right scramble→resolve decode, 0.5s.
export function Scramble({ text, className, dur = 500 }: { text: string; className?: string; dur?: number }) {
  const [out, setOut] = useState(text)
  useEffect(() => {
    const start = performance.now()
    let raf = 0
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / dur)
      const reveal = Math.floor(p * text.length)
      let s = ''
      for (let i = 0; i < text.length; i++) {
        if (i < reveal || text[i] === ' ') s += text[i]
        else s += GLYPHS[(Math.floor(now / 35) + i * 3) % GLYPHS.length]
      }
      setOut(s)
      if (p < 1) raf = requestAnimationFrame(tick)
      else setOut(text)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [text, dur])
  return <span className={className}>{out}</span>
}

export function CountUp({ value, from: fromProp, dur = 1100 }: { value: number; from?: number; dur?: number }) {
  const [n, setN] = useState(fromProp ?? 0)
  const from = useRef(fromProp ?? 0)
  useEffect(() => {
    const start = performance.now()
    const a = fromProp != null ? fromProp : from.current
    let raf = 0
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / dur)
      setN(Math.round(a + (value - a) * easeOut(p)))
      if (p < 1) raf = requestAnimationFrame(tick)
      else from.current = value
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [value, fromProp, dur])
  return <>{n.toLocaleString('en-US')}</>
}

// Ring fills only when `active` flips true — lets the replay "reach" the diagnosis
// before the confidence resolves, so the motion carries meaning.
export function ConfidenceRing({ value, size = 104, active = true }: { value: number; size?: number; active?: boolean }) {
  const r = size / 2 - 8
  const circ = 2 * Math.PI * r
  const [dash, setDash] = useState(circ)
  useEffect(() => {
    const id = requestAnimationFrame(() => setDash(active ? circ * (1 - value) : circ))
    return () => cancelAnimationFrame(id)
  }, [value, circ, active])
  return (
    <svg width={size} height={size} className={`ring ${active ? 'on' : ''}`}>
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
        {active ? value.toFixed(2) : '· ·'}
      </text>
    </svg>
  )
}
