import { useEffect, useState } from 'react'

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
