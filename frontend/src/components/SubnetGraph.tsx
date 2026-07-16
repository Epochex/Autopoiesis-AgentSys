import { useMemo } from 'react'
import type { GraphAnalysis, GraphDevice, SubnetGraph } from '../types'
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
}
const KIND_EN: Record<string, string> = {
  clash: 'session clash / dup IP',
  bcast: 'broadcast domain',
  codst: 'shared destination',
  fleet: 'same vendor OUI',
  family: 'hostname family',
  lease: 'DHCP lockstep',
  portfp: 'port fingerprint',
}
const ROLE_ZH: Record<string, string> = {
  camera: '摄像头', intercom: '门禁对讲', mobile: '移动端', workstation: '工作站', server: '服务器', unknown: '未识别',
}
const short = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`)

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
  lang,
  hoverIp,
  selectedIp,
  marks,
  showPanel,
  onHover,
  onPick,
  onAnalyze,
  onCloseAnalysis,
}: {
  graph: SubnetGraph
  analysis: GraphAnalysis | null
  center: Pt
  rx: number
  ry: number
  lang: Lang
  hoverIp: string | null
  selectedIp: string | null
  marks: Record<string, { severity: string; verdict: string }>
  showPanel: boolean
  onHover: (ip: string | null) => void
  onPick: (dev: GraphDevice) => void
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

  const st = graph.stats

  return (
    <g className="sg">
      {/* ── community territories ── */}
      {graph.clusters.map((c) => {
        const pts = c.members.map((m) => pos[m]).filter(Boolean)
        if (pts.length < 2) return null
        const cx = pts.reduce((a, p) => a + p.x, 0) / pts.length
        const cy = pts.reduce((a, p) => a + p.y, 0) / pts.length
        const label = labelOf[c.id] ?? `${c.vendor || (zh ? ROLE_ZH[c.role] ?? c.role : c.role)} ×${c.size}`
        return (
          <g key={c.id} className={`sg-cluster ${c.deny > 20000 ? 'hot' : ''}`} pointerEvents="none">
            <path d={hull(pts, 22)} className="sg-hull" />
            <text x={cx} y={cy - Math.max(30, radius * 0.06)} className="sg-hull-label" textAnchor="middle">
              {label}
            </text>
          </g>
        )
      })}

      {/* ── capillaries: inferred relations sit behind observed flows ── */}
      <g className="sg-edges" pointerEvents="none">
        {graph.edges.map((e, i) => {
          const a = pos[e.src]
          const b = pos[e.dst]
          if (!a || !b) return null
          const dim = !!hoverIp && !(e.src === hoverIp || e.dst === hoverIp)
          const w = Math.max(0.5, Math.min(2.6, e.weight * 0.7))
          const mx = (a.x + b.x) / 2 + (b.y - a.y) * 0.09
          const my = (a.y + b.y) / 2 - (b.x - a.x) * 0.09
          const d = `M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}`
          return (
            <g key={i}>
              <path d={d} className={`sg-edge k-${e.kind} ${e.observed ? 'obs' : 'inf'} ${dim ? 'dim' : ''}`} style={{ strokeWidth: w }} />
              {e.observed && !dim ? (
                <circle r={1.7} className={`sg-drip k-${e.kind}`}>
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
          const r = size(d)
          const dim = !!hoverIp && hoverIp !== d.ip && !neighbours?.has(d.ip)
          const mark = marks[d.ip]
          const fl = flagged[d.ip]
          const sev = fl?.sev ?? (mark?.severity === 'high' ? 'high' : mark?.severity === 'medium' ? 'medium' : '')
          return (
            <g
              key={d.ip}
              className={`sg-node t-${d.threat} ${d.seenBy} ${dim ? 'dim' : ''} ${selectedIp === d.ip ? 'sel' : ''}`}
              onMouseEnter={() => onHover(d.ip)}
              onMouseLeave={() => onHover(null)}
              onClick={(e) => {
                e.stopPropagation()
                onPick(d)
              }}
              style={{ cursor: 'pointer' }}
            >
              {sev ? <circle cx={p.x} cy={p.y} r={r + 6} className={`sg-flag sev-${sev}`} /> : null}
              {anomalyIps.has(d.ip) ? <circle cx={p.x} cy={p.y} r={r + 3.5} className="sg-anom" /> : null}
              <circle cx={p.x} cy={p.y} r={r} className="sg-dot" />
              {d.threat !== 'ok' || selectedIp === d.ip || hoverIp === d.ip ? (
                <text x={p.x + r + 5} y={p.y + 3} className="sg-ip">{d.ip.split('.').slice(-1)[0]}</text>
              ) : null}
              <circle cx={p.x} cy={p.y} r={Math.max(r + 6, 9)} fill="transparent" />
            </g>
          )
        })}
      </g>

      {/* ── hover read-out ── */}
      {hovered ? (
        (() => {
          const p = pos[hovered.ip]
          const links = graph.edges.filter((e) => e.src === hovered.ip || e.dst === hovered.ip)
          const kinds = [...new Set(links.map((l) => l.kind))]
          const w = 250
          const x = Math.min(Math.max(p.x + 18, 8), 1360 - w - 8)
          const y = Math.min(Math.max(p.y - 40, 6), 1000 - 130)
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

      {/* ── segment read-out + agent trigger ── */}
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
          {!analysis ? (
            <button className="sg-cta" onClick={onAnalyze}>
              ⚡ {zh ? 'Agent 关联分析' : 'AGENT · CORRELATE'}
            </button>
          ) : null}
        </div>
      </foreignObject>

      {/* ── agent findings (yields the left column to a per-device analysis) ── */}
      {analysis && showPanel ? (
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

      {/* ── legend ── */}
      <foreignObject x={1360 - 218} y={1000 - 132} width={206} height={124} className="sg-tip-fo">
        <div className="sg-legend">
          <div className="sg-lg-t">{zh ? '关联证据' : 'RELATION EVIDENCE'}</div>
          {(['clash', 'bcast', 'codst', 'lease', 'fleet', 'family'] as const).map((k) => (
            <div key={k} className="sg-lg-r">
              <span className={`sg-lg-line k-${k} ${k === 'clash' || k === 'bcast' || k === 'codst' ? 'obs' : 'inf'}`} />
              {kindLabel[k]}
            </div>
          ))}
        </div>
      </foreignObject>
    </g>
  )
}
