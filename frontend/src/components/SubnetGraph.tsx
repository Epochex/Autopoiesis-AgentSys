import { useMemo } from 'react'
import type { DeviceProfile, GraphAnalysis, GraphDevice, ProfilePeer, SubnetGraph } from '../types'
import type { Lang } from '../i18n'

type Pt = { x: number; y: number }

const KIND_ZH: Record<string, string> = {
  clash: '会话冲突 · IP 重复',
  bcast: '同广播域',
  codst: '同目的服务',
  fleet: '同厂商 OUI',
  family: '同命名族',
  lease: 'DHCP 同步续约',
  portfp: '端口指纹相同',
  wan: '互联网外联',
}
const KIND_EN: Record<string, string> = {
  clash: 'session clash / dup IP',
  bcast: 'broadcast domain',
  codst: 'shared destination',
  fleet: 'same vendor OUI',
  family: 'hostname family',
  lease: 'DHCP lockstep',
  portfp: 'port fingerprint',
  wan: 'internet egress',
}
const ROLE_ZH: Record<string, string> = {
  camera: '摄像头', intercom: '门禁对讲', mobile: '移动端', workstation: '工作站', server: '服务器', unknown: '未识别',
  'cross-segment peer': '跨段对端', 'internet-endpoint': '互联网端点',
}
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)

/* Node glyph = device class, drawn as a filled icon with interior cutouts
   (even-odd: the cuts read as paper), inheriting threat coloring through
   .sg-dot: monitor with screen + stand, phone with screen slab, dome camera
   with lens, server tower with slots + LED, meridian globe endpoint; peers
   stay a hollow ring, untyped hosts a plain dot. Scaled by node radius. */
function DotShape({ role, x, y, r }: { role: string; x: number; y: number; r: number }) {
  const s = Math.max(3.6, r)
  if (role === 'workstation') {
    return (
      <path
        className="sg-dot"
        fillRule="evenodd"
        d={`M ${x - 1.2 * s} ${y - 1.0 * s} h ${2.4 * s} v ${1.5 * s} h ${-2.4 * s} Z ` +
          `M ${x - 0.95 * s} ${y - 0.78 * s} h ${1.9 * s} v ${1.06 * s} h ${-1.9 * s} Z ` +
          `M ${x - 0.34 * s} ${y + 0.5 * s} h ${0.68 * s} v ${0.3 * s} h ${0.42 * s} v ${0.26 * s} h ${-1.52 * s} v ${-0.26 * s} h ${0.42 * s} Z`}
      />
    )
  }
  if (role === 'camera') {
    return (
      <path
        className="sg-dot"
        fillRule="evenodd"
        d={`M ${x - 0.95 * s} ${y + 0.15 * s} a ${0.95 * s} ${0.95 * s} 0 0 1 ${1.9 * s} 0 Z ` +
          `M ${x - 0.3 * s} ${y - 0.28 * s} a ${0.3 * s} ${0.3 * s} 0 1 0 ${0.6 * s} 0 a ${0.3 * s} ${0.3 * s} 0 1 0 ${-0.6 * s} 0 ` +
          `M ${x - 1.2 * s} ${y + 0.32 * s} h ${2.4 * s} v ${0.42 * s} h ${-2.4 * s} Z ` +
          `M ${x - 0.85 * s} ${y + 0.42 * s} h ${0.5 * s} v ${0.22 * s} h ${-0.5 * s} Z ` +
          `M ${x + 0.35 * s} ${y + 0.42 * s} h ${0.5 * s} v ${0.22 * s} h ${-0.5 * s} Z`}
      />
    )
  }
  if (role === 'mobile') {
    return (
      <path
        className="sg-dot"
        fillRule="evenodd"
        d={`M ${x - 0.66 * s} ${y - 0.82 * s} a ${0.24 * s} ${0.24 * s} 0 0 1 ${0.24 * s} ${-0.24 * s} h ${0.84 * s} a ${0.24 * s} ${0.24 * s} 0 0 1 ${0.24 * s} ${0.24 * s} v ${1.64 * s} a ${0.24 * s} ${0.24 * s} 0 0 1 ${-0.24 * s} ${0.24 * s} h ${-0.84 * s} a ${0.24 * s} ${0.24 * s} 0 0 1 ${-0.24 * s} ${-0.24 * s} Z ` +
          `M ${x - 0.44 * s} ${y - 0.68 * s} h ${0.88 * s} v ${1.18 * s} h ${-0.88 * s} Z ` +
          `M ${x - 0.14 * s} ${y + 0.72 * s} h ${0.28 * s} v ${0.14 * s} h ${-0.28 * s} Z`}
      />
    )
  }
  if (role === 'server') {
    return (
      <path
        className="sg-dot"
        fillRule="evenodd"
        d={`M ${x - 0.85 * s} ${y - 1.15 * s} h ${1.7 * s} v ${2.3 * s} h ${-1.7 * s} Z ` +
          `M ${x - 0.55 * s} ${y - 0.85 * s} h ${1.1 * s} v ${0.22 * s} h ${-1.1 * s} Z ` +
          `M ${x - 0.55 * s} ${y - 0.38 * s} h ${1.1 * s} v ${0.22 * s} h ${-1.1 * s} Z ` +
          `M ${x + 0.22 * s} ${y + 0.55 * s} a ${0.16 * s} ${0.16 * s} 0 1 0 ${0.32 * s} 0 a ${0.16 * s} ${0.16 * s} 0 1 0 ${-0.32 * s} 0`}
      />
    )
  }
  if (role === 'internet-endpoint') {
    return (
      <g>
        <circle cx={x} cy={y} r={1.05 * s} className="sg-dot" />
        <path
          className="sg-cut"
          d={`M ${x - 1.05 * s} ${y} h ${2.1 * s} ` +
            `M ${x - 0.9 * s} ${y - 0.52 * s} a ${1.5 * s} ${1.5 * s} 0 0 0 ${1.8 * s} 0 ` +
            `M ${x - 0.9 * s} ${y + 0.52 * s} a ${1.5 * s} ${1.5 * s} 0 0 1 ${1.8 * s} 0 ` +
            `M ${x} ${y - 1.05 * s} a ${0.5 * s} ${1.05 * s} 0 0 0 0 ${2.1 * s} a ${0.5 * s} ${1.05 * s} 0 0 0 0 ${-2.1 * s}`}
        />
      </g>
    )
  }
  return <circle cx={x} cy={y} r={r} className="sg-dot" />
}

