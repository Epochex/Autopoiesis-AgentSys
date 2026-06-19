import { useMemo, useRef, useState } from 'react'
import type { DataStats, Device, Subnet, Topology } from '../types'
import { Scramble } from './Motion'
import { Analyzing, type Threat } from './ThreatCard'
import type { Lang } from '../i18n'

type Pt = { x: number; y: number }
const VBW = 1360
const VBH = 1000
const bez = (a: Pt, b: Pt) => `M${a.x} ${a.y} C ${(a.x + b.x) / 2} ${a.y}, ${(a.x + b.x) / 2} ${b.y}, ${b.x} ${b.y}`
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)
const weight = (f: number) => Math.max(1, Math.min(5.5, 1 + Math.log10(f + 1) * 0.7))
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

function group(key: string): 'attack' | 'deny' | 'health' {
  if (key === 'admin_bruteforce_lockout') return 'attack'
  if (key === 'internal_policy_deny_expected' || key === 'device_service_port_probe_contained') return 'deny'
  return 'health'
}

function Edge({ a, b, tone, flows, dim, hot, delay, tempo }: { a: Pt; b: Pt; tone: string; flows: number; dim: boolean; hot?: boolean; delay: number; tempo: number }) {
  const d = bez(a, b)
  const n = dim ? 1 : Math.max(1, Math.min(7, Math.round(Math.log10(flows + 1) - 1)))
  const dur = Math.max(0.7, Math.max(1.4, 4.2 - Math.log10(flows + 1) * 0.42) / tempo)
  return (
    <>
      <path d={d} className={`flow-line ${tone} ${dim ? 'dim' : ''} ${hot ? 'hot' : ''} appear`} style={{ strokeWidth: weight(flows) + (hot ? 1.5 : 0), animationDelay: `${delay}s` }} />
      {Array.from({ length: n }).map((_, i) => (
        <circle key={i} r={dim ? 1.4 : hot ? 3 : 2.4} className={`pulse ${tone} ${dim ? 'dim' : ''}`}>
          <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`${(i * dur) / n}s`} path={d} />
        </circle>
      ))}
    </>
  )
}

function Float({ x, y, w, lines, tone }: { x: number; y: number; w: number; lines: { k: string; v: string }[]; tone: string }) {
  return (
    <foreignObject x={x} y={clamp(y, 2, VBH - 60)} width={w} height={14 + lines.length * 19} className="float-fo">
      <div className={`float-panel ${tone}`}>
        {lines.map((l, i) => (
          <div key={i} className="float-row">
            <span className="float-k">{l.k}</span>
            <Scramble className="float-v" text={l.v} />
          </div>
        ))}
      </div>
    </foreignObject>
  )
}

const THREAT_DX: Record<string, number> = { high: 0, watch: 30, ok: 56 }

