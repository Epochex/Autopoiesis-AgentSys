import { useCallback, useMemo, useRef, useState } from 'react'
import type { DataStats, Device, GraphAnalysis, GraphDevice, Subnet, SubnetGraph, Topology } from '../types'
import { Scramble } from './Motion'
import { SubnetGraphLayer } from './SubnetGraph'
import { Analyzing, type Threat, type WanThreat } from './ThreatCard'
import type { Lang } from '../i18n'

type Pt = { x: number; y: number }
const VBW = 1360
const VBH = 1000
const bez = (a: Pt, b: Pt) => `M${a.x} ${a.y} C ${(a.x + b.x) / 2} ${a.y}, ${(a.x + b.x) / 2} ${b.y}, ${b.x} ${b.y}`
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)
const clipS = (s: string, n: number) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s)
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

/** Annular-sector (wedge) path from an inner to an outer radius across [a1,a2]°. */
function annularSector(c: Pt, rIn: number, rOut: number, a1: number, a2: number): string {
  const large = a2 - a1 > 180 ? 1 : 0
  const p1 = polar(c, rOut, a1)
  const p2 = polar(c, rOut, a2)
  const p3 = polar(c, rIn, a2)
  const p4 = polar(c, rIn, a1)
  return `M ${p1.x} ${p1.y} A ${rOut} ${rOut} 0 ${large} 1 ${p2.x} ${p2.y} L ${p3.x} ${p3.y} A ${rIn} ${rIn} 0 ${large} 0 ${p4.x} ${p4.y} Z`
}

/** Ambient sector-radar watermark centred on the focal core: range rings,
 *  coordinate crosshair, azimuth ticks. Pure decoration. */