/** Convex hull (monotone chain), pushed outward so it reads as a soft territory. */
function hull(pts: Pt[], pad: number): string {
  if (pts.length < 3) {
    if (!pts.length) return ''
    const c = pts[0]
    const r = pad + (pts.length > 1 ? Math.hypot(pts[1].x - c.x, pts[1].y - c.y) / 2 : 0)
    const m = { x: (pts[0].x + (pts[1]?.x ?? pts[0].x)) / 2, y: (pts[0].y + (pts[1]?.y ?? pts[0].y)) / 2 }
    return `M ${m.x - r} ${m.y} a ${r} ${r} 0 1 0 ${r * 2} 0 a ${r} ${r} 0 1 0 ${-r * 2} 0`
  }
  const p = [...pts].sort((a, b) => a.x - b.x || a.y - b.y)
  const cross = (o: Pt, a: Pt, b: Pt) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)
  const lower: Pt[] = []
  for (const q of p) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], q) <= 0) lower.pop()
    lower.push(q)
  }
  const upper: Pt[] = []
  for (const q of [...p].reverse()) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], q) <= 0) upper.pop()
    upper.push(q)
  }
  const h = [...lower.slice(0, -1), ...upper.slice(0, -1)]
  const cx = h.reduce((a, q) => a + q.x, 0) / h.length
  const cy = h.reduce((a, q) => a + q.y, 0) / h.length
  const out = h.map((q) => {
    const d = Math.hypot(q.x - cx, q.y - cy) || 1
    return { x: q.x + ((q.x - cx) / d) * pad, y: q.y + ((q.y - cy) / d) * pad }
  })
  // Catmull-ish smoothing through the expanded hull points
  let d = `M ${(out[0].x + out[out.length - 1].x) / 2} ${(out[0].y + out[out.length - 1].y) / 2}`
  for (let i = 0; i < out.length; i++) {
    const cur = out[i]
    const next = out[(i + 1) % out.length]
    d += ` Q ${cur.x} ${cur.y} ${(cur.x + next.x) / 2} ${(cur.y + next.y) / 2}`
  }
  return d + ' Z'
}

