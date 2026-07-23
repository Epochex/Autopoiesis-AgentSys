/* 事件驱动全链路拓扑剧场 / EVENT-DRIVEN FULL-CHAIN TOPOLOGY THEATER
 *
 * A NetOps live-feed event promoted onto page 1. Unlike the resting console
 * (one segment drilled at a time), the theater expands the WHOLE topology at
 * once — every subnet, every device the firewall's mined device graph knows —
 * and plays the event across it:
 *
 *   WAN field → FortiGate → interfaces → all subnet clusters (all nodes)
 *                    ╲ syslog (real: R230 is the configured sink)
 *                     R230 ── NetOps pipeline rail (correlator → … → remediation)
 *
 * Honesty rules carried over from the console:
 *   - every node is a real mined device; positions inside a cluster reuse the
 *     graph's own layout, the cluster placement is a reading order, not a map
 *   - the event's device key maps onto the topology ONLY through the payload's
 *     real anchors (R230边缘节点 → anchor "R230" = 192.168.1.23); an unmapped
 *     key is stated as "not on the real network", never guessed
 *   - the pipeline rail lights exactly the stages the landed record lit
 */
import { useMemo, useState } from 'react'
import type { DataStats, SubnetGraph, TheaterEvent, Topology } from '../types'
import type { Lang } from '../i18n'
import { PIPELINE } from './netops-pipeline'
import './theater.css'

type Pt = { x: number; y: number }
const bez = (a: Pt, b: Pt) => `M${a.x} ${a.y} C ${(a.x + b.x) / 2} ${a.y}, ${(a.x + b.x) / 2} ${b.y}, ${b.x} ${b.y}`
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

const CORE: Pt = { x: 400, y: 470 }
const RAIL_Y = 64
const RAIL_X0 = 600
const RAIL_DX = 244
/* WAN tally block (1 mark = 1 real distinct source, a reading order not a map) */
const TALLY = { x: 58, y: 330, cols: 18, cell: 6.4 }

const ROLE_ZH: Record<string, string> = {
  camera: '摄像头', intercom: '门禁', mobile: '移动端', workstation: '工作站', server: '服务器', unknown: '未识别',
}
/* stage-2 hue families: one per device ROLE (hulls + dots) and one per pipeline
 * 环节 (rail + chain). Values live in theater.css; these lists drive markup. */
const HULL_ROLES = ['workstation', 'camera', 'mobile', 'intercom', 'server'] as const

/** Andrew monotone-chain convex hull, for the role-encircling fields. */
function hull(pts: Pt[]): Pt[] {
  if (pts.length < 3) return pts
  const p = [...pts].sort((a, b) => a.x - b.x || a.y - b.y)
  const cross = (o: Pt, a: Pt, b: Pt) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)
  const lo: Pt[] = []
  for (const q of p) {
    while (lo.length >= 2 && cross(lo[lo.length - 2], lo[lo.length - 1], q) <= 0) lo.pop()
    lo.push(q)
  }
  const hi: Pt[] = []
  for (const q of [...p].reverse()) {
    while (hi.length >= 2 && cross(hi[hi.length - 2], hi[hi.length - 1], q) <= 0) hi.pop()
    hi.push(q)
  }
  return [...lo.slice(0, -1), ...hi.slice(0, -1)]
}
/** hull → smooth closed path, expanded a little from its centroid */
function hullPath(pts: Pt[], pad: number): string {
  if (!pts.length) return ''
  const cx = pts.reduce((a, p) => a + p.x, 0) / pts.length
  const cy = pts.reduce((a, p) => a + p.y, 0) / pts.length
  const ex = pts.map((p) => {
    const d = Math.hypot(p.x - cx, p.y - cy) || 1
    return { x: p.x + ((p.x - cx) / d) * pad, y: p.y + ((p.y - cy) / d) * pad }
  })
  if (ex.length === 1) return `M ${ex[0].x - pad} ${ex[0].y} a ${pad} ${pad} 0 1 0 ${pad * 2} 0 a ${pad} ${pad} 0 1 0 ${-pad * 2} 0`
  if (ex.length === 2) return `M ${ex[0].x} ${ex[0].y} L ${ex[1].x} ${ex[1].y}`
  const mid = (a: Pt, b: Pt) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 })
  let d = `M ${mid(ex[ex.length - 1], ex[0]).x} ${mid(ex[ex.length - 1], ex[0]).y}`
  for (let i = 0; i < ex.length; i++) {
    const m = mid(ex[i], ex[(i + 1) % ex.length])
    d += ` Q ${ex[i].x} ${ex[i].y} ${m.x} ${m.y}`
  }
  return d + ' Z'
}

