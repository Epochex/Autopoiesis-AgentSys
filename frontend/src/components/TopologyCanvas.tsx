import { useMemo } from 'react'
import type { DataStats, Device, Subnet, Topology } from '../types'

type Pt = { x: number; y: number }
const bez = (a: Pt, b: Pt) => `M${a.x} ${a.y} C ${(a.x + b.x) / 2} ${a.y}, ${(a.x + b.x) / 2} ${b.y}, ${b.x} ${b.y}`
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)
const weight = (f: number) => Math.max(1, Math.min(5.5, 1 + Math.log10(f + 1) * 0.7))
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

function group(key: string): 'attack' | 'deny' | 'health' {
  if (key === 'admin_bruteforce_lockout') return 'attack'
  if (key === 'internal_policy_deny_expected' || key === 'device_service_port_probe_contained') return 'deny'
  return 'health'
}

function Edge({ a, b, tone, flows, dim, delay, tempo }: { a: Pt; b: Pt; tone: string; flows: number; dim: boolean; delay: number; tempo: number }) {
  const d = bez(a, b)
  const n = dim ? 1 : Math.max(1, Math.min(7, Math.round(Math.log10(flows + 1) - 1)))
  const dur = Math.max(0.7, (Math.max(1.4, 4.2 - Math.log10(flows + 1) * 0.42)) / tempo)
  return (
    <>
      <path d={d} className={`flow-line ${tone} ${dim ? 'dim' : ''} appear`} style={{ strokeWidth: weight(flows), animationDelay: `${delay}s` }} />
      {Array.from({ length: n }).map((_, i) => (
        <circle key={i} r={dim ? 1.4 : 2.4} className={`pulse ${tone} ${dim ? 'dim' : ''}`}>
          <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`${(i * dur) / n}s`} path={d} />
        </circle>
      ))}
    </>
  )
}

const THREAT_DX: Record<string, number> = { high: 0, watch: 30, ok: 58 }

