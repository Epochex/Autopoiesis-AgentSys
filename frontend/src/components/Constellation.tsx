import { useMemo, useState } from 'react'
import type { Lang } from '../i18n'

export type CNode = { ip: string; label: string; role: string; severity: string; summary: string; out: number; deny: number; ports: string[] }
export type CLink = { src: string; dst: string; relation: string; strength: number }
export type CCluster = { name: string; members: string[]; note: string }
export type ConstData = { cidr: string; nodes: CNode[]; links: CLink[]; clusters: CCluster[]; model?: string }

type Pt = { x: number; y: number }
const W = 1320
const H = 760
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)
const sevColor = (s: string) => (s === 'high' ? '#ff5a6a' : s === 'medium' ? '#ffb347' : '#5fe4d1')
const rad = (out: number) => Math.max(7, Math.min(26, 5 + Math.log10(out + 1) * 5))

function curve(a: Pt, b: Pt) {
  const mx = (a.x + b.x) / 2
  const my = (a.y + b.y) / 2
  const dx = b.x - a.x
  const dy = b.y - a.y
  const nx = -dy
  const ny = dx
  const len = Math.hypot(nx, ny) || 1
  const off = Math.min(60, len * 0.18)
  return `M${a.x} ${a.y} Q ${mx + (nx / len) * off} ${my + (ny / len) * off} ${b.x} ${b.y}`
}

