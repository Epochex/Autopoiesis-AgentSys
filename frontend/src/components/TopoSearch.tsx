import { useEffect, useRef, useState } from 'react'
import type { Lang } from '../i18n'

/* ── 内网态势 · 设备搜索 — keyword → device, drill + focus its profile ──────────
 * GET /api/rca/device_search?q= searches every mined device across the topology's
 * subnets (ip / name / vendor / role / ports / live hostname). Picking a result
 * drills to its subnet and focuses the device so its profile/画像 opens. */

type Hit = { ip: string; cidr: string; name: string | null; vendor: string; role: string; threat: string; flows: number; topPorts: string[] }

export function TopoSearch({ lang, onPick }: { lang: Lang; onPick: (ip: string, cidr: string) => void }) {
  const zh = lang === 'zh'
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<Hit[]>([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const t = useRef<number | null>(null)

  useEffect(() => {
    if (t.current) window.clearTimeout(t.current)
    const s = q.trim()
    if (s.length < 1) { setHits([]); setOpen(false); return }
    setBusy(true)
    t.current = window.setTimeout(() => {
      fetch(`/api/rca/device_search?q=${encodeURIComponent(s)}`)
        .then((r) => r.json())
        .then((d) => { if (d && d.ok) { setHits(d.results); setOpen(true) } })
        .catch(() => {})
        .finally(() => setBusy(false))
    }, 220)
    return () => { if (t.current) window.clearTimeout(t.current) }
  }, [q])

  return (
    <div className="topo-search">
      <div className="ts-box">
        <span className="ts-ico">⌕</span>
        <input className="ts-input" value={q} placeholder={zh ? '搜索设备 · IP / 主机名 / 厂商 / 端口' : 'search device · ip / host / vendor / port'}
          onChange={(e) => setQ(e.target.value)} onFocus={() => hits.length && setOpen(true)} spellCheck={false} />
        {busy ? <span className="ts-busy" /> : q ? <button className="ts-clear" onClick={() => { setQ(''); setHits([]); setOpen(false) }}>✕</button> : null}
      </div>
      {open && hits.length ? (
        <div className="ts-results">
          <div className="ts-count">{hits.length} {zh ? '个匹配 · 点击定位并打开详情' : 'matches · click to locate + open details'}</div>
          {hits.map((h) => (
            <button key={h.cidr + h.ip} className={`ts-hit t-${h.threat}`} onClick={() => { onPick(h.ip, h.cidr); setOpen(false) }}>
              <span className="ts-hit-ip">{h.ip}</span>
              <span className="ts-hit-name">{h.name || h.vendor}</span>
              <span className="ts-hit-meta">{h.vendor}{h.role ? ` · ${h.role}` : ''} · {h.cidr}{h.topPorts.length ? ` · :${h.topPorts.join('/')}` : ''}</span>
              <span className={`ts-hit-th t-${h.threat}`}>{h.threat}</span>
            </button>
          ))}
        </div>
      ) : open && !busy && q.trim() ? (
        <div className="ts-results"><div className="ts-count">{zh ? '无匹配设备' : 'no device matched'}</div></div>
      ) : null}
    </div>
  )
}