export function TopologyCanvas({
  topo,
  stats,
  activeKey,
  drillSub,
  drillDev,
  tempo,
  onSub,
  onDev,
}: {
  topo: Topology
  stats: DataStats
  activeKey: string
  drillSub: string | null
  drillDev: string | null
  tempo: number
  onSub: (s: Subnet | null) => void
  onDev: (d: Device | null, cidr: string) => void
}) {
  const g = group(activeKey)
  const core: Pt = { x: 452, y: 236 }

  const layout = useMemo(() => {
    const atk = stats.topAttackerSrc.slice(0, 3).map((d, i) => ({ ip: d[0], v: d[1], p: { x: 70, y: 70 + i * 66 } as Pt }))
    const lan = topo.interfaces.filter((it) => it.kind === 'lan')
    const ifs = lan.map((it, i) => {
      const p: Pt = { x: 720, y: 70 + i * 92 }
      const sub = topo.subnets.find((s) => s.intf === it.name && s.hosts > 1)
      return { it, p, sub, subP: { x: 912, y: p.y } as Pt }
    })
    return { atk, ifs }
  }, [topo, stats])

  return (
    <svg viewBox="0 0 1360 470" className="flow-canvas" preserveAspectRatio="xMidYMid meet">
      <defs>
        <radialGradient id="gw" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgba(95,228,209,0.32)" />
          <stop offset="100%" stopColor="rgba(95,228,209,0)" />
        </radialGradient>
      </defs>

      {layout.atk.map((a, i) => (
        <Edge key={`ea${i}`} a={a.p} b={core} tone="t-attack" flows={a.v} dim={g !== 'attack'} delay={0.1 + i * 0.05} tempo={tempo} />
      ))}
      {layout.ifs.map((f, i) => {
        const fade = (g === 'attack' ? 1 : 0) || (drillSub && f.sub?.cidr !== drillSub ? 1 : 0)
        return (
          <g key={`if${i}`}>
            <Edge a={core} b={f.p} tone="t-flow" flows={f.it.flows} dim={!!fade} delay={0.4 + i * 0.06} tempo={tempo} />
            {f.sub ? <Edge a={f.p} b={f.subP} tone="t-flow" flows={f.sub.flows} dim={!!fade} delay={0.6 + i * 0.06} tempo={tempo} /> : null}
          </g>
        )
      })}

      <circle cx={core.x} cy={core.y} r="64" fill="url(#gw)" />
      <g className="node gw-node appear" style={{ animationDelay: '0.3s' }}>
        <rect x={core.x - 48} y={core.y - 27} width="96" height="54" rx="2" />
        <text x={core.x} y={core.y - 4} className="n-title">{topo.core.name}</text>
        <text x={core.x} y={core.y + 13} className="n-sub">{topo.core.ip}</text>
      </g>

      {layout.atk.map((a, i) => (
        <g key={`an${i}`} className={`node appear ${g === 'attack' ? '' : 'node-dim'}`} style={{ animationDelay: `${i * 0.05}s` }}>
          <rect x={a.p.x - 7} y={a.p.y - 7} width="14" height="14" className="m-attack" transform={`rotate(45 ${a.p.x} ${a.p.y})`} />
          <text x={a.p.x + 16} y={a.p.y - 1} className="n-ip" textAnchor="start">{a.ip}</text>
          <text x={a.p.x + 16} y={a.p.y + 12} className="n-v" textAnchor="start">{short(a.v)}</text>
        </g>
      ))}
      <text x={70} y={36} className="zone-tag appear">WAN1 · {short(stats.distinctSrc)} src</text>

      {layout.ifs.map((f, i) => {
        const dimIf = g === 'attack' || (drillSub && f.sub?.cidr !== drillSub)
        const highThreat = f.sub?.devices?.some((dv) => dv.threat === 'high')
        const open = drillSub === f.sub?.cidr
        return (
          <g key={`ifn${i}`} className={`node appear ${dimIf ? 'node-dim' : ''}`} style={{ animationDelay: `${0.5 + i * 0.06}s` }}>
            <rect x={f.p.x - 52} y={f.p.y - 16} width="104" height="32" className="m-intf" />
            <text x={f.p.x} y={f.p.y - 1} className="n-intf">{f.it.name}</text>
            <text x={f.p.x} y={f.p.y + 12} className="n-sub">{short(f.it.flows)} flows</text>
            {f.sub ? (
              <g className={`subnet ${open ? 'open' : ''}`} onClick={() => onSub(open ? null : f.sub!)} style={{ cursor: 'pointer' }}>
                <rect x={f.subP.x - 9} y={f.subP.y - 9} width="18" height="18" className="m-host" />
                {highThreat ? <circle cx={f.subP.x + 9} cy={f.subP.y - 9} r="3.5" className="threat-pip" /> : null}
                <text x={f.subP.x + 18} y={f.subP.y - 1} className="n-ip" textAnchor="start">{f.sub.cidr}</text>
                <text x={f.subP.x + 18} y={f.subP.y + 12} className="n-v amber" textAnchor="start">{f.sub.hosts} hosts {open ? '▾' : '▸'}</text>
              </g>
            ) : null}
          </g>
        )
      })}

      {/* level-2/3 tree: devices branching from the open subnet, ports as leaves */}
      {layout.ifs.map((f) => {
        if (!f.sub || drillSub !== f.sub.cidr) return null
        const devs = (f.sub.devices ?? []).slice(0, 7)
        const baseY = clamp(f.subP.y, 150, 320)
        const subNode: Pt = { x: f.subP.x, y: f.subP.y }
        return (
          <g key={`tree-${f.sub.cidr}`}>
            {devs.map((dv, j) => {
              const dy = clamp(baseY + (j - (devs.length - 1) / 2) * 44, 26, 444)
              const dp: Pt = { x: 1066 + (THREAT_DX[dv.threat] ?? 40), y: dy }
              const open = drillDev === dv.ip
              const rad = dv.threat === 'high' ? 8 : dv.threat === 'watch' ? 6 : 5
              return (
                <g key={dv.ip} className="branch-in">
                  <path d={bez(subNode, dp)} className={`branch ${dv.threat}`} />
                  <g className="dev-node" onClick={() => onDev(open ? null : dv, f.sub!.cidr)} style={{ cursor: 'pointer' }}>
                    <circle cx={dp.x} cy={dp.y} r={rad} className={`m-dev ${dv.threat} ${open ? 'sel' : ''}`} />
                    <text x={dp.x + 14} y={dp.y - 1} className="n-ip" textAnchor="start">{dv.ip}</text>
                    <text x={dp.x + 14} y={dp.y + 12} className={`n-v ${dv.threat === 'high' ? '' : 'amber'}`} textAnchor="start">{short(dv.deny)} deny</text>
                  </g>
                  {open
                    ? dv.top_ports.slice(0, 3).map((pt, k) => {
                        const lp: Pt = { x: dp.x + 150, y: clamp(dp.y + (k - (dv.top_ports.length - 1) / 2) * 26, 20, 450) }
                        return (
                          <g key={pt} className="branch-in leaf">
                            <path d={bez(dp, lp)} className={`branch ${dv.threat}`} />
                            <rect x={lp.x - 4} y={lp.y - 4} width="8" height="8" className={`m-leaf ${dv.threat}`} />
                            <text x={lp.x + 12} y={lp.y + 3} className="n-leaf" textAnchor="start">:{pt}</text>
                          </g>
                        )
                      })
                    : null}
                </g>
              )
            })}
          </g>
        )
      })}
    </svg>
  )
}
