import { useMemo, useRef, useState } from 'react'
import type { DataStats, Device, Subnet, Topology } from '../types'
import { Scramble } from './Motion'
import { Analyzing, type Threat, type WanThreat } from './ThreatCard'
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

function Edge({ a, b, tone, flows, dim, hot, hero, delay, tempo }: { a: Pt; b: Pt; tone: string; flows: number; dim: boolean; hot?: boolean; hero?: boolean; delay: number; tempo: number }) {
  const d = bez(a, b)
  const n = dim ? 1 : Math.max(1, Math.min(hero ? 8 : 5, Math.round(Math.log10(flows + 1) - 1)))
  const dur = Math.max(0.7, Math.max(1.4, 4.2 - Math.log10(flows + 1) * 0.42) / tempo)
  // Width encodes hierarchy, not just raw throughput: the hero threat path is
  // deliberately the boldest line; the supporting fan is capped thin so the eye
  // locks onto the focal core, and dimmed context is thinnest.
  const w = hero ? weight(flows) + 2.6 : dim ? Math.min(weight(flows), 2) : Math.min(weight(flows), 3.2)
  return (
    <>
      <path d={d} className={`flow-line ${tone} ${dim ? 'dim' : ''} ${hot ? 'hot' : ''} ${hero ? 'hero' : ''} appear`} style={{ strokeWidth: w + (hot ? 1.2 : 0), animationDelay: `${delay}s` }} />
      {Array.from({ length: n }).map((_, i) => (
        <circle key={i} r={dim ? 1.3 : hero ? 3.4 : hot ? 3 : 2.2} className={`pulse ${tone} ${dim ? 'dim' : ''} ${hero ? 'hero' : ''}`}>
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

// ── tactical HUD geometry helpers ──────────────────────────────────────────
const polar = (c: Pt, r: number, deg: number): Pt => {
  const a = (deg * Math.PI) / 180
  return { x: c.x + Math.cos(a) * r, y: c.y + Math.sin(a) * r }
}

/** Ambient sector-radar watermark centred on the focal core: range rings,
 *  coordinate crosshair, azimuth ticks and a shaded WAN-ingress threat sector.
 *  Pure decoration — gives big-picture context without competing with data. */
function HudRadar({ core }: { core: Pt }) {
  const rings = [136, 244, 352]
  const sIn = 250
  const sOut = 352
  const a1 = 156
  const a2 = 220
  const sector = `M ${polar(core, sIn, a1).x} ${polar(core, sIn, a1).y} L ${polar(core, sOut, a1).x} ${polar(core, sOut, a1).y} A ${sOut} ${sOut} 0 0 1 ${polar(core, sOut, a2).x} ${polar(core, sOut, a2).y} L ${polar(core, sIn, a2).x} ${polar(core, sIn, a2).y} A ${sIn} ${sIn} 0 0 0 ${polar(core, sIn, a1).x} ${polar(core, sIn, a1).y} Z`
  return (
    <g className="hud-radar" pointerEvents="none">
      <line x1={44} y1={core.y} x2={1316} y2={core.y} className="hud-axis" />
      <line x1={core.x} y1={26} x2={core.x} y2={974} className="hud-axis" />
      <path d={sector} className="hud-sector" />
      <path d={sector} className="hud-sector-edge" />
      {rings.map((r) => (
        <circle key={r} cx={core.x} cy={core.y} r={r} className="hud-ring" />
      ))}
      {Array.from({ length: 36 }).map((_, i) => {
        const deg = i * 10
        const r2 = i % 3 === 0 ? 338 : 345
        const p1 = polar(core, r2, deg)
        const p2 = polar(core, 352, deg)
        return <line key={i} x1={p1.x} y1={p1.y} x2={p2.x} y2={p2.y} className="hud-azi" />
      })}
      {['000', '090', '180', '270'].map((b, i) => {
        const p = polar(core, 366, i * 90)
        return (
          <text key={b} x={p.x} y={p.y + 3} className="hud-bearing" textAnchor="middle">{b}</text>
        )
      })}
    </g>
  )
}

/** The FortiGate core as a reticle-locked hero: angular corner brackets, a
 *  rotating crimson lock ring, crosshair ticks and the ONE acid focal accent. */
function CoreReticle({ core, lang }: { core: Pt; lang: Lang }) {
  const bx = 82
  const by = 54
  const L = 17
  const corners = [
    `M ${core.x - bx} ${core.y - by + L} L ${core.x - bx} ${core.y - by} L ${core.x - bx + L} ${core.y - by}`,
    `M ${core.x + bx - L} ${core.y - by} L ${core.x + bx} ${core.y - by} L ${core.x + bx} ${core.y - by + L}`,
    `M ${core.x + bx} ${core.y + by - L} L ${core.x + bx} ${core.y + by} L ${core.x + bx - L} ${core.y + by}`,
    `M ${core.x - bx + L} ${core.y + by} L ${core.x - bx} ${core.y + by} L ${core.x - bx} ${core.y + by - L}`,
  ]
  return (
    <g className="core-reticle" pointerEvents="none">
      <circle cx={core.x} cy={core.y} r={104} className="core-lockring">
        <animateTransform attributeName="transform" type="rotate" from={`0 ${core.x} ${core.y}`} to={`360 ${core.x} ${core.y}`} dur="18s" repeatCount="indefinite" />
      </circle>
      {corners.map((d, i) => (
        <path key={i} d={d} className="core-bracket" />
      ))}
      <line x1={core.x} y1={core.y - by} x2={core.x} y2={core.y - by + 9} className="core-tick" />
      <line x1={core.x} y1={core.y + by} x2={core.x} y2={core.y + by - 9} className="core-tick" />
      <line x1={core.x - bx} y1={core.y} x2={core.x - bx + 9} y2={core.y} className="core-tick" />
      <line x1={core.x + bx} y1={core.y} x2={core.x + bx - 9} y2={core.y} className="core-tick" />
      <rect x={core.x - 32} y={core.y - by - 5} width={64} height={5} className="core-acid" />
      <text x={core.x} y={core.y - by - 12} className="core-label" textAnchor="middle">
        ◎ {lang === 'zh' ? '主目标 · 核心' : 'PRIMARY TARGET · CORE'}
      </text>
    </g>
  )
}

/** Corner situational read-outs — the big-picture intel the client asked for:
 *  scan-id, data window, distinct-src / lockout counts, device / interface /
 *  subnet counts, and a threat status line. */
function HudReadouts({ stats, meshCount, ifCount, subCount, lang, showStatus }: { stats: DataStats; meshCount: number; ifCount: number; subCount: number; lang: Lang; showStatus: boolean }) {
  const scanId = `R230-${((stats.adminLoginFailed ?? 0) % 0x10000).toString(16).toUpperCase().padStart(4, '0')}`
  const win = stats.windowDays?.[stats.windowDays.length - 1] ?? ''
  return (
    <g className="hud-readouts" pointerEvents="none">
      <g textAnchor="end">
        <text x={1316} y={30} className="hud-r-dim">{win} · {lang === 'zh' ? '48H 窗口' : '48H WINDOW'}</text>
        <text x={1316} y={47} className="hud-r-id">SCAN ▸ {scanId}</text>
        <text x={1316} y={64} className="hud-r-line">SRC <tspan className="hot">{short(stats.distinctSrc)}</tspan> · LCK <tspan className="hot">{stats.lockouts ?? 0}</tspan></text>
        <text x={1316} y={81} className="hud-r-line">DEV <tspan className="acc">{meshCount}</tspan> · IF {ifCount} · NET {subCount}</text>
      </g>
      {showStatus ? (
        <text x={44} y={642} className="hud-status">
          <tspan className="hot">◆ {lang === 'zh' ? '威胁等级 · 升高' : 'THREAT ELEVATED'}</tspan>
          <tspan className="dim"> · {lang === 'zh' ? 'WAN 暴力破解战役' : 'WAN BRUTE-FORCE CAMPAIGN'} · {ifCount} IF / {subCount} NET {lang === 'zh' ? '联动' : 'LINKED'}</tspan>
        </text>
      ) : null}
      <text transform="rotate(-90 22 452)" x={22} y={452} className="hud-vlabel" textAnchor="middle">NET-SITUATIONAL CONSOLE</text>
    </g>
  )
}

const THREAT_DX: Record<string, number> = { high: 0, watch: 30, ok: 56 }
const KILLCHAIN: { k: string; zh: string; en: string }[] = [
  { k: 'recon', zh: '侦察', en: 'recon' },
  { k: 'credential-access', zh: '凭证攻击', en: 'cred-access' },
  { k: 'lateral-movement', zh: '横向移动', en: 'lateral' },
  { k: 'impact', zh: '影响', en: 'impact' },
]

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
  wan,
  onWan,
  onCloseWan,
  onHoverSubnet,
  onOpen3D,
  onCloseThreat,
  onSub,
  onDev,
  onBatch,
  onPentest,
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
  wan: WanThreat | null
  onWan: (ip: string) => void
  onCloseWan: () => void
  onHoverSubnet?: (cidr: string | null) => void
  onOpen3D: () => void
  onCloseThreat: () => void
  onSub: (s: Subnet | null) => void
  onDev: (d: Device | null, cidr: string) => void
  onBatch: (cidr: string) => void
  onPentest?: () => void
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
      <g transform={`translate(${pan.x} ${pan.y})`}>
        <HudRadar core={core} />
        {layout.atk.map((a, i) => (
          <Edge key={`ea${i}`} a={a.p} b={core} tone="t-attack" flows={a.v} dim={g !== 'attack'} hero={g === 'attack'} delay={0.1 + i * 0.05} tempo={tempo} />
        ))}
        {layout.ifs.map((f, i) => {
          const focused = drillSub === f.sub?.cidr
          const fade = g === 'attack' || (drillSub && !focused) || !!wan
          return (
            <g key={`if${i}`}>
              <Edge a={core} b={f.p} tone="t-flow" flows={f.it.flows} dim={!!fade} hot={focused} delay={0.4 + i * 0.06} tempo={tempo} />
              {f.sub ? <Edge a={f.p} b={f.subP} tone="t-flow" flows={f.sub.flows} dim={!!fade} hot={focused} delay={0.6 + i * 0.06} tempo={tempo} /> : null}
            </g>
          )
        })}

        <g className="node gw-node appear" style={{ animationDelay: '0.3s' }}>
          <rect x={core.x - 60} y={core.y - 32} width="120" height="64" rx="1" />
          <text x={core.x} y={core.y - 5} className="n-title">{topo.core.name}</text>
          <text x={core.x} y={core.y + 14} className="n-sub">{topo.core.ip}</text>
        </g>
        <CoreReticle core={core} lang={lang} />

        {layout.atk.map((a, i) => {
          const sel = wan?.ip === a.ip
          return (
            <g
              key={`an${i}`}
              className={`node appear atk-node ${g === 'attack' ? '' : 'node-dim'} ${sel ? 'sel' : ''}`}
              style={{ animationDelay: `${i * 0.05}s`, cursor: 'pointer' }}
              onClick={() => onWan(a.ip)}
            >
              {sel ? <circle cx={a.p.x} cy={a.p.y} r="15" className="atk-halo" /> : null}
              <rect x={a.p.x - 7} y={a.p.y - 7} width="14" height="14" className="m-attack" transform={`rotate(45 ${a.p.x} ${a.p.y})`} />
              <text x={a.p.x + 16} y={a.p.y - 1} className="n-ip" textAnchor="start">{a.ip}</text>
              <text x={a.p.x + 16} y={a.p.y + 12} className="n-v" textAnchor="start">{short(a.v)}<tspan className="probe-hint"> ▸ 研判</tspan></text>
            </g>
          )
        })}
        <text x={70} y={120} className="zone-tag appear">WAN1 · {short(stats.distinctSrc)} src · {stats.lockouts ?? ''} lockout</text>

        {layout.ifs.map((f, i) => {
          const focused = drillSub === f.sub?.cidr
          const dimIf = g === 'attack' || (drillSub && !focused) || !!wan
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
                          <span className={`sev-tag ${threat.severity ?? ''}`}>{threat.severity}</span>
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

        {/* WAN intrusion deep-analysis: campaign lockstep → admin target → cross-canvas pivots */}
        {wan ? (
          (() => {
            const wa = layout.atk.find((a) => a.ip === wan.ip)
            const anchorP: Pt = wa ? wa.p : { x: 70, y: 280 }
            const fg = core
            const sibs = wan.siblings ?? []
            const inter = wan.internalCorrelation ?? []
            const stage = wan.killChain ?? ''
            return (
              <g className="wan-layer">
                <path d={bez(anchorP, fg)} className="wan-spine appear" />
                <text x={(anchorP.x + fg.x) / 2 - 6} y={(anchorP.y + fg.y) / 2 - 10} className="wan-spine-tag" textAnchor="middle">
                  {short(wan.attempts ?? 0)} · admin login
                </text>

                {/* /24 lockstep sibling cluster */}
                <text x={anchorP.x} y={anchorP.y - 26} className="wan-netblock" textAnchor="start">
                  ◇ {wan.netblock} · {short(wan.netblockAttempts ?? 0)} · lockstep
                </text>
                {sibs.map((s, k) => {
                  const sp: Pt = { x: anchorP.x + 78, y: clamp(anchorP.y - 44 + k * 30, 20, VBH - 20) }
                  return (
                    <g key={s.ip} className="branch-in">
                      <path d={bez(anchorP, sp)} className="wan-sib-link" />
                      <rect x={sp.x - 5} y={sp.y - 5} width="10" height="10" className="m-attack sib" transform={`rotate(45 ${sp.x} ${sp.y})`} />
                      <text x={sp.x + 12} y={sp.y + 3} className="wan-sib-ip" textAnchor="start">{s.ip} · {short(s.attempts ?? 0)}</text>
                    </g>
                  )
                })}

                {/* FortiGate admin target + lockout impact */}
                {!wan.loading && !wan.error ? (
                  <>
                    <circle cx={fg.x} cy={fg.y} r="34" className="wan-lockring" />
                    <text x={fg.x} y={fg.y + 54} className="wan-lock" textAnchor="middle">⊘ {wan.lockouts ?? 0} admin lockouts</text>
                  </>
                ) : null}

                {/* cross-canvas post-compromise pivots */}
                {inter.length ? (
                  <text x={1132} y={112} className="impact-tag" textAnchor="start">
                    {lang === 'zh' ? '内网横移关联' : 'internal pivots'}
                  </text>
                ) : null}
                {inter.map((c, k) => {
                  const span = inter.length > 1 ? (VBH - 320) / (inter.length - 1) : 0
                  const ip_: Pt = { x: 1132, y: clamp(150 + k * span, 130, VBH - 90) }
                  return (
                    <g key={c.ip} className="branch-in wan-pivot">
                      <path d={bez(fg, ip_)} className="wan-pivot-link" />
                      <circle cx={ip_.x} cy={ip_.y} r="6" className="m-dev high" />
                      <text x={ip_.x + 12} y={ip_.y - 1} className="n-ip" textAnchor="start">{c.ip}</text>
                      <text x={ip_.x + 12} y={ip_.y + 12} className="wan-rel" textAnchor="start">{c.relation} · {short(c.deny ?? 0)} deny</text>
                    </g>
                  )
                })}

                {/* deep-analysis panel */}
                {wan.loading ? (
                  <foreignObject x={36} y={678} width={580} height={130}>
                    <div className="an-panel wan-panel">
                      <div className="an-head"><span className="an-kicker">DEEPSEEK · WAN · {wan.ip}</span></div>
                      <Analyzing lang={lang} />
                    </div>
                  </foreignObject>
                ) : wan.error ? (
                  <foreignObject x={36} y={690} width={560} height={90}>
                    <div className="an-panel wan-panel"><div className="an-body err">{wan.error}</div></div>
                  </foreignObject>
                ) : (
                  <foreignObject x={28} y={628} width={660} height={364}>
                    <div className={`an-panel wan-panel sev-${wan.severity}`}>
                      <div className="an-head">
                        <span className="an-kicker">DEEPSEEK · WAN 入侵研判 · {wan.ip}</span>
                        <button className="an-x" onClick={onCloseWan}>✕</button>
                      </div>
                      <div className="an-verdict">
                        <span className={`sev-dot ${wan.severity}`} />
                        <Scramble className="an-vtxt" text={wan.verdict ?? ''} />
                        <span className={`sev-tag ${wan.severity ?? ''}`}>{wan.severity}</span>
                        {typeof wan.confidence === 'number' ? <span className="wan-conf">{Math.round(wan.confidence * 100)}%</span> : null}
                      </div>
                      <div className="kc-rail">
                        {KILLCHAIN.map((s) => (
                          <span key={s.k} className={`kc-step ${s.k === stage ? 'on' : ''}`}>{lang === 'zh' ? s.zh : s.en}</span>
                        ))}
                      </div>
                      <p className="an-analysis">{wan.campaign}</p>
                      <div className="wan-meta">
                        <span><i>{lang === 'zh' ? '归因' : 'attribution'}</i>{wan.attribution}</span>
                        <span><i className="bad">{lang === 'zh' ? '影响' : 'blast'}</i>{wan.blast}</span>
                      </div>
                      {wan.actions && wan.actions.length ? (
                        <ol className="wan-actions">
                          {wan.actions.map((a, i) => (<li key={i}>{a}</li>))}
                        </ol>
                      ) : null}
                      <span className="tc-model">{wan.model} · {wan.distinctSrc} src</span>
                      {onPentest ? (
                        <button className="wan-pentest-cta" onClick={onPentest}>
                          <span className="wpc-arrow">▸</span>
                          <span className="wpc-txt">{lang === 'zh' ? '转入自我渗透测试 · 主动验证暴露面' : 'ESCALATE → SELF-PENTEST · PROVE THE EXPOSURE'}</span>
                          <span className="wpc-tag">PAGE 03</span>
                        </button>
                      ) : null}
                    </div>
                  </foreignObject>
                )}
              </g>
            )
          })()
        ) : null}

        <HudReadouts
          stats={stats}
          meshCount={meshCount}
          ifCount={layout.ifs.length}
          subCount={layout.ifs.filter((f) => f.sub).length}
          lang={lang}
          showStatus={!threat && !wan}
        />
      </g>
    </svg>
  )
}