/** a small train of pulses flowing along a path (the stage-2 motion language) */
function Flow({ d, n = 3, dur = 2.6, cls = '' }: { d: string; n?: number; dur?: number; cls?: string }) {
  return (
    <g pointerEvents="none">
      {Array.from({ length: n }).map((_, i) => (
        <circle key={i} r={2.6} className={`th-pulse ${cls}`}>
          <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`${(i * dur) / n}s`} path={d} />
        </circle>
      ))}
    </g>
  )
}

/* cluster placement presets, biggest device-count first — a reading order that
 * fills the right field of the 1920x1000 plate and stays clear of the WAN column */
const REGIONS: { c: Pt; rx: number; ry: number }[] = [
  { c: { x: 1300, y: 650 }, rx: 420, ry: 245 },
  { c: { x: 1030, y: 250 }, rx: 300, ry: 162 },
  { c: { x: 1720, y: 220 }, rx: 150, ry: 110 },
  { c: { x: 620, y: 860 }, rx: 130, ry: 70 },
]

export function TheaterStage({
  topo, stats, graphs, theater, lang, onClose, onNode, busyIp, impactNodes, analysisIp,
}: {
  topo: Topology
  stats: DataStats
  graphs: Record<string, SubnetGraph>
  theater: TheaterEvent
  lang: Lang
  onClose: () => void
  /** stage-3 hook: bidirectional node analysis (internal device / WAN attacker) */
  onNode?: (ip: string, side: 'internal' | 'wan') => void
  busyIp?: string | null
  /** nodes on an open analysis' attack path — highlighted red-dashed */
  impactNodes?: string[]
  /** the entity the open analysis is about — impact lines radiate from it */
  analysisIp?: string | null
}) {
  const zh = lang === 'zh'
  const [hover, setHover] = useState<{ ip: string; p: Pt; role: string; vendor: string; threat: string; deny: number } | null>(null)
  const [sel, setSel] = useState<string | null>(null)

  /* map the event's NetOps device key onto a real topology anchor — or admit it */
  const anchor = useMemo(() => {
    if (!theater.device) return null
    return topo.anchors.find((a) => theater.device.includes(a.name)) ?? null
  }, [theater.device, topo.anchors])

  /* ── full expansion: every subnet gets a region, every mined device a node ── */
  const scene = useMemo(() => {
    const withGraph = topo.subnets
      .filter((s) => graphs[s.cidr])
      .sort((a, b) => (graphs[b.cidr]?.devices.length ?? 0) - (graphs[a.cidr]?.devices.length ?? 0))
    const stubs = topo.subnets.filter((s) => !graphs[s.cidr])

    const regions = withGraph.map((s, i) => {
      const r = REGIONS[i] ?? REGIONS[REGIONS.length - 1]
      const g = graphs[s.cidr]
      const pos: Record<string, Pt> = {}
      for (const d of g.devices) pos[d.ip] = { x: r.c.x + d.x * r.rx * 0.88, y: r.c.y + d.y * r.ry * 0.86 }
      return { sub: s, g, ...r, pos }
    })
    const stubNodes = stubs.map((s, i) => ({ sub: s, p: { x: 700 + i * 190, y: 905 } as Pt }))

    /* one flat ip→pos index across every cluster, for cross-cluster lines */
    const posOf: Record<string, Pt> = {}
    for (const r of regions) Object.assign(posOf, r.pos)

    /* interfaces: those serving subnets sit on the core→cluster path; the rest
     * (real ports with real flows, no mined subnet) hang as short spokes */
    const lan = topo.interfaces.filter((it) => it.kind === 'lan')
    const ifNodes = lan.map((it, i) => {
      const served = [...regions.map((r) => ({ p: r.c, kind: 'region' as const, key: r.sub.cidr })), ...stubNodes.map((s) => ({ p: s.p, kind: 'stub' as const, key: s.sub.cidr }))]
        .filter((t) => topo.subnets.find((s) => s.cidr === t.key)?.intf === it.name)
      const centroid = served.length
        ? { x: served.reduce((a, t) => a + t.p.x, 0) / served.length, y: served.reduce((a, t) => a + t.p.y, 0) / served.length }
        : { x: CORE.x + 130, y: CORE.y + 190 + i * 64 }
      const t = served.length ? 0.32 : 1
      const p: Pt = { x: CORE.x + (centroid.x - CORE.x) * t, y: CORE.y + (centroid.y - CORE.y) * t }
      return { it, p, served }
    })
    return { regions, stubNodes, ifNodes, posOf }
  }, [topo, graphs])

  const anchorP = anchor ? scene.posOf[anchor.ip] : undefined

  /* the focus whose relations are reinforced: a clicked node wins, else the event anchor */
  const focusIp = sel ?? anchor?.ip ?? null
  const { hotEdges, relIps } = useMemo(() => {
    const rel = new Set<string>()
    const edges: { a: Pt; b: Pt; evidence: string; observed: boolean }[] = []
    if (focusIp) {
      for (const r of scene.regions) {
        for (const e of r.g.edges) {
          if (e.src !== focusIp && e.dst !== focusIp) continue
          const a = scene.posOf[e.src]
          const b = scene.posOf[e.dst]
          if (!a || !b) continue
          rel.add(e.src === focusIp ? e.dst : e.src)
          edges.push({ a, b, evidence: e.evidence, observed: e.observed })
        }
      }
    }
    return { hotEdges: edges, relIps: rel }
  }, [focusIp, scene])

  const impact = useMemo(() => new Set(impactNodes ?? []), [impactNodes])
  const rows = Math.ceil(stats.distinctSrc / TALLY.cols)
  const railStages = PIPELINE.map((p, i) => ({ ...p, p: { x: RAIL_X0 + i * RAIL_DX, y: RAIL_Y } as Pt }))
  const hotStageSet = new Set(theater.stageIds)
  const firstHot = railStages.find((s) => hotStageSet.has(s.id)) ?? railStages[0]
  const atk = stats.topAttackerSrc.slice(0, 3)

  const kindLab = theater.kind === 'alert' ? (zh ? '实时告警' : 'LIVE ALERT') : theater.kind === 'suggestion' ? (zh ? '处置建议' : 'SUGGESTION') : (zh ? '自由浏览' : 'BROWSE')

  return (
    <g className="theater">
      {/* ── NetOps pipeline rail: the chain the landed event actually lit ── */}
      <g className="th-rail">
        <text x={RAIL_X0 - 12} y={RAIL_Y - 26} className="th-rail-kick" textAnchor="start">
          {zh ? 'NetOps 流处理链 · 运行于 R230' : 'NETOPS STREAM CHAIN · ON R230'}
        </text>
        {railStages.map((s, i) => {
          const hot = hotStageSet.has(s.id)
          const armHot = i < railStages.length - 1 && hot && hotStageSet.has(railStages[i + 1].id)
          const armD = `M ${s.p.x + 74} ${s.p.y} L ${railStages[i + 1]?.p.x - 74} ${railStages[i + 1]?.p.y}`
          return (
            <g key={s.id} className={`th-stage st-${s.id} ${hot ? 'hot' : ''}`}>
              {i < railStages.length - 1 ? (
                <line x1={s.p.x + 74} y1={s.p.y} x2={railStages[i + 1].p.x - 74} y2={railStages[i + 1].p.y}
                  className={`th-rail-arm ${armHot ? 'hot' : ''}`} />
              ) : null}
              {armHot ? <Flow d={armD} n={2} dur={1.8} cls={`st-${s.id}`} /> : null}
              <rect x={s.p.x - 70} y={s.p.y - 15} width={140} height={30} className="th-stage-box" />
              <text x={s.p.x} y={s.p.y - 2} className="th-stage-n" textAnchor="middle">{String(i + 1).padStart(2, '0')}</text>
              <text x={s.p.x} y={s.p.y + 10} className="th-stage-l" textAnchor="middle">{zh ? s.zh : s.en}</text>
            </g>
          )
        })}
      </g>

      {/* event chain: the mapped device node climbs into the pipeline rail —
          colored by the first 环节 it lights, with pulses flowing device → rail */}
      {theater.kind !== 'browse' && anchorP ? (
        <g className={`th-chain st-${firstHot.id}`} pointerEvents="none">
          <path d={bez(anchorP, { x: firstHot.p.x, y: firstHot.p.y + 15 })} className="th-chain-line" />
          <Flow d={bez(anchorP, { x: firstHot.p.x, y: firstHot.p.y + 15 })} n={4} dur={3.2} cls={`st-${firstHot.id}`} />
          <text x={(anchorP.x + firstHot.p.x) / 2} y={(anchorP.y + firstHot.p.y) / 2 - 8} className="th-chain-lab" textAnchor="middle">
            {theater.scenario || (zh ? '事件流入' : 'event ingest')} ↗
          </text>
        </g>
      ) : null}

      {/* ── WAN field: the real source tally + named attackers, kept on stage ── */}
      <g className="th-wan">
        <text x={TALLY.x} y={TALLY.y - 30} className="th-wan-kick">{zh ? '外网 · 暴力破解' : 'WAN · BRUTE FORCE'}</text>
        <text x={TALLY.x} y={TALLY.y - 14} className="th-wan-cap">{stats.distinctSrc} {zh ? '来源 · 每格1个' : 'sources · 1 mark each'}</text>
        {Array.from({ length: stats.distinctSrc }).map((_, i) => (
          <rect key={i} x={TALLY.x + (i % TALLY.cols) * TALLY.cell} y={TALLY.y + Math.floor(i / TALLY.cols) * TALLY.cell} width={3.4} height={3.4} className="th-src" />
        ))}
        {(() => {
          const d = `M ${TALLY.x + TALLY.cols * TALLY.cell + 8} ${TALLY.y + (rows * TALLY.cell) / 2} Q ${(TALLY.x + CORE.x) / 2} ${TALLY.y + (rows * TALLY.cell) / 2} ${CORE.x - 74} ${CORE.y - 8}`
          return (
            <>
              <path d={d} className="th-wanline" pointerEvents="none" />
              <Flow d={d} n={3} dur={2.2} cls="wan" />
            </>
          )
        })()}
        {/* attack vectors render OUTSIDE the clickable groups — a line stretching to
            the core would swallow each trigger's bounding-box centre (the exact
            portal-links failure this codebase already documents) */}
        <g pointerEvents="none">
          {atk.map((a, i) => (
            <line key={a[0]} x1={92} y1={600 + i * 46} x2={CORE.x - 74} y2={CORE.y + 6} className="th-wanline thin" />
          ))}
        </g>
        {atk.map((a, i) => {
          const p: Pt = { x: 84, y: 600 + i * 46 }
          const on = busyIp === a[0]
          return (
            <g key={a[0]} className={`th-atk ${impact.has(a[0]) ? 'impact' : ''} ${on ? 'busy' : ''}`}
              style={{ cursor: onNode ? 'pointer' : 'default' }}
              onClick={() => onNode?.(a[0], 'wan')}>
              <rect x={p.x - 10} y={p.y - 14} width={150} height={30} fill="transparent" />
              <rect x={p.x - 5} y={p.y - 5} width={10} height={10} transform={`rotate(45 ${p.x} ${p.y})`} className="th-atk-mark" />
              <text x={p.x + 14} y={p.y - 1} className="th-atk-ip" textAnchor="start">{a[0]}</text>
              <text x={p.x + 14} y={p.y + 12} className="th-atk-v" textAnchor="start">{short(a[1])} {zh ? '次尝试' : 'attempts'}{onNode ? <tspan className="th-atk-hint"> ▸ {zh ? '研判' : 'ASSESS'}</tspan> : null}</text>
            </g>
          )
        })}
        <text x={TALLY.x} y={TALLY.y + rows * TALLY.cell + 22} className="th-wan-cap">{stats.lockouts ?? 0} {zh ? '次锁定 · 已遏制' : 'lockouts · contained'}</text>
      </g>

      {/* ── FortiGate core ── */}
      <g className="th-core">
        <rect x={CORE.x - 66} y={CORE.y - 30} width={132} height={60} className="th-core-box" />
        <text x={CORE.x} y={CORE.y - 6} className="th-core-t" textAnchor="middle">{topo.core.name}</text>
        <text x={CORE.x} y={CORE.y + 13} className="th-core-s" textAnchor="middle">{topo.core.ip}</text>
      </g>

      {/* syslog spine: the real full-mirror feed FortiGate → R230 (configured sink) */}
      {(() => {
        const r230 = topo.anchors.find((a) => a.role.includes('syslog'))
        const p = r230 ? scene.posOf[r230.ip] : undefined
        if (!p) return null
        const d = bez({ x: CORE.x + 40, y: CORE.y - 24 }, p)
        return (
          <g className="th-syslog" pointerEvents="none">
            <path d={d} className="th-syslog-line" />
            <Flow d={d} n={3} dur={2.8} cls="net" />
            <text x={(CORE.x + p.x) / 2} y={(CORE.y - 24 + p.y) / 2 - 8} className="th-syslog-lab" textAnchor="middle">syslog {zh ? '全量镜像' : 'full mirror'} ▸</text>
          </g>
        )
      })()}

      {/* ── interfaces + spokes to every cluster / stub ── */}
      {scene.ifNodes.map((f) => (
        <g key={f.it.name} className="th-if">
          <line x1={CORE.x + 66} y1={CORE.y} x2={f.p.x} y2={f.p.y} className="th-spoke" pointerEvents="none" />
          {f.served.map((t) => (
            <line key={t.key} x1={f.p.x} y1={f.p.y} x2={t.p.x} y2={t.p.y} className="th-spoke" pointerEvents="none" />
          ))}
          <rect x={f.p.x - 44} y={f.p.y - 13} width={88} height={26} className="th-if-box" />
          <text x={f.p.x} y={f.p.y - 1} className="th-if-t" textAnchor="middle">{f.it.name}</text>
          <text x={f.p.x} y={f.p.y + 10} className="th-if-s" textAnchor="middle">{short(f.it.flows)} {zh ? '连接' : 'conns'}</text>
        </g>
      ))}

      {/* ── every subnet, every node ── */}
      {scene.regions.map((r) => {
        const st = r.g.stats
        return (
          <g key={r.sub.cidr} className="th-cluster">
            <ellipse cx={r.c.x} cy={r.c.y} rx={r.rx} ry={r.ry} className={`th-region ${impact.has(r.sub.cidr) ? 'impact' : ''}`} pointerEvents="none" />
            <text x={r.c.x - r.rx + 14} y={r.c.y - r.ry + 4} className="th-region-lab">
              {r.sub.cidr} · {r.g.devices.length} {zh ? '台设备' : 'devices'} · {st.edges} {zh ? '条关联' : 'relations'}
            </text>
            {/* 角色分色圈定: every role with members gets its convex field, in its hue */}
            <g className="th-hulls" pointerEvents="none">
              {HULL_ROLES.map((role) => {
                const pts = r.g.devices.filter((d) => d.role === role).map((d) => r.pos[d.ip])
                if (!pts.length) return null
                const d = hullPath(hull(pts), 16)
                const top = pts.reduce((a, p) => (p.y < a.y ? p : a), pts[0])
                return (
                  <g key={role} className={`th-hull role-${role}`}>
                    <path d={d} className="th-hull-path" />
                    <text x={top.x} y={top.y - 22} className="th-hull-lab" textAnchor="middle">
                      {zh ? ROLE_ZH[role] : role} ×{pts.length}
                    </text>
                  </g>
                )
              })}
            </g>
            {/* mined relations: the faint ground truth mesh */}
            <g className="th-mesh" pointerEvents="none">
              {r.g.edges.map((e, i) => {
                const a = r.pos[e.src]
                const b = r.pos[e.dst]
                if (!a || !b) return null
                return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={`th-edge ${e.observed ? 'obs' : ''}`} />
              })}
            </g>
            {/* devices */}
            {r.g.devices.map((d) => {
              const p = r.pos[d.ip]
              const isAnchor = anchor?.ip === d.ip
              const isFocus = focusIp === d.ip
              const isRel = relIps.has(d.ip)
              const anchorMeta = topo.anchors.find((a) => a.ip === d.ip)
              const labeled = isAnchor || isFocus || !!anchorMeta || d.threat === 'high'
              return (
                <g key={d.ip}
                  className={`th-dev role-${d.role} t-${d.threat} ${isFocus ? 'focus' : ''} ${isRel ? 'rel' : ''} ${impact.has(d.ip) ? 'impact' : ''} ${busyIp === d.ip ? 'busy' : ''}`}
                  style={{ cursor: 'pointer' }}
                  onMouseEnter={() => setHover({ ip: d.ip, p, role: d.role, vendor: d.vendor, threat: d.threat, deny: d.deny })}
                  onMouseLeave={() => setHover(null)}
                  onClick={() => setSel(sel === d.ip ? null : d.ip)}>
                  {isFocus ? <circle cx={p.x} cy={p.y} r={13} className="th-focus-ring" /> : null}
                  <circle cx={p.x} cy={p.y} r={isFocus ? 6.5 : d.threat === 'high' ? 5 : 3.4} className="th-dot" />
                  {labeled ? (
                    <text x={p.x + (isFocus ? 12 : 9)} y={p.y + (isFocus ? -10 : 3)} className={`th-dev-lab ${isFocus ? 'focus' : ''}`} textAnchor="start">
                      {anchorMeta ? `${anchorMeta.name} · ` : ''}{d.ip}
                    </text>
                  ) : null}
                </g>
              )
            })}
          </g>
        )
      })}

      {/* single-host subnets: stated as stubs, not padded into fake clusters */}
      {scene.stubNodes.map((s) => (
        <g key={s.sub.cidr} className="th-stub">
          <rect x={s.p.x - 7} y={s.p.y - 7} width={14} height={14} className="th-stub-box" />
          <text x={s.p.x + 14} y={s.p.y - 1} className="th-stub-t" textAnchor="start">{s.sub.cidr}</text>
          <text x={s.p.x + 14} y={s.p.y + 11} className="th-stub-s" textAnchor="start">{s.sub.hosts} {zh ? '台 · 无关系图' : 'host · no mesh'}</text>
        </g>
      ))}

      {/* reinforced relations of the focus node — drawn last, over everything */}
      <g className="th-hot" pointerEvents="none">
        {hotEdges.map((e, i) => (
          <line key={i} x1={e.a.x} y1={e.a.y} x2={e.b.x} y2={e.b.y} className={`th-edge-hot ${e.observed ? 'obs' : ''}`} />
        ))}
      </g>

      {/* bidirectional analysis impact: the attack path radiates from the analyzed
          entity (WAN attacker or internal host) to every on-canvas impact node */}
      {analysisIp ? (() => {
        const atkIdx = atk.findIndex((a) => a[0] === analysisIp)
        const src: Pt | undefined = scene.posOf[analysisIp] ?? (atkIdx >= 0 ? { x: 84, y: 600 + atkIdx * 46 } : undefined)
        if (!src) return null
        return (
          <g className="th-impact" pointerEvents="none">
            {[...impact].map((n) => {
              const p: Pt | undefined = n === topo.core.ip ? CORE : scene.posOf[n]
              if (!p || n === analysisIp) return null
              return <path key={n} d={bez(src, p)} className="th-impact-line" />
            })}
          </g>
        )
      })() : null}

      {/* the focus node's assess trigger — DeepSeek is a deliberate click, never a
          side effect of exploring */}
      {onNode && sel && scene.posOf[sel] ? (
        <foreignObject
          x={clamp(scene.posOf[sel].x - 70, 8, 1920 - 210)}
          y={clamp(scene.posOf[sel].y + 18, 8, 940)}
          width={200} height={34} className="th-chip-fo">
          <button className="th-chip" onClick={() => onNode(sel, 'internal')} disabled={busyIp === sel}>
            {busyIp === sel ? (zh ? '研判中…' : 'ASSESSING…') : `⚡ ${zh ? '内部设备研判' : 'ASSESS HOST'} · DeepSeek`}
          </button>
        </foreignObject>
      ) : null}

      {/* hover tooltip */}
      {hover ? (
        <g className="th-tip" pointerEvents="none">
          <text x={clamp(hover.p.x + 12, 20, 1740)} y={clamp(hover.p.y - 14, 30, 970)} className="th-tip-t" textAnchor="start">
            {hover.ip} · {zh ? ROLE_ZH[hover.role] ?? hover.role : hover.role}
            {hover.vendor !== 'unknown' ? ` · ${hover.vendor}` : ''} · {short(hover.deny)} {zh ? '拦截' : 'deny'}
          </text>
        </g>
      ) : null}

      {/* color legend: 环节 hues on the rail, 角色 hues on the clusters */}
      <foreignObject x={20} y={946} width={900} height={40} className="th-legend-fo">
        <div className="th-legend">
          <span className="th-legend-k">{zh ? '角色分色' : 'ROLES'}</span>
          {HULL_ROLES.map((role) => (
            <span key={role} className={`th-legend-chip role-${role}`}><i />{zh ? ROLE_ZH[role] : role}</span>
          ))}
          <span className="th-legend-k">{zh ? '· 环节分色' : '· STAGES'}</span>
          {PIPELINE.map((p) => (
            <span key={p.id} className={`th-legend-chip st-${p.id}`}><i />{zh ? p.zh : p.en}</span>
          ))}
        </div>
      </foreignObject>

      {/* ── event banner ── */}
      <foreignObject x={20} y={112} width={430} height={190} className="th-banner-fo">
        <div className="th-banner">
          <div className="th-banner-h">
            <span className="th-banner-kind">{kindLab}</span>
            {theater.severity ? <span className={`th-banner-sev s-${theater.severity}`}>{theater.priority || theater.severity}</span> : null}
            <button className="th-banner-x" onClick={onClose}>✕ {zh ? '退出剧场' : 'EXIT'}</button>
          </div>
          {theater.kind !== 'browse' ? (
            <>
              <div className="th-banner-dev">{theater.device || '—'}</div>
              <div className="th-banner-sum">{theater.summary || theater.scenario || ''}</div>
              <div className="th-banner-meta">
                {theater.ts.replace('T', ' ').replace(/\+.*$/, '')} ·{' '}
                {anchor
                  ? (zh ? `已映射 → ${anchor.name} ${anchor.ip}` : `mapped → ${anchor.name} ${anchor.ip}`)
                  : (zh ? '实验流标识 · 不在实网拓扑,不作杜撰映射' : 'lab-stream key · not on the real network')}
              </div>
            </>
          ) : (
            <div className="th-banner-sum">{zh ? '全拓扑展开 · 点击任意节点看其真实关联' : 'Full topology · click any node for its mined relations'}</div>
          )}
          <div className="th-banner-note">
            {zh ? `实网设备图 ${scene.regions.reduce((a, r) => a + r.g.devices.length, 0)} 节点 · NetOps 实时落地事件` : `${scene.regions.reduce((a, r) => a + r.g.devices.length, 0)} mined nodes · landed NetOps events`}
          </div>
        </div>
      </foreignObject>
    </g>
  )
}