export function TopologyCanvas({
  topo,
  stats,
  activeKey,
  drillSub,
  drillDev,
  tempo,
  marks,
  threat,
  lang,
  meshCount,
  meshLoading,
  hover3D,
  hover3DCidr,
  topoAlert,
  onHoverSubnet,
  onOpen3D,
  onCloseThreat,
  onSub,
  onDev,
  onBatch,
}: {
  topo: Topology
  stats: DataStats
  activeKey: string
  drillSub: string | null
  drillDev: string | null
  tempo: number
  marks: Record<string, { severity: string; verdict: string }>
  threat: Threat | null
  lang: Lang
  meshCount: number
  meshLoading: boolean
  hover3D: string | null
  hover3DCidr: string | null
  topoAlert: { cidr: string; ip: string; verdict: string; severity: string } | null
  onHoverSubnet?: (cidr: string | null) => void
  onOpen3D: () => void
  onCloseThreat: () => void
  onSub: (s: Subnet | null) => void
  onDev: (d: Device | null, cidr: string) => void
  onBatch: (cidr: string) => void
}) {
  const g = group(activeKey)
  const core: Pt = { x: 452, y: 340 }
  const ref = useRef<SVGSVGElement | null>(null)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const drag = useRef<{ px: number; py: number; x: number; y: number } | null>(null)

  const onDown = (e: React.MouseEvent) => {
    if (e.button !== 2 && e.button !== 1) return
    e.preventDefault()
    drag.current = { px: e.clientX, py: e.clientY, x: pan.x, y: pan.y }
  }
  const onMove = (e: React.MouseEvent) => {
    if (!drag.current || !ref.current) return
    const k = VBW / ref.current.clientWidth
    setPan({ x: drag.current.x + (e.clientX - drag.current.px) * k, y: drag.current.y + (e.clientY - drag.current.py) * k })
  }
  const endDrag = () => {
    drag.current = null
  }

  const layout = useMemo(() => {
    const atk = stats.topAttackerSrc.slice(0, 3).map((d, i) => ({ ip: d[0], v: d[1], p: { x: 70, y: 170 + i * 110 } as Pt }))
    const lan = topo.interfaces.filter((it) => it.kind === 'lan')
    const ys = [120, 300, 470, 600]
    const ifs = lan.map((it, i) => {
      const p: Pt = { x: 700, y: ys[i] ?? 120 + i * 150 }
      const sub = topo.subnets.find((s) => s.intf === it.name && s.hosts > 1)
      return { it, p, sub, subP: { x: 900, y: p.y } as Pt }
    })
    return { atk, ifs }
  }, [topo, stats])

  const openSub = layout.ifs.find((f) => f.sub && drillSub === f.sub.cidr)?.sub ?? null
  const devPos: Record<string, Pt> = {}
  if (openSub) {
    ;(openSub.devices ?? []).slice(0, 7).forEach((dv, j) => {
      devPos[dv.ip] = { x: 1066 + (THREAT_DX[dv.threat] ?? 40), y: 90 + j * 80 }
    })
  }
  const anchor = threat ? devPos[threat.ip] : undefined

  return (
    <svg
      ref={ref}
      viewBox={`0 0 ${VBW} ${VBH}`}
      className="flow-canvas"
      preserveAspectRatio="xMidYMid meet"
      onMouseDown={onDown}
      onMouseMove={onMove}
      onMouseUp={endDrag}
      onMouseLeave={endDrag}
      onContextMenu={(e) => e.preventDefault()}
      style={{ cursor: drag.current ? 'grabbing' : 'default' }}
    >
      <defs>
        <radialGradient id="gw" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgba(95,228,209,0.32)" />
          <stop offset="100%" stopColor="rgba(95,228,209,0)" />
        </radialGradient>
        <filter id="mglow" x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="3.5" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>

      <g transform={`translate(${pan.x} ${pan.y})`}>
        {layout.atk.map((a, i) => (
          <Edge key={`ea${i}`} a={a.p} b={core} tone="t-attack" flows={a.v} dim={g !== 'attack'} delay={0.1 + i * 0.05} tempo={tempo} />
        ))}
        {layout.ifs.map((f, i) => {
          const focused = drillSub === f.sub?.cidr
          const fade = g === 'attack' || (drillSub && !focused)
          return (
            <g key={`if${i}`}>
              <Edge a={core} b={f.p} tone="t-flow" flows={f.it.flows} dim={!!fade} hot={focused} delay={0.4 + i * 0.06} tempo={tempo} />
              {f.sub ? <Edge a={f.p} b={f.subP} tone="t-flow" flows={f.sub.flows} dim={!!fade} hot={focused} delay={0.6 + i * 0.06} tempo={tempo} /> : null}
            </g>
          )
        })}

        <circle cx={core.x} cy={core.y} r="68" fill="url(#gw)" />
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
        <text x={70} y={120} className="zone-tag appear">WAN1 · {short(stats.distinctSrc)} src</text>

        {layout.ifs.map((f, i) => {
          const focused = drillSub === f.sub?.cidr
          const dimIf = g === 'attack' || (drillSub && !focused)
          const highThreat = f.sub?.devices?.some((dv) => dv.threat === 'high')
          return (
            <g key={`ifn${i}`} className={`node appear ${dimIf ? 'node-dim' : ''}`} style={{ animationDelay: `${0.5 + i * 0.06}s` }}>
              <rect x={f.p.x - 52} y={f.p.y - 16} width="104" height="32" className="m-intf" />
              <text x={f.p.x} y={f.p.y - 1} className="n-intf">{f.it.name}</text>
              <text x={f.p.x} y={f.p.y + 12} className="n-sub">{short(f.it.flows)} flows</text>
              {f.sub ? (
                <g
                  className={`subnet ${focused ? 'open' : ''} ${hover3DCidr === f.sub.cidr ? 'mapped' : ''}`}
                  onClick={() => onSub(focused ? null : f.sub!)}
                  onMouseEnter={() => onHoverSubnet?.(f.sub!.cidr)}
                  onMouseLeave={() => onHoverSubnet?.(null)}
                  style={{ cursor: 'pointer' }}
                >
                  {hover3DCidr === f.sub.cidr ? <circle cx={f.subP.x} cy={f.subP.y} r="18" className="map-ring" /> : null}
                  <rect x={f.subP.x - 9} y={f.subP.y - 9} width="18" height="18" className="m-host" />
                  {highThreat ? <circle cx={f.subP.x + 9} cy={f.subP.y - 9} r="3.5" className="threat-pip" /> : null}
                  <text x={f.subP.x + 18} y={f.subP.y - 1} className="n-ip" textAnchor="start">{f.sub.cidr}</text>
                  <text x={f.subP.x + 18} y={f.subP.y + 12} className="n-v amber" textAnchor="start">{f.sub.hosts} hosts {focused ? '▾' : '▸'}</text>
                  {hover3DCidr === f.sub.cidr && hover3D ? (
                    <text x={f.subP.x + 18} y={f.subP.y + 26} className="map-ip" textAnchor="start">◂ {hover3D}</text>
                  ) : null}
                  {topoAlert && topoAlert.cidr === f.sub.cidr ? (
                    <g>
                      <circle cx={f.subP.x} cy={f.subP.y} r="21" className="alert-ring" />
                      <text x={f.subP.x + 18} y={f.subP.y + (hover3DCidr === f.sub.cidr ? 40 : 26)} className="alert-verdict" textAnchor="start">
                        ⚠ {topoAlert.ip} · {topoAlert.verdict || '研判中…'}
                      </text>
                    </g>
                  ) : null}
                </g>
              ) : null}
            </g>
          )
        })}

        {/* open subnet → fixed-column device tree, generous spacing */}
        {layout.ifs.map((f) => {
          if (!f.sub || drillSub !== f.sub.cidr) return null
          const devs = (f.sub.devices ?? []).slice(0, 7)
          const subNode: Pt = { x: f.subP.x, y: f.subP.y }
          const hi = devs.filter((d) => d.threat === 'high').length
          return (
            <g key={`tree-${f.sub.cidr}`}>
              <Float x={subNode.x - 96} y={subNode.y - 84} w={232} tone={hi ? 'alert' : 'flow'}
                lines={[
                  { k: 'SUBNET', v: f.sub.cidr },
                  { k: 'HOSTS', v: `${f.sub.hosts} · ${short(f.sub.flows)} flows` },
                  { k: 'FLAGGED', v: `${hi} high · ${short(f.sub.accept)} accept` },
                ]} />
              <g className="batch-trig" onClick={() => onBatch(f.sub!.cidr)} style={{ cursor: 'pointer' }}>
                <rect x={subNode.x - 8} y={subNode.y + 24} width="132" height="22" />
                <text x={subNode.x + 58} y={subNode.y + 39}>⚡ 批量研判 / batch</text>
              </g>
              {devs.map((dv, j) => {
                const dy = 90 + j * 80
                const dp: Pt = { x: 1066 + (THREAT_DX[dv.threat] ?? 40), y: dy }
                const open = drillDev === dv.ip
                const mark = marks[dv.ip]
                const alert = !!mark && (mark.severity === 'high' || mark.severity === 'medium')
                const tone = alert ? 'alert' : dv.threat
                const showPorts = open || alert
                const rad = dv.threat === 'high' ? 8 : dv.threat === 'watch' ? 6 : 5
                return (
                  <g key={dv.ip} className="branch-in">
                    <path d={bez(subNode, dp)} className={`branch ${tone}`} />
                    <g className="dev-node" onClick={() => onDev(open ? null : dv, f.sub!.cidr)} style={{ cursor: 'pointer' }}>
                      <circle cx={dp.x} cy={dp.y} r={rad} className={`m-dev ${tone} ${open ? 'sel' : ''}`} />
                      <text x={dp.x + 14} y={dp.y - 1} className="n-ip" textAnchor="start">{dv.ip}</text>
                      {mark ? (
                        <text x={dp.x + 14} y={dp.y + 12} className={`n-verdict ${alert ? 'alert' : ''}`} textAnchor="start">{mark.verdict}</text>
                      ) : (
                        <text x={dp.x + 14} y={dp.y + 12} className={`n-v ${dv.threat === 'high' ? '' : 'amber'}`} textAnchor="start">{short(dv.deny)} deny</text>
                      )}
                    </g>
                    {open ? (
                      <Float x={dp.x - 6} y={dp.y - 64} w={206} tone={alert ? 'alert' : dv.threat}
                        lines={[
                          { k: 'DENY', v: `${short(dv.deny)} / ${dv.accept} ok` },
                          { k: 'PORTS', v: dv.top_ports.map((p) => `:${p}`).join(' ') },
                        ]} />
                    ) : null}
                    {showPorts
                      ? dv.top_ports.slice(0, 3).map((pt, k) => {
                          const lp: Pt = { x: dp.x + 172, y: clamp(dp.y + (k - (dv.top_ports.length - 1) / 2) * 22, 16, VBH - 16) }
                          return (
                            <g key={pt} className="branch-in leaf">
                              <path d={bez(dp, lp)} className={`branch ${tone} ${alert ? 'flow-alert' : ''}`} />
                              <rect x={lp.x - 4} y={lp.y - 4} width="8" height="8" className={`m-leaf ${tone}`} />
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

        {/* 3D constellation portal — on the topology itself */}
        {meshCount > 0 ? (
          <g className="portal3d" onClick={onOpen3D} style={{ cursor: 'pointer' }}>
            {layout.ifs.map((f, i) => (f.sub ? <path key={i} d={bez(f.subP, { x: 1252, y: 372 })} className="portal-link" /> : null))}
            <circle cx={1252} cy={372} r="30" className="portal-halo" />
            <circle cx={1252} cy={372} r="20" className="portal-ring" />
            <text x={1252} y={377} className="portal-glyph" textAnchor="middle">⬡</text>
            <text x={1252} y={418} className="portal-label" textAnchor="middle">{meshLoading ? (lang === 'zh' ? '建模中…' : 'modeling…') : lang === 'zh' ? '3D 全网建模' : '3D model'}</text>
            <text x={1252} y={433} className="portal-sub" textAnchor="middle">{meshCount} {lang === 'zh' ? '设备' : 'devices'}</text>
          </g>
        ) : null}

        {/* in-canvas analysis layer: leader line + panel + impact subgraph */}
        {threat && anchor ? (
          (() => {
            const panelTop: Pt = { x: 360, y: 706 }
            const cx = 980
            const cy = 824
            const peers = threat.impactPeers ?? []
            return (
              <g className="analysis-layer">
                <path d={bez(anchor, panelTop)} className="leader" />
                <path d={bez(anchor, { x: cx, y: cy - 30 })} className="leader" />
                {threat.loading ? (
                  <foreignObject x={40} y={690} width={560} height={120}>
                    <div className="an-panel">
                      <div className="an-head"><span className="an-kicker">DEEPSEEK · {threat.ip}</span></div>
                      <Analyzing lang={lang} />
                    </div>
                  </foreignObject>
                ) : threat.error ? (
                  <foreignObject x={40} y={690} width={560} height={90}>
                    <div className="an-panel"><div className="an-body err">{threat.error}</div></div>
                  </foreignObject>
                ) : (
                  <>
                    <foreignObject x={40} y={680} width={600} height={300}>
                      <div className={`an-panel sev-${threat.severity}`}>
                        <div className="an-head">
                          <span className="an-kicker">DEEPSEEK · {threat.ip}</span>
                          <button className="an-x" onClick={onCloseThreat}>✕</button>
                        </div>
                        <div className="an-verdict">
                          <span className={`sev-dot ${threat.severity}`} />
                          <Scramble className="an-vtxt" text={threat.verdict ?? ''} />
                          <span className="sev-tag">{threat.severity}</span>
                        </div>
                        <p className="an-analysis">{threat.analysis}</p>
                        <div className="an-pred">
                          <div><span className="pl">{lang === 'zh' ? '最可能' : 'likely'}</span>{threat.mostLikely}</div>
                          <div><span className="pl bad">{lang === 'zh' ? '最坏' : 'worst'}</span>{threat.worstCase}</div>
                          <div><span className="pl ok">{lang === 'zh' ? '恢复' : 'recovery'}</span>{threat.recovery?.action} <b>· {threat.recovery?.eta}</b></div>
                        </div>
                      </div>
                    </foreignObject>

                    <text x={cx} y={cy - 96} className="impact-tag" textAnchor="middle">
                      {lang === 'zh' ? '影响面关系网络' : 'blast-radius graph'}
                    </text>
                    <circle cx={cx} cy={cy} r="11" className="m-dev alert sel" />
                    <text x={cx} y={cy + 26} className="n-ip" textAnchor="middle">{threat.ip}</text>
                    {peers.map((p, i) => {
                      const py = cy + (i - (peers.length - 1) / 2) * 70
                      const px = cx + 200
                      const real = devPos[p.ip]
                      return (
                        <g key={p.ip} className="branch-in">
                          <path d={bez({ x: cx, y: cy }, { x: px, y: py })} className="branch alert flow-alert" />
                          {real ? <path d={bez({ x: px, y: py }, real)} className="link-up" /> : null}
                          <circle cx={px} cy={py} r="7" className="m-dev high" />
                          <text x={px + 14} y={py - 1} className="n-ip" textAnchor="start">{p.ip}</text>
                          <text x={px + 14} y={py + 12} className="n-verdict alert" textAnchor="start">{p.relation}</text>
                        </g>
                      )
                    })}
                  </>
                )}
              </g>
            )
          })()
        ) : null}
      </g>
    </svg>
  )
}