function HudRadar({ core }: { core: Pt }) {
  const rings = [136, 244, 352]
  return (
    <g className="hud-radar" pointerEvents="none">
      <line x1={44} y1={core.y} x2={1316} y2={core.y} className="hud-axis" />
      <line x1={core.x} y1={26} x2={core.x} y2={974} className="hud-axis" />
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

/** WAN-ingress THREAT CONE — the region the internet is actively attacking through.
 *  Hazard-striped caution fill + range-ring "incoming waves" that converge on the
 *  FortiGate + an oscillating radar sweep. Motion speed tracks the live event rate
 *  (tempo), so a busier hour visibly hammers harder. Reads as "under fire", not decor. */
function ThreatCone({ core, tempo, active }: { core: Pt; tempo: number; active: boolean }) {
  const a1 = 158
  const a2 = 216
  const rIn = 104
  const rOut = 398
  const wedge = annularSector(core, rIn, rOut, a1, a2)
  const waveDur = Math.max(2.0, 3.8 / tempo)
  const sweepDur = Math.max(2.8, 5.4 / tempo)
  return (
    <g className={`threat-cone ${active ? 'live' : ''}`} pointerEvents="none">
      <defs>
        <clipPath id="tc-clip"><path d={wedge} /></clipPath>
        <pattern id="tc-haz" width="16" height="16" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <rect width="8" height="16" className="tc-haz-stripe" />
        </pattern>
      </defs>
      <path d={wedge} fill="url(#tc-haz)" className="tc-fill" />
      <path d={wedge} className="tc-edge" />
      <g clipPath="url(#tc-clip)">
        {[0, 1, 2, 3].map((i) => (
          <circle key={i} cx={core.x} cy={core.y} className="tc-wave" r={rOut}>
            <animate attributeName="r" values={`${rOut};${rIn}`} dur={`${waveDur}s`} begin={`${(i * waveDur) / 4}s`} repeatCount="indefinite" />
            <animate attributeName="opacity" values="0;0.6;0" dur={`${waveDur}s`} begin={`${(i * waveDur) / 4}s`} repeatCount="indefinite" />
          </circle>
        ))}
        <line x1={core.x + rIn} y1={core.y} x2={core.x + rOut} y2={core.y} className="tc-sweep">
          <animateTransform attributeName="transform" type="rotate" dur={`${sweepDur}s`} repeatCount="indefinite"
            calcMode="spline" keyTimes="0;0.5;1" keySplines="0.45 0 0.55 1;0.45 0 0.55 1"
            values={`${a1} ${core.x} ${core.y};${a2} ${core.x} ${core.y};${a1} ${core.x} ${core.y}`} />
        </line>
      </g>
    </g>
  )
}

/** One WAN attacker → FortiGate TRACER BEAM: a source→target gradient beam under a
 *  soft glow, with a streaking comet volley (bright head + fading trail) racing
 *  inbound. This is the "incoming fire" — the thing the old thin static curve never
 *  conveyed. */
function AttackBeam({ a, b, i, attempts, tempo, dim }: { a: Pt; b: Pt; i: number; attempts: number; tempo: number; dim: boolean }) {
  const d = bez(a, b)
  const gid = `beam-grad-${i}`
  const dur = Math.max(0.85, 1.7 / tempo)
  const w = Math.max(2.4, Math.min(5, 1.7 + Math.log10(attempts + 1)))
  const trail = 4
  return (
    <g className={`atk-beam ${dim ? 'dim' : ''}`} pointerEvents="none">
      <defs>
        <linearGradient id={gid} gradientUnits="userSpaceOnUse" x1={a.x} y1={a.y} x2={b.x} y2={b.y}>
          <stop offset="0" className="beam-stop-0" />
          <stop offset="0.6" className="beam-stop-1" />
          <stop offset="1" className="beam-stop-2" />
        </linearGradient>
      </defs>
      <path d={d} className="beam-halo" style={{ strokeWidth: w + 7 }} />
      <path d={d} stroke={`url(#${gid})`} className="beam-core" style={{ strokeWidth: w }} />
      {/* two staggered comet volleys, each a bright head trailed by fading motes */}
      {[0, 1].flatMap((v) =>
        Array.from({ length: trail }).map((_, h) => (
          <circle key={`${v}-${h}`} className={`beam-mote ${h === 0 ? 'head' : ''}`} r={Math.max(1.1, 3.6 - h * 0.8)} style={{ opacity: 0.95 - h * 0.22 }}>
            <animateMotion dur={`${dur}s`} repeatCount="indefinite" path={d} begin={`${(v * dur) / 2 + h * 0.05}s`} />
          </circle>
        )),
      )}
    </g>
  )
}

/** Impact shockwave at the FortiGate where the volleys land — coral rings bursting
 *  outward in time with the comet cadence. */
function CoreImpact({ core, tempo }: { core: Pt; tempo: number }) {
  const dur = Math.max(0.85, 1.7 / tempo)
  return (
    <g className="core-impact" pointerEvents="none">
      {[0, 1].map((i) => (
        <circle key={i} cx={core.x} cy={core.y} className="impact-ring" r={4}>
          <animate attributeName="r" values="6;34" dur={`${dur}s`} begin={`${(i * dur) / 2}s`} repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.7;0" dur={`${dur}s`} begin={`${(i * dur) / 2}s`} repeatCount="indefinite" />
        </circle>
      ))}
    </g>
  )
}

/** The FortiGate core as a reticle-locked hero: angular corner brackets, a
 *  rotating crimson lock ring, crosshair ticks and the ONE acid focal accent.
 *  No caption — the reticle itself says "locked target"; a tiny padlock glyph
 *  is the only marker. */
function CoreReticle({ core }: { core: Pt }) {
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
      {/* tiny padlock = locked target; replaces the old text caption */}
      <g className="core-lock">
        <path d={`M ${core.x - 3.5} ${core.y - by - 14} v -3 a 3.5 3.5 0 0 1 7 0 v 3`} className="core-lock-shackle" />
        <rect x={core.x - 5.5} y={core.y - by - 14} width={11} height={8} className="core-lock-body" />
      </g>
    </g>
  )
}

/** Corner situational read-outs — plain-language big picture: data window,
 *  attack-source / lockout counts, device / link / subnet counts, one status line. */
function HudReadouts({ stats, meshCount, ifCount, subCount, lang, showStatus }: { stats: DataStats; meshCount: number; ifCount: number; subCount: number; lang: Lang; showStatus: boolean }) {
  const zh = lang === 'zh'
  return (
    <g className="hud-readouts" pointerEvents="none">
      <g textAnchor="end">
        <text x={1316} y={30} className="hud-r-dim">{zh ? '近 48 小时' : 'LAST 48H'}</text>
        <text x={1316} y={50} className="hud-r-line"><tspan className="hot">{short(stats.distinctSrc)}</tspan> {zh ? '攻击来源' : 'sources'} · <tspan className="hot">{stats.lockouts ?? 0}</tspan> {zh ? '次锁定' : 'lockouts'}</text>
        <text x={1316} y={67} className="hud-r-line"><tspan className="acc">{meshCount}</tspan> {zh ? '设备' : 'devices'} · {ifCount} {zh ? '接口' : 'links'} · {subCount} {zh ? '网段' : 'subnets'}</text>
      </g>
      {showStatus ? (
        <text x={44} y={642} className="hud-status">
          <tspan className="hot">◆ {zh ? '威胁升高' : 'THREAT RISING'}</tspan>
          <tspan className="dim"> · {zh ? '外网暴力破解' : 'INTERNET BRUTE-FORCE'}</tspan>
        </text>
      ) : null}
    </g>
  )
}

const THREAT_DX: Record<string, number> = { high: 0, watch: 30, ok: 56 }
const KILLCHAIN: { k: string; zh: string; en: string }[] = [
  { k: 'recon', zh: '踩点', en: 'recon' },
  { k: 'credential-access', zh: '盗密码', en: 'steal creds' },
  { k: 'lateral-movement', zh: '内网扩散', en: 'spread' },
  { k: 'impact', zh: '破坏', en: 'impact' },
]

const SEV_ZH: Record<string, string> = { critical: '严重', high: '高危', medium: '中危', low: '低危' }
const sevLabel = (sev: string | undefined, lang: Lang) => (lang === 'zh' ? SEV_ZH[sev ?? ''] ?? sev ?? '' : sev ?? '')

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
  graph,
  graphAnalysis,
  hoverDev,
  onHoverDev,
  onGraphAnalyze,
  onCloseGraphAnalysis,
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
  graph: SubnetGraph | null
  graphAnalysis: GraphAnalysis | null
  hoverDev: string | null
  onHoverDev: (ip: string | null) => void
  onGraphAnalyze: (cidr: string) => void
  onCloseGraphAnalysis: () => void
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
  // ── viewport: wheel-zoom about the cursor, drag-to-pan anywhere on the plate ──
  const [view, setView] = useState({ k: 1, x: 0, y: 0 })
  const drag = useRef<{ px: number; py: number; x: number; y: number; moved: boolean } | null>(null)
  const [panning, setPanning] = useState(false)

  /** client px → root user space (accounts for the meet-fit letterboxing) */
  const toLocal = useCallback((e: React.MouseEvent | React.WheelEvent): Pt => {
    const svg = ref.current
    if (!svg) return { x: 0, y: 0 }
    const box = svg.getBoundingClientRect()
    const s = Math.min(box.width / VBW, box.height / VBH)
    return {
      x: (e.clientX - box.left - (box.width - VBW * s) / 2) / s,
      y: (e.clientY - box.top - (box.height - VBH * s) / 2) / s,
    }
  }, [])

  const onDown = (e: React.MouseEvent) => {
    drag.current = { px: e.clientX, py: e.clientY, x: view.x, y: view.y, moved: false }
  }
  const onMove = (e: React.MouseEvent) => {
    const d = drag.current
    const svg = ref.current
    if (!d || !svg) return
    const box = svg.getBoundingClientRect()
    const s = Math.min(box.width / VBW, box.height / VBH)
    const dx = (e.clientX - d.px) / s
    const dy = (e.clientY - d.py) / s
    if (!d.moved && Math.hypot(dx, dy) < 3) return // let clicks through
    d.moved = true
    if (!panning) setPanning(true)
    setView((v) => ({ ...v, x: d.x + dx, y: d.y + dy }))
  }
  const endDrag = () => {
    drag.current = null
    setPanning(false)
  }
  const onWheel = (e: React.WheelEvent) => {
    const p = toLocal(e)
    setView((v) => {
      const k = Math.max(0.45, Math.min(6, v.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)))
      // keep the point under the cursor pinned while the scale changes
      return { k, x: p.x - ((p.x - v.x) / v.k) * k, y: p.y - ((p.y - v.y) / v.k) * k }
    })
  }
  const resetView = () => setView({ k: 1, x: 0, y: 0 })

  const zoomBy = (f: number) =>
    setView((v) => {
      const k = Math.max(0.45, Math.min(6, v.k * f))
      const cx = VBW / 2
      const cy = VBH / 2
      return { k, x: cx - ((cx - v.x) / v.k) * k, y: cy - ((cy - v.y) / v.k) * k }
    })

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
  const drilled = !!graph && drillSub === graph.cidr
  // Expanding a segment hands it the ENTIRE plate: the gateway chain collapses to
  // a breadcrumb and the ~120 hosts spread across a wide ellipse that fills the
  // whole field (kept clear of the bottom-left agent panel).
  const meshCenter: Pt = { x: 706, y: 446 }
  const meshRX = 606
  const meshRY = 384
  const devPos: Record<string, Pt> = {}
  if (drilled && graph) {
    for (const dv of graph.devices) {
      devPos[dv.ip] = { x: meshCenter.x + dv.x * meshRX, y: meshCenter.y + dv.y * meshRY }
    }
  } else if (openSub) {
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
      onWheel={onWheel}
      onContextMenu={(e) => e.preventDefault()}
      style={{ cursor: panning ? 'grabbing' : 'grab' }}
    >
      <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
        {/* Drilling into a segment hands the whole field to that LAN: the ambient
            radar, the WAN-attack fan and the sibling interfaces fall away so the
            eye analyses one subnet's device relations, not the whole console. */}
        {!drilled ? <HudRadar core={core} /> : null}
        {!drilled && !wan ? <ThreatCone core={core} tempo={tempo} active /> : null}
        {!drilled
          ? layout.atk.map((a, i) => (
              <AttackBeam key={`ea${i}`} a={a.p} b={core} i={i} attempts={a.v} tempo={tempo} dim={!!wan && wan.ip !== a.ip} />
            ))
          : null}
        {!drilled
          ? layout.ifs.map((f, i) => {
              const focused = drillSub === f.sub?.cidr
              const fade = g === 'attack' || (drillSub && !focused) || !!wan
              return (
                <g key={`if${i}`}>
                  <Edge a={core} b={f.p} tone="t-flow" flows={f.it.flows} dim={!!fade} hot={focused} delay={0.4 + i * 0.06} tempo={tempo} />
                  {f.sub ? <Edge a={f.p} b={f.subP} tone="t-flow" flows={f.sub.flows} dim={!!fade} hot={focused} delay={0.6 + i * 0.06} tempo={tempo} /> : null}
                </g>
              )
            })
          : null}

        {!drilled ? (
          <>
            <g className="node gw-node appear" style={{ animationDelay: '0.3s' }}>
              <rect x={core.x - 60} y={core.y - 32} width="120" height="64" rx="1" />
              <text x={core.x} y={core.y - 5} className="n-title">{topo.core.name}</text>
              <text x={core.x} y={core.y + 14} className="n-sub">{topo.core.ip}</text>
            </g>
            <CoreReticle core={core} />
            <CoreImpact core={core} tempo={tempo} />
          </>
        ) : null}

        {/* drilled breadcrumb — the collapsed gateway chain, one click back to全网 */}
        {drilled && graph ? (
          <g className="mesh-crumb" onClick={() => onSub(null)} style={{ cursor: 'pointer' }}>
            <text x={30} y={200} className="mesh-crumb-t">
              ◂ {topo.core.name} · {layout.ifs.find((f) => f.sub?.cidr === graph.cidr)?.it.name ?? 'LAN'}
            </text>
            <text x={30} y={220} className="mesh-crumb-b">{lang === 'zh' ? '返回全网态势' : 'BACK TO CONSOLE'}</text>
          </g>
        ) : null}

        {!drilled
          ? layout.atk.map((a, i) => {
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
              <text x={a.p.x + 16} y={a.p.y + 12} className="n-v" textAnchor="start">{short(a.v)}<tspan className="probe-hint"> ▸ {lang === 'zh' ? '分析' : 'ANALYZE'}</tspan></text>
            </g>
          )
            })
          : null}
        {!drilled ? (
          <text x={70} y={120} className="zone-tag appear">
            {lang === 'zh'
              ? `外网入口 · ${short(stats.distinctSrc)} 来源 · ${stats.lockouts ?? 0} 次锁定`
              : `INTERNET IN · ${short(stats.distinctSrc)} sources · ${stats.lockouts ?? 0} lockouts`}
          </text>
        ) : null}

        {layout.ifs.map((f, i) => {
          const focused = drillSub === f.sub?.cidr
          if (drilled) return null
          const dimIf = g === 'attack' || (drillSub && !focused) || !!wan
          const highThreat = f.sub?.devices?.some((dv) => dv.threat === 'high')
          return (
            <g key={`ifn${i}`} className={`node appear ${dimIf ? 'node-dim' : ''}`} style={{ animationDelay: `${0.5 + i * 0.06}s` }}>
              <rect x={f.p.x - 52} y={f.p.y - 16} width="104" height="32" className="m-intf" />
              <text x={f.p.x} y={f.p.y - 1} className="n-intf">{f.it.name}</text>
              <text x={f.p.x} y={f.p.y + 12} className="n-sub">{short(f.it.flows)} {lang === 'zh' ? '连接' : 'conns'}</text>
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
                  <text x={f.subP.x + 18} y={f.subP.y + 12} className="n-v amber" textAnchor="start">{f.sub.hosts} {lang === 'zh' ? '台设备' : 'devices'} {focused ? '▾' : '▸'}</text>
                  {hover3DCidr === f.sub.cidr && hover3D ? (
                    <text x={f.subP.x + 18} y={f.subP.y + 26} className="map-ip" textAnchor="start">◂ {hover3D}</text>
                  ) : null}
                  {topoAlert && topoAlert.cidr === f.sub.cidr ? (
                    <g>
                      <circle cx={f.subP.x} cy={f.subP.y} r="21" className="alert-ring" />
                      <text x={f.subP.x + 18} y={f.subP.y + (hover3DCidr === f.sub.cidr ? 40 : 26)} className="alert-verdict" textAnchor="start">
                        ⚠ {topoAlert.ip} · {topoAlert.verdict || (lang === 'zh' ? '分析中…' : 'analyzing…')}
                      </text>
                    </g>
                  ) : null}
                </g>
              ) : null}
            </g>
          )
        })}

        {/* open subnet → the FULL segment: every host, every mined relation */}
        {drilled && graph ? (
                <g key={`mesh-${graph.cidr}`}>
                  <SubnetGraphLayer
                    graph={graph}
                    analysis={graphAnalysis}
                    center={meshCenter}
                    rx={meshRX}
                    ry={meshRY}
                    lang={lang}
                    hoverIp={hoverDev}
                    selectedIp={drillDev}
                    marks={marks}
                    showPanel={!threat}
                    onHover={onHoverDev}
                    onPick={(dv: GraphDevice) =>
                      onDev(
                        drillDev === dv.ip
                          ? null
                          : { ip: dv.ip, flows: dv.flows, deny: dv.deny, accept: dv.accept, threat: dv.threat, top_ports: dv.topPorts },
                        graph.cidr,
                      )
                    }
                    onAnalyze={() => onGraphAnalyze(graph.cidr)}
                    onCloseAnalysis={onCloseGraphAnalysis}
                  />
                </g>
          ) : null}

        {/* fallback: subnet opened but no mined graph (or still loading) */}
        {layout.ifs.map((f) => {
          if (!f.sub || drillSub !== f.sub.cidr || drilled) return null
          const devs = (f.sub.devices ?? []).slice(0, 7)
          const subNode: Pt = { x: f.subP.x, y: f.subP.y }
          const hi = devs.filter((d) => d.threat === 'high').length
          return (
            <g key={`tree-${f.sub.cidr}`}>
              <Float x={subNode.x - 96} y={subNode.y - 84} w={232} tone={hi ? 'alert' : 'flow'}
                lines={[
                  { k: lang === 'zh' ? '网段' : 'NET', v: f.sub.cidr },
                  { k: lang === 'zh' ? '设备' : 'DEVICES', v: `${f.sub.hosts} · ${short(f.sub.flows)} ${lang === 'zh' ? '连接' : 'conns'}` },
                  { k: lang === 'zh' ? '可疑' : 'FLAGGED', v: lang === 'zh' ? `${hi} 高危 · ${short(f.sub.accept)} 放行` : `${hi} high · ${short(f.sub.accept)} allowed` },
                ]} />
              <g className="batch-trig" onClick={() => onBatch(f.sub!.cidr)} style={{ cursor: 'pointer' }}>
                <rect x={subNode.x - 8} y={subNode.y + 24} width="132" height="22" />
                <text x={subNode.x + 58} y={subNode.y + 39}>⚡ {lang === 'zh' ? '全部分析' : 'ANALYZE ALL'}</text>
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
                        <text x={dp.x + 14} y={dp.y + 12} className={`n-v ${dv.threat === 'high' ? '' : 'amber'}`} textAnchor="start">{short(dv.deny)} {lang === 'zh' ? '次拦截' : 'blocked'}</text>
                      )}
                    </g>
                    {open ? (
                      <Float x={dp.x - 6} y={dp.y - 64} w={206} tone={alert ? 'alert' : dv.threat}
                        lines={[
                          { k: lang === 'zh' ? '拦截' : 'BLOCKED', v: lang === 'zh' ? `${short(dv.deny)} · ${dv.accept} 放行` : `${short(dv.deny)} · ${dv.accept} allowed` },
                          { k: lang === 'zh' ? '端口' : 'PORTS', v: dv.top_ports.map((p) => `:${p}`).join(' ') },
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

        {/* 3D constellation portal — hidden while the WAN pivots or a segment mesh own the right field */}
        {meshCount > 0 && !wan && !drilled ? (
          <g className="portal3d" onClick={onOpen3D} style={{ cursor: 'pointer' }}>
            {layout.ifs.map((f, i) => (f.sub ? <path key={i} d={bez(f.subP, { x: 1252, y: 372 })} className="portal-link" /> : null))}
            <circle cx={1252} cy={372} r="30" className="portal-halo" />
            <circle cx={1252} cy={372} r="20" className="portal-ring" />
            <text x={1252} y={377} className="portal-glyph" textAnchor="middle">⬡</text>
            <text x={1252} y={418} className="portal-label" textAnchor="middle">{meshLoading ? (lang === 'zh' ? '载入中…' : 'loading…') : lang === 'zh' ? '3D 全网视图' : '3D NETWORK VIEW'}</text>
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
                      <div className="an-head"><span className="an-kicker">{lang === 'zh' ? 'AI 分析' : 'AI'} · {threat.ip}</span></div>
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
                          <span className="an-kicker">{lang === 'zh' ? 'AI 分析' : 'AI'} · {threat.ip}</span>
                          <button className="an-x" onClick={onCloseThreat}>✕</button>
                        </div>
                        <div className="an-verdict">
                          <span className={`sev-dot ${threat.severity}`} />
                          <Scramble className="an-vtxt" text={threat.verdict ?? ''} />
                          <span className={`sev-tag ${threat.severity ?? ''}`}>{sevLabel(threat.severity, lang)}</span>
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
                      {lang === 'zh' ? '影响范围' : 'IMPACT MAP'}
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
                  {short(wan.attempts ?? 0)} {lang === 'zh' ? '次登录尝试' : 'login attempts'}
                </text>

                {/* /24 lockstep sibling cluster */}
                <text x={anchorP.x} y={anchorP.y - 26} className="wan-netblock" textAnchor="start">
                  ◇ {lang === 'zh' ? '整段 IP 协同' : 'whole IP block'} · {short(wan.netblockAttempts ?? 0)}
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
                    <text x={fg.x} y={fg.y + 54} className="wan-lock" textAnchor="middle">⊘ {wan.lockouts ?? 0} {lang === 'zh' ? '次账号锁定' : 'account lockouts'}</text>
                  </>
                ) : null}

                {/* cross-canvas post-compromise pivots */}
                {inter.length ? (
                  <text x={1132} y={112} className="impact-tag" textAnchor="start">
                    {lang === 'zh' ? '内网扩散' : 'INTERNAL SPREAD'}
                  </text>
                ) : null}
                {inter.map((c, k) => {
                  const span = inter.length > 1 ? (VBH - 320) / (inter.length - 1) : 0
                  const ip_: Pt = { x: 1132, y: clamp(150 + k * span, 130, VBH - 90) }
                  return (
                    <g key={c.ip} className="branch-in wan-pivot">
                      <path d={bez(fg, ip_)} className="wan-pivot-link" />
                      <circle cx={ip_.x} cy={ip_.y} r="6" className="m-dev high" />
                      <text x={ip_.x + 12} y={ip_.y - 1} className="n-ip" textAnchor="start">{c.ip} · {short(c.deny ?? 0)} {lang === 'zh' ? '次拦截' : 'blocked'}</text>
                      <text x={ip_.x + 12} y={ip_.y + 12} className="wan-rel" textAnchor="start">{clipS(c.relation, 26)}</text>
                    </g>
                  )
                })}

                {/* deep-analysis panel */}
                {wan.loading ? (
                  <foreignObject x={36} y={678} width={580} height={130}>
                    <div className="an-panel wan-panel">
                      <div className="an-head"><span className="an-kicker">{lang === 'zh' ? 'AI 分析' : 'AI'} · {wan.ip}</span></div>
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
                        <span className="an-kicker">{lang === 'zh' ? 'AI 分析 · 入侵判定' : 'AI · INTRUSION'} · {wan.ip}</span>
                        <button className="an-x" onClick={onCloseWan}>✕</button>
                      </div>
                      <div className="an-verdict">
                        <span className={`sev-dot ${wan.severity}`} />
                        <Scramble className="an-vtxt" text={wan.verdict ?? ''} />
                        <span className={`sev-tag ${wan.severity ?? ''}`}>{sevLabel(wan.severity, lang)}</span>
                        {typeof wan.confidence === 'number' ? <span className="wan-conf">{Math.round(wan.confidence * 100)}%</span> : null}
                      </div>
                      <div className="kc-rail">
                        {KILLCHAIN.map((s) => (
                          <span key={s.k} className={`kc-step ${s.k === stage ? 'on' : ''}`}>{lang === 'zh' ? s.zh : s.en}</span>
                        ))}
                      </div>
                      <p className="an-analysis">{wan.campaign}</p>
                      <div className="wan-meta">
                        <span><i>{lang === 'zh' ? '疑似来自' : 'SOURCE'}</i>{wan.attribution}</span>
                        <span><i className="bad">{lang === 'zh' ? '影响' : 'IMPACT'}</i>{wan.blast}</span>
                      </div>
                      {wan.actions && wan.actions.length ? (
                        <ol className="wan-actions">
                          {wan.actions.map((a, i) => (<li key={i}>{a}</li>))}
                        </ol>
                      ) : null}
                      {onPentest ? (
                        <button className="wan-pentest-cta" onClick={onPentest}>
                          <span className="wpc-txt">{lang === 'zh' ? '去实测暴露面' : 'TEST THE EXPOSURE'}</span>
                          <span className="wpc-arrow">▸</span>
                        </button>
                      ) : null}
                    </div>
                  </foreignObject>
                )}
              </g>
            )
          })()
        ) : null}

        {!drilled ? (
          <HudReadouts
            stats={stats}
            meshCount={meshCount}
            ifCount={layout.ifs.length}
            subCount={layout.ifs.filter((f) => f.sub).length}
            lang={lang}
            showStatus={!threat && !wan}
          />
        ) : null}
      </g>

      {/* viewport controls — pinned to the plate (never zoomed), sits above the
          legend on the right so it never collides with the bottom-left agent panel */}
      <foreignObject x={VBW - 218} y={drilled ? VBH - 174 : VBH - 44} width={214} height={32} className="zoom-fo">
        <div className="zoom-ctl">
          <button onClick={() => zoomBy(1 / 1.3)} title="zoom out">−</button>
          <span className="zoom-k">{Math.round(view.k * 100)}%</span>
          <button onClick={() => zoomBy(1.3)} title="zoom in">+</button>
          <button className="zoom-reset" onClick={resetView}>{lang === 'zh' ? '复位' : 'RESET'}</button>
          <span className="zoom-hint">{lang === 'zh' ? '拖动平移 · 滚轮缩放' : 'drag · scroll'}</span>
        </div>
      </foreignObject>
    </svg>
  )
}
