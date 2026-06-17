import { useMemo } from 'react'
import type { DataStats, Topology } from '../types'

type Pt = { x: number; y: number }
const bez = (a: Pt, b: Pt) => `M${a.x} ${a.y} C ${(a.x + b.x) / 2} ${a.y}, ${(a.x + b.x) / 2} ${b.y}, ${b.x} ${b.y}`
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)
const weight = (f: number) => Math.max(1, Math.min(5, 1 + Math.log10(f + 1) * 0.7))

function group(key: string): 'attack' | 'deny' | 'health' {
  if (key === 'admin_bruteforce_lockout') return 'attack'
  if (key === 'internal_policy_deny_expected' || key === 'device_service_port_probe_contained') return 'deny'
  return 'health'
}

function Edge({ a, b, tone, w, dim, pulses = 2 }: { a: Pt; b: Pt; tone: string; w: number; dim: boolean; pulses?: number }) {
  const d = bez(a, b)
  return (
    <>
      <path d={d} className={`flow-line ${tone} ${dim ? 'dim' : ''}`} style={{ strokeWidth: w }} />
      {Array.from({ length: dim ? 1 : pulses }).map((_, i) => (
        <circle key={i} r={dim ? 1.4 : 2.4} className={`pulse ${tone} ${dim ? 'dim' : ''}`}>
          <animateMotion dur={`${2.6 + (i % 3) * 0.6}s`} repeatCount="indefinite" begin={`${i * 0.9}s`} path={d} />
        </circle>
      ))}
    </>
  )
}

export function TopologyCanvas({ topo, stats, activeKey }: { topo: Topology; stats: DataStats; activeKey: string }) {
  const g = group(activeKey)
  const core: Pt = { x: 452, y: 236 }

  const layout = useMemo(() => {
    const atk = stats.topAttackerSrc.slice(0, 3).map((d, i) => ({ ip: d[0], v: d[1], p: { x: 70, y: 70 + i * 66 } as Pt }))
    const lan = topo.interfaces.filter((it) => it.kind === 'lan')
    const ifs = lan.map((it, i) => {
      const p: Pt = { x: 740, y: 70 + i * 92 }
      const sub = topo.subnets.find((s) => s.intf === it.name)
      return { it, p, sub, subP: { x: 962, y: p.y } as Pt }
    })
    return { atk, ifs }
  }, [topo, stats])

  return (
    <svg viewBox="0 0 1120 470" className="flow-canvas" preserveAspectRatio="xMidYMid meet">
      <defs>
        <radialGradient id="gw" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgba(95,228,209,0.32)" />
          <stop offset="100%" stopColor="rgba(95,228,209,0)" />
        </radialGradient>
      </defs>

      {/* external → core (wan1) */}
      {layout.atk.map((a, i) => (
        <Edge key={`ea${i}`} a={a.p} b={core} tone="t-attack" w={2} dim={g !== 'attack'} pulses={3} />
      ))}
      {/* core → interface → subnet */}
      {layout.ifs.map((f, i) => (
        <g key={`if${i}`}>
          <Edge a={core} b={f.p} tone="t-flow" w={weight(f.it.flows)} dim={g === 'attack'} />
          {f.sub ? <Edge a={f.p} b={f.subP} tone="t-flow" w={weight(f.sub.flows)} dim={g === 'attack'} /> : null}
        </g>
      ))}

      {/* core */}
      <circle cx={core.x} cy={core.y} r="64" fill="url(#gw)" />
      <g className="node gw-node">
        <rect x={core.x - 48} y={core.y - 27} width="96" height="54" rx="2" />
        <text x={core.x} y={core.y - 4} className="n-title">{topo.core.name}</text>
        <text x={core.x} y={core.y + 13} className="n-sub">{topo.core.ip}</text>
      </g>

      {/* attackers */}
      {layout.atk.map((a, i) => (
        <g key={`an${i}`} className={`node ${g === 'attack' ? '' : 'node-dim'}`}>
          <rect x={a.p.x - 7} y={a.p.y - 7} width="14" height="14" className="m-attack" transform={`rotate(45 ${a.p.x} ${a.p.y})`} />
          <text x={a.p.x + 16} y={a.p.y - 1} className="n-ip" textAnchor="start">{a.ip}</text>
          <text x={a.p.x + 16} y={a.p.y + 12} className="n-v" textAnchor="start">{short(a.v)}</text>
        </g>
      ))}
      <text x={70} y={36} className="zone-tag">WAN1 · {short(stats.distinctSrc)} src</text>

      {/* interfaces + subnets */}
      {layout.ifs.map((f, i) => (
        <g key={`ifn${i}`} className={`node ${g === 'attack' ? 'node-dim' : ''}`}>
          <rect x={f.p.x - 52} y={f.p.y - 16} width="104" height="32" className="m-intf" />
          <text x={f.p.x} y={f.p.y - 1} className="n-intf">{f.it.name}</text>
          <text x={f.p.x} y={f.p.y + 12} className="n-sub">{short(f.it.flows)} flows</text>
          {f.sub ? (
            <g>
              <rect x={f.subP.x - 8} y={f.subP.y - 8} width="16" height="16" className="m-host" />
              <text x={f.subP.x + 16} y={f.subP.y - 1} className="n-ip" textAnchor="start">{f.sub.cidr}</text>
              <text x={f.subP.x + 16} y={f.subP.y + 12} className="n-v amber" textAnchor="start">{f.sub.hosts} hosts</text>
            </g>
          ) : null}
        </g>
      ))}
    </svg>
  )
}
