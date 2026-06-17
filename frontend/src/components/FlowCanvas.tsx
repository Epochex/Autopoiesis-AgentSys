import { useMemo } from 'react'
import type { DataStats } from '../types'

type Pt = { x: number; y: number }
const bez = (a: Pt, b: Pt) => `M${a.x} ${a.y} C ${a.x + 150} ${a.y}, ${b.x - 150} ${b.y}, ${b.x} ${b.y}`
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)

// which flow group drove the active diagnosis
function activeGroup(key: string): 'attack' | 'deny' | 'health' {
  if (key === 'admin_bruteforce_lockout') return 'attack'
  if (key === 'internal_policy_deny_expected' || key === 'device_service_port_probe_contained') return 'deny'
  return 'health'
}

function Pulses({ path, tone, n, dim }: { path: string; tone: string; n: number; dim: boolean }) {
  return (
    <>
      <path d={path} className={`flow-line ${tone} ${dim ? 'dim' : ''}`} id={path.length + tone} />
      {Array.from({ length: dim ? 1 : n }).map((_, i) => (
        <circle key={i} r={dim ? 1.6 : 2.6} className={`pulse ${tone} ${dim ? 'dim' : ''}`}>
          <animateMotion dur={`${2.4 + (i % 3) * 0.5}s`} repeatCount="indefinite" begin={`${i * 0.7}s`} path={path} />
        </circle>
      ))}
    </>
  )
}

export function FlowCanvas({ stats, activeKey }: { stats: DataStats; activeKey: string }) {
  const g = activeGroup(activeKey)
  const { atk, host, port, gateway, sink, cons } = useMemo(() => {
    return {
      atk: stats.topAttackerSrc.slice(0, 3).map((d, i) => ({ ip: d[0], v: d[1], p: { x: 96, y: 78 + i * 62 } })),
      host: stats.topDenySrc.slice(0, 3).map((d, i) => ({ ip: d[0], v: d[1], p: { x: 96, y: 356 + i * 62 } })),
      port: stats.topDenyPorts.slice(0, 3).map((d, i) => ({ pt: d[0], v: d[1], p: { x: 838, y: 360 + i * 64 } })),
      gateway: { x: 492, y: 268 } as Pt,
      sink: { x: 824, y: 120 } as Pt,
      cons: { x: 940, y: 120 } as Pt,
    }
  }, [stats])

  return (
    <svg viewBox="0 0 1000 540" className="flow-canvas" preserveAspectRatio="xMidYMid meet">
      <defs>
        <radialGradient id="gw" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgba(95,228,209,0.35)" />
          <stop offset="100%" stopColor="rgba(95,228,209,0)" />
        </radialGradient>
      </defs>

      {/* edges + flowing pulses */}
      {atk.map((a, i) => (
        <Pulses key={`a${i}`} path={bez(a.p, gateway)} tone="t-attack" n={3} dim={g !== 'attack'} />
      ))}
      {host.map((h, i) => (
        <Pulses key={`h${i}`} path={bez(h.p, gateway)} tone="t-deny" n={2} dim={g !== 'deny'} />
      ))}
      {port.map((p, i) => (
        <Pulses key={`p${i}`} path={bez(gateway, p.p)} tone="t-deny" n={2} dim={g !== 'deny'} />
      ))}
      <Pulses path={bez(gateway, sink)} tone="t-flow" n={3} dim={g !== 'health'} />
      <Pulses path={bez(sink, cons)} tone="t-flow" n={2} dim={false} />

      {/* gateway core */}
      <circle cx={gateway.x} cy={gateway.y} r="62" fill="url(#gw)" />
      <g className="node gw-node">
        <rect x={gateway.x - 46} y={gateway.y - 26} width="92" height="52" rx="2" />
        <text x={gateway.x} y={gateway.y - 4} className="n-title">FortiGate</text>
        <text x={gateway.x} y={gateway.y + 12} className="n-sub">192.168.1.1</text>
      </g>

      {/* endpoints — markers + minimal labels */}
      {atk.map((a, i) => (
        <g key={`an${i}`} className={`node ${g === 'attack' ? '' : 'node-dim'}`}>
          <rect x={a.p.x - 7} y={a.p.y - 7} width="14" height="14" className="m-attack" transform={`rotate(45 ${a.p.x} ${a.p.y})`} />
          <text x={a.p.x + 16} y={a.p.y - 1} className="n-ip" textAnchor="start">{a.ip}</text>
          <text x={a.p.x + 16} y={a.p.y + 12} className="n-v" textAnchor="start">{short(a.v)}</text>
        </g>
      ))}
      {host.map((h, i) => (
        <g key={`hn${i}`} className={`node ${g === 'deny' ? '' : 'node-dim'}`}>
          <rect x={h.p.x - 6} y={h.p.y - 6} width="12" height="12" className="m-host" />
          <text x={h.p.x + 16} y={h.p.y - 1} className="n-ip" textAnchor="start">{h.ip}</text>
          <text x={h.p.x + 16} y={h.p.y + 12} className="n-v" textAnchor="start">{short(h.v)}</text>
        </g>
      ))}
      {port.map((p, i) => (
        <g key={`pn${i}`} className={`node ${g === 'deny' ? '' : 'node-dim'}`}>
          <rect x={p.p.x - 6} y={p.p.y - 6} width="12" height="12" className="m-port" />
          <text x={p.p.x - 16} y={p.p.y - 1} className="n-ip" textAnchor="end">:{p.pt}</text>
          <text x={p.p.x - 16} y={p.p.y + 12} className="n-v" textAnchor="end">{short(p.v)}</text>
        </g>
      ))}
      <g className="node">
        <rect x={sink.x - 7} y={sink.y - 7} width="14" height="14" className="m-flow" />
        <text x={sink.x} y={sink.y - 16} className="n-ip">R230</text>
      </g>
      <g className="node">
        <circle cx={cons.x} cy={cons.y} r="8" className="m-cons" />
        <text x={cons.x} y={cons.y - 16} className="n-ip">selfevo</text>
      </g>
    </svg>
  )
}