export function Constellation({ data, lang, onClose }: { data: ConstData; lang: Lang; onClose: () => void }) {
  const [hot, setHot] = useState<string | null>(null)

  const { pos, clusterViz } = useMemo(() => {
    const cx = W / 2
    const cy = H / 2 + 10
    const clusters = data.clusters.length ? data.clusters : [{ name: 'all', members: data.nodes.map((n) => n.ip), note: '' }]
    const ipCluster: Record<string, number> = {}
    clusters.forEach((c, ci) => c.members.forEach((m) => (ipCluster[m] = ci)))
    data.nodes.forEach((n) => { if (ipCluster[n.ip] === undefined) ipCluster[n.ip] = clusters.length })
    const groups = clusters.length + 1
    const R = Math.min(cx, cy) - 130
    const pos: Record<string, Pt> = {}
    const clusterViz: { name: string; note: string; c: Pt; r: number; sev: string }[] = []
    for (let ci = 0; ci < groups; ci++) {
      const a = (ci / groups) * Math.PI * 2 - Math.PI / 2
      const center: Pt = { x: cx + Math.cos(a) * R, y: cy + Math.sin(a) * R }
      const members = data.nodes.filter((n) => ipCluster[n.ip] === ci)
      if (!members.length) continue
      const ring = 26 + members.length * 9
      members.forEach((m, mi) => {
        const ma = (mi / members.length) * Math.PI * 2 + ci
        const jr = ring * (0.7 + 0.3 * ((mi * 37) % 10) / 10)
        pos[m.ip] = { x: center.x + Math.cos(ma) * jr, y: center.y + Math.sin(ma) * jr }
      })
      const hi = members.some((m) => m.severity === 'high')
      const cl = data.clusters[ci]
      clusterViz.push({ name: cl?.name ?? '其他', note: cl?.note ?? '', c: center, r: ring + 34, sev: hi ? 'high' : members.some((m) => m.severity === 'medium') ? 'medium' : 'low' })
    }
    return { pos, clusterViz }
  }, [data])

  const neigh = useMemo(() => {
    if (!hot) return new Set<string>()
    const s = new Set<string>([hot])
    data.links.forEach((l) => { if (l.src === hot) s.add(l.dst); if (l.dst === hot) s.add(l.src) })
    return s
  }, [hot, data.links])

  const hotNode = data.nodes.find((n) => n.ip === hot)

  return (
    <section className="const-section">
      <div className="const-head">
        <span className="const-kicker">{lang === 'zh' ? 'DeepSeek 全子网关系建模' : 'DeepSeek subnet model'} · {data.cidr} · {data.nodes.length} {lang === 'zh' ? '设备' : 'devices'} · {data.clusters.length} {lang === 'zh' ? '簇' : 'clusters'}</span>
        <button className="tc-x" onClick={onClose}>✕</button>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="const-svg" preserveAspectRatio="xMidYMid meet">
        <defs>
          <filter id="glow" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="4" result="b" />
            <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <radialGradient id="halo-high"><stop offset="0%" stopColor="rgba(255,90,106,0.16)" /><stop offset="100%" stopColor="rgba(255,90,106,0)" /></radialGradient>
          <radialGradient id="halo-medium"><stop offset="0%" stopColor="rgba(255,179,71,0.12)" /><stop offset="100%" stopColor="rgba(255,179,71,0)" /></radialGradient>
          <radialGradient id="halo-low"><stop offset="0%" stopColor="rgba(95,228,209,0.1)" /><stop offset="100%" stopColor="rgba(95,228,209,0)" /></radialGradient>
        </defs>

        {clusterViz.map((c, i) => (
          <g key={i} className="cl-halo" style={{ ['--d' as string]: `${i * 0.08}s` }}>
            <circle cx={c.c.x} cy={c.c.y} r={c.r} fill={`url(#halo-${c.sev})`} />
            <text x={c.c.x} y={c.c.y - c.r - 6} className="cl-name" textAnchor="middle">{c.name}</text>
          </g>
        ))}

        {data.links.map((l, i) => {
          const a = pos[l.src]; const b = pos[l.dst]
          if (!a || !b) return null
          const on = !hot || neigh.has(l.src) && neigh.has(l.dst)
          const lit = hot && (l.src === hot || l.dst === hot)
          return (
            <g key={i} className={`cl-link ${on ? '' : 'dim'}`}>
              <path d={curve(a, b)} className={`cl-edge ${lit ? 'lit' : ''}`} style={{ strokeWidth: 0.6 + l.strength * 0.7 }} />
              {(lit || l.strength >= 3) ? (
                <circle r="2.4" className="cl-particle">
                  <animateMotion dur={`${2.6 - l.strength * 0.4}s`} repeatCount="indefinite" path={curve(a, b)} />
                </circle>
              ) : null}
            </g>
          )
        })}

        {data.nodes.map((n, i) => {
          const p = pos[n.ip]; if (!p) return null
          const on = !hot || neigh.has(n.ip)
          const r = rad(n.out)
          return (
            <g key={n.ip} className={`cl-node ${on ? '' : 'dim'} ${hot === n.ip ? 'sel' : ''} ${n.severity}`}
              style={{ ['--d' as string]: `${0.2 + i * 0.04}s`, transformOrigin: `${p.x}px ${p.y}px` }}
              onMouseEnter={() => setHot(n.ip)} onMouseLeave={() => setHot(null)}>
              <circle cx={p.x} cy={p.y} r={r + 5} className="cl-aura" fill={sevColor(n.severity)} />
              <circle cx={p.x} cy={p.y} r={r} className="cl-core" fill={sevColor(n.severity)} fillOpacity={0.22} stroke={sevColor(n.severity)} filter="url(#glow)" />
              <text x={p.x} y={p.y + r + 13} className="cl-ip" textAnchor="middle">{n.label || n.role}</text>
            </g>
          )
        })}
      </svg>
      {hotNode ? (
        <div className="const-profile">
          <strong>{hotNode.ip}</strong>
          <span className="cp-label" style={{ color: sevColor(hotNode.severity) }}>{hotNode.label} · {hotNode.severity}</span>
          <span>{hotNode.summary}</span>
          <span className="cp-meta">out {short(hotNode.out)} · deny {short(hotNode.deny)} · {hotNode.ports.map((p) => `:${p}`).join(' ')}</span>
        </div>
      ) : (
        <div className="const-profile hint">{lang === 'zh' ? '悬停设备查看画像，亮线为其关系边' : 'hover a device for its profile and relations'} · {data.model}</div>
      )}
    </section>
  )
}