export function SubnetGraphLayer({
  graph,
  analysis,
  center,
  rx,
  ry,
  vbw,
  vbh,
  lang,
  hoverIp,
  selectedIp,
  focusIp,
  profile,
  marks,
  showPanel,
  onHover,
  onFocus,
  onAnalyze,
  onCloseAnalysis,
}: {
  graph: SubnetGraph
  analysis: GraphAnalysis | null
  center: Pt
  rx: number
  ry: number
  /** canvas viewBox extent — read-outs pin to the plate edges, so they must
   *  follow the canvas rather than a duplicated 1360x1000 literal. */
  vbw: number
  vbh: number
  lang: Lang
  hoverIp: string | null
  selectedIp: string | null
  /** the host promoted to an isolated ego-network (click a node). */
  focusIp: string | null
  /** traffic portrait of the focused host — drives the flow-topology overlay. */
  profile?: DeviceProfile | null
  marks: Record<string, { severity: string; verdict: string }>
  showPanel: boolean
  onHover: (ip: string | null) => void
  onFocus: (ip: string | null) => void
  onAnalyze: () => void
  onCloseAnalysis: () => void
}) {
  const zh = lang === 'zh'
  const kindLabel = zh ? KIND_ZH : KIND_EN
  const radius = Math.min(rx, ry)

  const pos = useMemo(() => {
    const m: Record<string, Pt> = {}
    for (const d of graph.devices) m[d.ip] = { x: center.x + d.x * rx, y: center.y + d.y * ry }
    return m
  }, [graph, center.x, center.y, rx, ry])

  const dev = useMemo(() => Object.fromEntries(graph.devices.map((d) => [d.ip, d])), [graph])
  const labelOf = useMemo(() => {
    const m: Record<string, string> = {}
    for (const c of analysis?.communities ?? []) m[c.id] = c.label
    return m
  }, [analysis])

  // A device is "flagged" if the agent named it in a pattern; severity comes from the pattern.
  const flagged = useMemo(() => {
    const m: Record<string, { sev: string; title: string }> = {}
    for (const p of analysis?.patterns ?? []) {
      for (const ip of p.members) {
        const cur = m[ip]
        if (!cur || (p.severity === 'high' && cur.sev !== 'high')) m[ip] = { sev: p.severity, title: p.title }
      }
    }
    return m
  }, [analysis])

  const anomalyIps = useMemo(() => new Set(graph.anomalies.flatMap((a) => a.members)), [graph])

  /* Legend content is derived from the edges this segment actually has, not from
     a fixed list of the seven kinds the miner can emit — 192.168.1.0/24 carries
     only five of them, and naming absent evidence classes invites the reader to
     look for marks that are not on the map. `observed` is the payload's own flag
     (build_device_graph.py: clash/bcast/codst are measured; the rest inferred). */
  const kindsPresent = useMemo(() => {
    const obs = new Set<string>()
    const inf = new Set<string>()
    for (const e of graph.edges) (e.observed ? obs : inf).add(e.kind)
    return { obs: [...obs].sort(), inf: [...inf].sort() }
  }, [graph])
  const degree = useMemo(() => {
    const d: Record<string, number> = {}
    for (const e of graph.edges) {
      d[e.src] = (d[e.src] ?? 0) + 1
      d[e.dst] = (d[e.dst] ?? 0) + 1
    }
    return d
  }, [graph])

  const size = (d: GraphDevice) => {
    const mass = Math.log10(d.deny + d.flows + 1)
    return Math.max(3.2, Math.min(11, 3.2 + mass * 1.9 + (degree[d.ip] ?? 0) * 0.22))
  }


  const hovered = hoverIp ? dev[hoverIp] : null
  const neighbours = useMemo(() => {
    if (!hoverIp) return null
    const s = new Set<string>()
    for (const e of graph.edges) {
      if (e.src === hoverIp) s.add(e.dst)
      if (e.dst === hoverIp) s.add(e.src)
    }
    return s
  }, [hoverIp, graph])

  // ── ego-network of the focused host: itself, its neighbours, and every edge
  //    that touches it (each carrying the evidence for why the two are linked). */
  const ego = useMemo(() => {
    if (!focusIp) return null
    const edges = graph.edges.filter((e) => e.src === focusIp || e.dst === focusIp)
    const members = new Set<string>([focusIp])
    const rel: { ip: string; kind: string; evidence: string; observed: boolean; weight: number }[] = []
    for (const e of edges) {
      const other = e.src === focusIp ? e.dst : e.src
      members.add(other)
      rel.push({ ip: other, kind: e.kind, evidence: e.evidence, observed: e.observed, weight: e.weight })
    }
    rel.sort((a, b) => b.weight - a.weight)
    return { members, edges, rel }
  }, [focusIp, graph])

  const st = graph.stats

  return (
    <g className={`sg ${ego ? 'is-ego' : ''}`}>
      {/* ── community territories ──
          Device-class bands (id `class-*`) read as console panels: hull around
          the aligned grid, header top-left with the counts that rank the band
          (risk hits · active · denied). Other clusters keep the centred label. */}
      {graph.clusters.map((c) => {
        const pts = c.members.map((m) => pos[m]).filter(Boolean)
        const isBand = c.id.startsWith('class-') || c.id === 'boundary-peers' || c.id === 'internet-endpoints'
        // captions work from a single member; only hulls need three points
        if (pts.length < (isBand ? 1 : 2)) return null
        const cx = pts.reduce((a, p) => a + p.x, 0) / pts.length
        const cy = pts.reduce((a, p) => a + p.y, 0) / pts.length
        const label = c.id === 'boundary-peers'
          ? `${zh ? '边界对端' : 'BOUNDARY PEERS'} ×${c.size}`
          : c.id === 'internet-endpoints'
            ? `${zh ? '互联网去向' : 'INTERNET EGRESS'} ×${c.size}`
            : labelOf[c.id] ?? `${c.vendor || (zh ? ROLE_ZH[c.role] ?? c.role : c.role)} ×${c.size}`
        const egoHome = ego ? c.members.some((m) => ego.members.has(m)) : false
        const cls = `sg-cluster ${c.deny > 20000 ? 'hot' : ''} ${ego ? (egoHome ? 'ego-in' : 'ego-off') : ''}`
        if (isBand) {
          /* Production groups draw NO container at all — the force layout makes
             membership legible as proximity, so ink is spent only on a floating
             caption above the group: the class name on an acid highlight bar
             (the console's editorial register), count as a bare numeral, then
             the counts that rank it. An open ego net owns the field. */
          if (ego) return null
          const topY = Math.min(...pts.map((p) => p.y))
          const active = c.members.filter((m) => (dev[m]?.flows ?? 0) > 0).length
          const flagged = c.members.filter((m) => dev[m]?.threat !== 'ok').length
          const parts = [
            `${active} ${zh ? '活跃' : 'active'}`,
            ...(flagged ? [`${flagged} ${zh ? '风险' : 'flagged'}`] : []),
            ...(c.deny > 0 ? [`${short(c.deny)} ${zh ? '拦截' : 'denied'}`] : []),
          ]
          const name = c.id === 'boundary-peers'
            ? (zh ? '边界对端' : 'BOUNDARY PEERS')
            : c.id === 'internet-endpoints'
              ? (zh ? '互联网去向' : 'INTERNET EGRESS')
              : (zh ? ROLE_ZH[c.role] ?? c.role : c.role)
          return (
            <foreignObject key={c.id} x={cx - 150} y={topY - 66} width={300} height={56} className="sg-cap-fo" pointerEvents="none">
              <div className="sg-cap-wrap">
                <div className="sg-cap"><span className="sg-cap-t">{name}</span><span className="sg-cap-n">×{c.size}</span></div>
                <div className={`sg-cap-s ${flagged ? 'risk' : ''}`}>{parts.join(' · ')}</div>
              </div>
            </foreignObject>
          )
        }
        return (
          <g key={c.id} className={cls} pointerEvents="none">
            <path d={hull(pts, 22)} className="sg-hull" />
            <text x={cx} y={cy - Math.max(30, radius * 0.06)} className="sg-hull-label" textAnchor="middle">
              {label}
            </text>
          </g>
        )
      })}

      {/* ── capillaries ──
          The resting map is a clustered census, not a wiring diagram: no edge
          draws until the reader asks — hover lights a host's relations, click
          promotes them to the ego net. Evidence text lives in the ego panel,
          not on the lines (forty on-line labels was the mess this replaces). */}
      <g className="sg-edges" pointerEvents="none">
        {graph.edges.map((e, i) => {
          const a = pos[e.src]
          const b = pos[e.dst]
          if (!a || !b) return null
          const touchesFocus = !!focusIp && (e.src === focusIp || e.dst === focusIp)
          const touchesHover = !!hoverIp && (e.src === hoverIp || e.dst === hoverIp)
          if (ego ? !touchesFocus : !touchesHover) return null
          const w = Math.max(0.5, Math.min(2.6, e.weight * 0.7)) * (touchesFocus ? 1.8 : 1)
          const mx = (a.x + b.x) / 2 + (b.y - a.y) * 0.09
          const my = (a.y + b.y) / 2 - (b.x - a.x) * 0.09
          const d = `M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}`
          return (
            <g key={i}>
              <path d={d} className={`sg-edge k-${e.kind} ${e.observed ? 'obs' : 'inf'} ${touchesFocus ? 'ego-edge' : ''}`} style={{ strokeWidth: w }} />
              {e.observed ? (
                <circle r={touchesFocus ? 2.4 : 1.7} className={`sg-drip k-${e.kind} ${touchesFocus ? 'ego-edge' : ''}`}>
                  <animateMotion dur={`${Math.max(1.6, 5 - e.weight)}s`} repeatCount="indefinite" path={d} />
                </circle>
              ) : null}
            </g>
          )
        })}
      </g>

      {/* ── agent-found pivot corridors ── */}
      {(analysis?.corridors ?? []).map((c, i) => {
        const a = pos[c.src]
        const b = pos[c.dst]
        if (!a || !b) return null
        const mx = (a.x + b.x) / 2 + (b.y - a.y) * 0.16
        const my = (a.y + b.y) / 2 - (b.x - a.x) * 0.16
        const d = `M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}`
        return (
          <g key={`cor${i}`} pointerEvents="none">
            <path d={d} className="sg-corridor" />
            <circle r={2.6} className="sg-corridor-drip">
              <animateMotion dur="2.4s" repeatCount="indefinite" path={d} />
            </circle>
          </g>
        )
      })}

      {/* ── hosts ── */}
      <g className="sg-nodes">
        {graph.devices.map((d) => {
          const p = pos[d.ip]
          if (!p) return null
          const isFocus = focusIp === d.ip
          const inEgo = ego?.members.has(d.ip) ?? false
          const r = size(d) * (isFocus ? 1.55 : 1)
          const dim = !ego && !!hoverIp && hoverIp !== d.ip && !neighbours?.has(d.ip)
          const egoCls = ego ? (isFocus ? 'ego-focus' : inEgo ? 'ego-in' : 'ego-off') : ''
          const mark = marks[d.ip]
          const fl = flagged[d.ip]
          const sev = fl?.sev ?? (mark?.severity === 'high' ? 'high' : mark?.severity === 'medium' ? 'medium' : '')
          /* Only HIGH-severity hosts carry a standing label — a full row of
             watch-level octets is exactly the kind of ink the grid removes;
             watch state still reads from the dashed ring, and hover names it. */
          const labelled = d.threat === 'high' || selectedIp === d.ip || hoverIp === d.ip || (ego ? inEgo : false)
          return (
            <g
              key={d.ip}
              className={`sg-node t-${d.threat} ${d.seenBy} r-${d.role.replace(/\s+/g, '-')} ${dim ? 'dim' : ''} ${selectedIp === d.ip ? 'sel' : ''} ${egoCls}`}
              onMouseEnter={() => onHover(d.ip)}
              onMouseLeave={() => onHover(null)}
              onClick={(e) => {
                e.stopPropagation()
                onFocus(isFocus ? null : d.ip)
              }}
              style={{ cursor: 'pointer' }}
            >
              {isFocus ? <circle cx={p.x} cy={p.y} r={r + 9} className="sg-focus-ring" /> : null}
              {sev ? <circle cx={p.x} cy={p.y} r={r + 6} className={`sg-flag sev-${sev}`} /> : null}
              {anomalyIps.has(d.ip) ? <circle cx={p.x} cy={p.y} r={r + 3.5} className="sg-anom" /> : null}
              <DotShape role={d.role} x={p.x} y={p.y} r={r} />
              {labelled ? (
                <text x={p.x + r + 5} y={p.y + 3} className="sg-ip">{isFocus ? d.ip : d.ip.split('.').slice(-1)[0]}</text>
              ) : null}
              <circle cx={p.x} cy={p.y} r={Math.max(r + 6, 9)} fill="transparent" />
            </g>
          )
        })}
      </g>

      {/* ── flow topology of the focused host: where its traffic actually GOES and
          what comes AT it (per-pair aggregates mined from the raw syslog), drawn on
          top of the relation ego-net. Internal peers use their real node positions;
          external endpoints get anchor stubs fanned beside the focus — the traffic
          log carries no hostnames, so ip + service + best-effort rDNS is exactly
          what the evidence supports, and nothing more is claimed. ── */}
      {ego && focusIp && profile && !profile.loading && pos[focusIp] ? (() => {
        const pf = pos[focusIp]
        const clampY = (y: number) => Math.max(40, Math.min(vbh - 40, y))
        const intOut = profile.outbound.filter((r) => r.kind === 'host' && !r.external && pos[r.ip])
        const intIn = profile.inbound.filter((r) => r.kind === 'host' && !r.external && pos[r.ip])
        /* Lines draw for every peer; per-line captions only for the loudest —
           a hub with forty peers is a star, not forty sentences. The full list
           lives in the ego panel. */
        const labelled = new Set(
          [...intOut, ...intIn].sort((a, b) => b.hits - a.hits).slice(0, 8).map((r) => r.ip),
        )
        const extOut = profile.outbound.filter((r) => r.external).slice(0, 6)
        const extIn = profile.inbound.filter((r) => r.external).slice(0, 4)
        const bcast = profile.outbound.filter((r) => r.kind === 'bcast')
        const svc = (r: ProfilePeer) => r.services[0] ?? (r.ports.length ? `:${r.ports[0]}` : '')
        // live throughput per destination IP, from the router's session sampler.
        const liveByIp: Record<string, { bps?: number }> = {}
        for (const d of profile.live?.destinations ?? []) liveByIp[d.ip] = { bps: d.bps }
        const fmtBw = (bps?: number | null) => {
          if (!bps || bps <= 0) return ''
          if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)}M`
          if (bps >= 1e3) return `${(bps / 1e3).toFixed(1)}k`
          return `${bps}`
        }
        const bwTag = (ip: string) => {
          const cur = fmtBw(liveByIp[ip]?.bps)
          return cur ? ` · ${cur}bps` : ''
        }
        const arc = (a: Pt, b: Pt, sign: number) =>
          `M ${a.x} ${a.y} Q ${(a.x + b.x) / 2 - (b.y - a.y) * 0.18 * sign} ${(a.y + b.y) / 2 + (b.x - a.x) * 0.18 * sign} ${b.x} ${b.y}`
        const flow = (a: Pt, b: Pt, r: ProfilePeer, key: string) => {
          const d = arc(a, b, r.dir === 'in' ? -1 : 1)
          const denied = r.deny > r.accept
          return (
            <g key={key} className="fp-flow">
              <path d={d} className={`fp-line ${r.dir} ${denied ? 'denied' : ''}`} />
              <circle r={2.2} className={`fp-drip ${denied ? 'denied' : ''}`}>
                <animateMotion dur={`${Math.max(1.4, 4.5 - Math.log10(r.hits + 1))}s`} repeatCount="indefinite" path={d} />
              </circle>
            </g>
          )
        }
        return (
          <g className="fp-layer" pointerEvents="none">
            {intOut.map((r) => flow(pf, pos[r.ip], r, `io${r.ip}`))}
            {intIn.map((r) => flow(pos[r.ip], pf, r, `ii${r.ip}`))}
            {intOut.filter((r) => labelled.has(r.ip)).map((r) => (
              <text key={`iol${r.ip}`} x={pos[r.ip].x} y={pos[r.ip].y - 12} className="fp-lbl" textAnchor="middle">
                ▸ {svc(r)} · {short(r.hits)}
              </text>
            ))}
            {intIn.filter((r) => labelled.has(r.ip)).map((r) => (
              <text key={`iil${r.ip}`} x={pos[r.ip].x} y={pos[r.ip].y + 20} className="fp-lbl in" textAnchor="middle">
                ◂ {svc(r)} · {short(r.hits)}
              </text>
            ))}

            {/* external destinations — fanned right of the focus */}
            {extOut.map((r, i) => {
              const p: Pt = { x: pf.x + 265, y: clampY(pf.y - ((extOut.length - 1) * 56) / 2 + i * 56) }
              const denied = r.deny > r.accept
              return (
                <g key={`eo${r.ip}`} className="fp-ext">
                  {flow(pf, p, r, `eof${r.ip}`)}
                  <rect x={p.x - 4.5} y={p.y - 4.5} width="9" height="9" className={`fp-anchor ${denied ? 'denied' : ''}`} transform={`rotate(45 ${p.x} ${p.y})`} />
                  <text x={p.x + 11} y={p.y - 1} className="fp-ext-name" textAnchor="start">
                    {r.rdns ?? r.ip}
                    {bwTag(r.ip) ? <tspan className="fp-bw">{bwTag(r.ip)}</tspan> : null}
                  </text>
                  <text x={p.x + 11} y={p.y + 11} className="fp-ext-sub" textAnchor="start">
                    {r.rdns ? `${r.ip} · ` : ''}{svc(r)}{r.country ? ` · ${r.country}` : ''} · {short(r.hits)}
                    {denied ? (lang === 'zh' ? ' · 被拦截' : ' · denied') : ''}
                  </text>
                </g>
              )
            })}
            {extOut.length ? (
              <text x={pf.x + 265} y={clampY(pf.y - ((extOut.length - 1) * 56) / 2 - 26)} className="fp-col" textAnchor="start">
                {lang === 'zh' ? '外联去向' : 'WAN DESTINATIONS'}
              </text>
            ) : null}

            {/* external sources hitting this host — fanned left */}
            {extIn.map((r, i) => {
              const p: Pt = { x: pf.x - 265, y: clampY(pf.y - ((extIn.length - 1) * 56) / 2 + i * 56) }
              const denied = r.deny > r.accept
              return (
                <g key={`ei${r.ip}`} className="fp-ext">
                  {flow(p, pf, r, `eif${r.ip}`)}
                  <rect x={p.x - 4.5} y={p.y - 4.5} width="9" height="9" className={`fp-anchor in ${denied ? 'denied' : ''}`} transform={`rotate(45 ${p.x} ${p.y})`} />
                  <text x={p.x - 11} y={p.y - 1} className="fp-ext-name" textAnchor="end">{r.rdns ?? r.ip}</text>
                  <text x={p.x - 11} y={p.y + 11} className="fp-ext-sub" textAnchor="end">
                    {r.rdns ? `${r.ip} · ` : ''}{svc(r)}{r.country ? ` · ${r.country}` : ''} · {short(r.hits)}
                    {denied ? (lang === 'zh' ? ' · 被拦截' : ' · denied') : ''}
                  </text>
                </g>
              )
            })}
            {extIn.length ? (
              <text x={pf.x - 265} y={clampY(pf.y - ((extIn.length - 1) * 56) / 2 - 26)} className="fp-col" textAnchor="end">
                {lang === 'zh' ? '入向来源' : 'INBOUND SOURCES'}
              </text>
            ) : null}

            {/* broadcast/multicast chatter — one collective stub above the focus */}
            {bcast.length ? (() => {
              const p: Pt = { x: pf.x, y: clampY(pf.y - 150) }
              const hits = bcast.reduce((a, r) => a + r.hits, 0)
              return (
                <g className="fp-ext">
                  <path d={arc(pf, p, 1)} className="fp-line out bcast" />
                  <circle cx={p.x} cy={p.y} r="5" className="fp-bcast-dot" />
                  <text x={p.x} y={p.y - 14} className="fp-ext-name" textAnchor="middle">
                    ⌁ {lang === 'zh' ? '广播 / 组播域' : 'broadcast / multicast'}
                  </text>
                  <text x={p.x + 10} y={p.y + 2} className="fp-ext-sub" textAnchor="start">
                    {short(hits)} {lang === 'zh' ? '条' : 'msgs'} · {bcast.slice(0, 2).map(svc).join(' / ')}
                  </text>
                </g>
              )
            })() : null}
          </g>
        )
      })() : null}

      {/* ── hover read-out (suppressed while an ego net owns the field) ── */}
      {hovered && !ego ? (
        (() => {
          const p = pos[hovered.ip]
          const links = graph.edges.filter((e) => e.src === hovered.ip || e.dst === hovered.ip)
          const kinds = [...new Set(links.map((l) => l.kind))]
          const w = 250
          const x = Math.min(Math.max(p.x + 18, 8), vbw - w - 8)
          const y = Math.min(Math.max(p.y - 40, 6), vbh - 130)
          return (
            <foreignObject x={x} y={y} width={w} height={132} className="sg-tip-fo" pointerEvents="none">
              <div className={`sg-tip t-${hovered.threat}`}>
                <div className="sg-tip-h">
                  <b>{hovered.ip}</b>
                  <span>{hovered.name ?? (zh ? '无主机名' : 'no hostname')}</span>
                </div>
                <div className="sg-tip-r">
                  <i>{zh ? '身份' : 'ID'}</i>
                  {(zh ? ROLE_ZH[hovered.role] ?? hovered.role : hovered.role)}
                  {hovered.vendor !== 'unknown' ? ` · ${hovered.vendor}` : ''}
                  {hovered.os ? ` · ${hovered.os}` : ''}
                </div>
                <div className="sg-tip-r">
                  <i>{zh ? '流量' : 'TRAFFIC'}</i>
                  {hovered.seenBy === 'dhcp'
                    ? (zh ? `仅 DHCP 可见 · ${hovered.leases} 次续约` : `DHCP-only · ${hovered.leases} leases`)
                    : `${short(hovered.deny)} ${zh ? '拦截' : 'blocked'} · ${short(hovered.accept)} ${zh ? '放行' : 'allowed'}`}
                </div>
                <div className="sg-tip-r">
                  <i>{zh ? '关联' : 'LINKS'}</i>
                  {links.length} · {kinds.map((k) => kindLabel[k]).join(' / ') || (zh ? '孤立' : 'isolated')}
                </div>
              </div>
            </foreignObject>
          )
        })()
      ) : null}

      {/* ── segment read-out + agent trigger (yields to the ego panel) ── */}
      {!ego ? (
      <foreignObject x={center.x - rx + 4} y={Math.max(6, center.y - ry - 74)} width={430} height={92} className="sg-tip-fo">
        <div className="sg-head">
          <div className="sg-head-t">{graph.cidr}</div>
          <div className="sg-head-s">
            <b>{st.devices}</b> {zh ? '台设备' : 'devices'} · <b>{st.withTraffic}</b> {zh ? '有流量' : 'routing'} ·{' '}
            <b>{st.dhcpOnly}</b> {zh ? '静默(仅DHCP)' : 'silent (DHCP only)'}
          </div>
          <div className="sg-head-s">
            <b>{st.edges}</b> {zh ? '条关联' : 'relations'} · {st.observedEdges} {zh ? '实测' : 'observed'} ·{' '}
            {st.edges - st.observedEdges} {zh ? '推断' : 'inferred'} · <b>{graph.clusters.length}</b> {zh ? '社区' : 'communities'}
          </div>
          {!analysis && graph.cidr !== 'mem://autopoiesis' ? (
            <button className="sg-cta" onClick={onAnalyze}>
              ⚡ {zh ? 'Agent 关联分析' : 'AGENT · CORRELATE'}
            </button>
          ) : null}
        </div>
      </foreignObject>
      ) : null}

      {/* ── agent findings (yields the left column to a per-device analysis) ── */}
      {analysis && showPanel && !ego ? (
        <foreignObject x={20} y={620} width={640} height={372} className="sg-tip-fo">
          <div className="sg-panel">
            <div className="sg-panel-h">
              <span>{zh ? 'AGENT · 网段关联模型' : 'AGENT · SEGMENT MODEL'} · {analysis.cidr}</span>
              <button onClick={onCloseAnalysis}>✕</button>
            </div>
            {analysis.loading ? (
              <div className="sg-panel-b sg-wait">{zh ? '正在关联全网设备…' : 'correlating every host…'}</div>
            ) : analysis.error ? (
              <div className="sg-panel-b sg-err">{analysis.error}</div>
            ) : (
              <div className="sg-panel-b">
                <p className="sg-sum">{analysis.summary}</p>
                <ul className="sg-pats">
                  {(analysis.patterns ?? []).map((p, i) => (
                    <li key={i} className={`sev-${p.severity}`}>
                      <div className="sg-pat-h">
                        <span className="sg-pat-t">{p.title}</span>
                        <span className="sg-pat-k">{p.kind}</span>
                        {typeof p.confidence === 'number' ? <span className="sg-pat-c">{Math.round(p.confidence * 100)}%</span> : null}
                      </div>
                      <div className="sg-pat-w">{p.why}</div>
                      <div className="sg-pat-m">{p.members.slice(0, 8).join(' · ')}{p.members.length > 8 ? ` +${p.members.length - 8}` : ''}</div>
                    </li>
                  ))}
                </ul>
                <div className="sg-foot">
                  <div><i>{zh ? '流量走向' : 'FLOW'}</i>{analysis.flow}</div>
                  <div><i className="bad">{zh ? '盲区' : 'BLIND SPOT'}</i>{analysis.blindSpot}</div>
                </div>
              </div>
            )}
          </div>
        </foreignObject>
      ) : null}

      {/* ── legend ──
          Grouped by the distinction the map is entitled to assert: a relation was
          either MEASURED in the syslog or INFERRED from a shared attribute. That
          is the line the graph must not blur, and it is the one the eye can read
          (solid vs dashed). The kinds are listed as words under each class rather
          than each getting its own hue. */}
      {!ego ? (
      <foreignObject x={20} y={vbh - 238} width={218} height={220} className="sg-tip-fo">
        <div className="sg-legend">
          {(() => {
            const roles = new Set(graph.devices.map((d) => d.role))
            const rows: [string, string][] = []
            if (roles.has('workstation')) rows.push(['▭', zh ? '工作站' : 'workstation'])
            if (roles.has('mobile')) rows.push(['▯', zh ? '移动端' : 'mobile'])
            if (roles.has('camera')) rows.push(['◠', zh ? '摄像头' : 'camera'])
            if (roles.has('server')) rows.push(['≡', zh ? '服务器' : 'server'])
            if (roles.has('internet-endpoint')) rows.push(['⊕', zh ? '互联网端点' : 'internet endpoint'])
            if (roles.has('unknown')) rows.push(['●', zh ? '未识别' : 'untyped'])
            if (roles.has('cross-segment peer')) rows.push(['○', zh ? '跨段对端' : 'cross-segment peer'])
            if (rows.length < 2) return null
            return (
              <>
                <div className="sg-lg-t">{zh ? '节点形状' : 'NODE GLYPH'}</div>
                {rows.map(([glyph, name]) => (
                  <div key={glyph} className="sg-lg-r"><span className="sg-lg-glyph">{glyph}</span>{name}</div>
                ))}
              </>
            )
          })()}
          <div className="sg-lg-t">{zh ? '关联证据' : 'RELATION EVIDENCE'}</div>
          {kindsPresent.obs.length ? (
            <>
              <div className="sg-lg-r">
                <span className="sg-lg-line obs" />
                {zh ? '实测' : 'observed'}
              </div>
              <div className="sg-lg-k">{kindsPresent.obs.map((k) => kindLabel[k] ?? k).join(' · ')}</div>
            </>
          ) : null}
          {kindsPresent.inf.length ? (
            <>
              <div className="sg-lg-r">
                <span className="sg-lg-line inf" />
                {zh ? '推断' : 'inferred'}
              </div>
              <div className="sg-lg-k">{kindsPresent.inf.map((k) => kindLabel[k] ?? k).join(' · ')}</div>
            </>
          ) : null}
        </div>
      </foreignObject>
      ) : null}
    </g>
  )
}
