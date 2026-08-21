/* Attack surface as the relation graph it actually is.
 *
 * The surface used to render as a wall of asset cards: 48 boxes of ip + ports,
 * sorted by score. That layout carries one fact per box and no relation between
 * any two of them, so the reader gets a pile to scroll rather than a picture to
 * read — and the interesting part of an attack surface is precisely which hosts
 * are tied to which.
 *
 * The ties are already mined and already evidence-typed by build_device_graph:
 * duplicate-IP/session clashes, shared broadcast domains, shared destinations,
 * vendor fleets, hostname families. Solid edges are hard evidence, dashed are
 * inferred, and the distinction stays visible instead of being averaged away.
 *
 * Clicking a node drives the same focus state as the address lattice, so the
 * graph, the lattice and the findings ledger are three views of one selection.
 */
import { useMemo, useState } from 'react'
import type { EnvSegment, GraphEdge, SubnetGraph } from '../types'
import type { SurfaceHost } from './scan-data'

type Props = {
  graph: SubnetGraph | null
  surface: SurfaceHost[]
  segments: EnvSegment[]
  zh: boolean
  selected: string | null
  onSelect: (ip: string) => void
}

type Node = {
  ip: string
  x: number
  y: number
  r: number
  name: string | null
  vendor: string
  role: string
  ports: string[]
  risk: number
  exposure: string | null
  env: 'contested' | 'unbound' | 'leased' | 'silent'
  flows: number
  deny: number
  loose: boolean
}

const EDGE_LABEL: Record<string, [string, string]> = {
  clash: ['地址/会话冲突', 'ADDRESS / SESSION CLASH'],
  bcast: ['同广播域', 'SAME BROADCAST DOMAIN'],
  codst: ['同目的地', 'SAME DESTINATION'],
  fleet: ['同厂商批次', 'SAME VENDOR BLOCK'],
  family: ['同主机名族', 'SAME HOSTNAME FAMILY'],
  lease: ['租约同步', 'LEASES IN LOCKSTEP'],
  portfp: ['端口指纹一致', 'IDENTICAL PORT FINGERPRINT'],
}
// Hard evidence first: these are the kinds a reader should scan for.
const EDGE_ORDER = ['clash', 'bcast', 'codst', 'fleet', 'family', 'lease', 'portfp']

const W = 1200
const PAD = 46
const FIELD_H = 340
const BAND_ROW = 46
/* The canvas is sized to its content: a fixed height would leave a dead strip
   under the parked band whenever the unrelated hosts fit in fewer rows. */
const bandHeight = (count: number, perRow: number) =>
  count ? 30 + Math.ceil(count / perRow) * BAND_ROW : 0

export function AttackSurfaceGraph({ graph, surface, segments, zh, selected, onSelect }: Props) {
  const [hover, setHover] = useState<Node | null>(null)
  const [muted, setMuted] = useState<Set<string>>(new Set())

  const { nodes, edges, kinds, looseCount, height, bandTop } = useMemo(() => {
    const risk = new Map(surface.map((host) => [host.ip, host]))
    const envState = new Map<string, Node['env']>()
    for (const segment of segments) {
      for (const cell of segment.cells) envState.set(cell.ip, cell.state)
    }

    const devices = graph?.devices ?? []
    const drawable = new Set(devices.map((device) => device.ip))
    const degree = new Map<string, number>()
    for (const edge of graph?.edges ?? []) {
      if (!drawable.has(edge.src) || !drawable.has(edge.dst)) continue
      degree.set(edge.src, (degree.get(edge.src) ?? 0) + 1)
      degree.set(edge.dst, (degree.get(edge.dst) ?? 0) + 1)
    }
    // Hosts with no mined relation carry no position information, and scattering
    // them across the canvas stretches the extent so the part that DOES have
    // structure collapses into an unreadable knot. They get a compact band of
    // their own, which is also the more honest statement: nothing was found
    // tying them to anything.
    const linked = devices.filter((device) => (degree.get(device.ip) ?? 0) > 0)
    const loose = devices.filter((device) => (degree.get(device.ip) ?? 0) === 0)

    // Fit to the DATA extent of the related hosts, not to the nominal [-1,1]
    // domain: the mined positions occupy a fraction of it, and pinning the
    // domain leaves most of the canvas empty while the structure collapses.
    //
    // Packing by connected component was tried and reverted: 48 of 52 hosts sit
    // in ONE component here (broadcast + clash ties), so packing had nothing to
    // pack and only flattened the cluster into a line.
    const fitted = linked.length ? linked : devices
    const xs = fitted.map((d) => d.x)
    const ys = fitted.map((d) => d.y)
    const minX = Math.min(...xs)
    const maxX = Math.max(...xs)
    const minY = Math.min(...ys)
    const maxY = Math.max(...ys)
    const perRow = Math.max(1, Math.floor((W - PAD * 2) / 58))
    const BAND = bandHeight(loose.length, perRow)
    const H = PAD + FIELD_H + BAND + 16
    const fieldH = FIELD_H
    const place = (device: { ip: string; x: number; y: number }) => ({
      x: PAD + ((device.x - minX) / Math.max(1e-6, maxX - minX)) * (W - PAD * 2),
      y: PAD + ((device.y - minY) / Math.max(1e-6, maxY - minY)) * fieldH,
    })

    const looseAt = new Map<string, { x: number; y: number }>()
    loose.forEach((device, index) => {
      looseAt.set(device.ip, {
        x: PAD + 24 + (index % perRow) * 58,
        y: H - BAND + 30 + Math.floor(index / perRow) * 46,
      })
    })

    const built: Node[] = devices.map((device) => {
      const host = risk.get(device.ip)
      const score = host?.risk_score ?? 0
      const parked = looseAt.get(device.ip)
      return {
        ip: device.ip,
        x: parked ? parked.x : place(device).x,
        y: parked ? parked.y : place(device).y,
        loose: !!parked,
        // Radius reads exposure, not traffic: this is the attack-surface view.
        r: 5 + Math.sqrt(Math.max(score, device.threat === 'high' ? 40 : 8)) * 1.15,
        name: device.name ?? host?.host ?? null,
        vendor: device.vendor,
        role: device.role,
        ports: host?.services?.map((service) => String(service.port)) ?? device.topPorts ?? [],
        risk: score,
        exposure: host?.exposure ?? null,
        env: envState.get(device.ip) ?? 'silent',
        flows: device.flows,
        deny: device.deny,
      }
    })

    // Separation pass. The miner lays out for topology, not for a label under
    // every node, so dense clusters arrive with circles and captions on top of
    // one another -- and a node whose address is unreadable cannot be clicked
    // with intent. Deterministic: fixed iteration count, no randomness.
    const gap = 13
    for (let pass = 0; pass < 140; pass += 1) {
      let moved = false
      for (let i = 0; i < built.length; i += 1) {
        for (let j = i + 1; j < built.length; j += 1) {
          const a = built[i]
          const b = built[j]
          if (a.loose !== b.loose) continue
          if (a.loose && b.loose) continue
          const dx = b.x - a.x
          const dy = b.y - a.y
          const need = a.r + b.r + gap
          const dist = Math.hypot(dx, dy) || 0.01
          if (dist >= need) continue
          const push = (need - dist) / 2
          const ux = dx / dist
          const uy = dy / dist
          a.x -= ux * push
          a.y -= uy * push
          b.x += ux * push
          b.y += uy * push
          moved = true
        }
      }
      if (!moved) break
    }
    const floorY = loose.length ? H - BAND - 18 : H - PAD
    for (const node of built) {
      if (node.loose) continue
      node.x = Math.max(PAD, Math.min(W - PAD, node.x))
      node.y = Math.max(PAD, Math.min(floorY, node.y))
    }

    const present = new Set(built.map((node) => node.ip))
    const kindsSeen = new Set<string>()
    const drawn = (graph?.edges ?? []).filter((edge: GraphEdge) => {
      const keep = present.has(edge.src) && present.has(edge.dst)
      if (keep) kindsSeen.add(edge.kind)
      return keep
    })
    return {
      nodes: built,
      edges: drawn,
      kinds: EDGE_ORDER.filter((kind) => kindsSeen.has(kind)),
      looseCount: loose.length,
      height: H,
      bandTop: H - BAND,
    }
  }, [graph, surface, segments])

  if (!graph || !nodes.length) return null

  const byIp = new Map(nodes.map((node) => [node.ip, node]))
  const active = selected ? byIp.get(selected) ?? null : null
  const readout = hover ?? active
  const neighbours = new Set<string>()
  if (active) {
    for (const edge of edges) {
      if (edge.src === active.ip) neighbours.add(edge.dst)
      if (edge.dst === active.ip) neighbours.add(edge.src)
    }
  }

  const toggleKind = (kind: string) =>
    setMuted((current) => {
      const next = new Set(current)
      if (next.has(kind)) next.delete(kind)
      else next.add(kind)
      return next
    })

  return (
    <div className="dx-graph">
      <div className="dx-graph-legend">
        {/* Two things a reader asks first: what does a circle mean, and what
            does a line mean. Both are spelled out here rather than left to a
            hover. NODES: size encodes exposure, fill encodes identity state
            (the same colours the address lattice uses). RELATIONS: each mined
            tie type, solid when observed and dashed when inferred. */}
        <div className="dx-gl-row">
          <span className="dx-gl-cap">{zh ? '节点' : 'NODES'}</span>
          <span className="dx-gl-item">
            <svg width="34" height="16" aria-hidden="true">
              <circle cx="6" cy="8" r="3" className="dx-gl-node" />
              <circle cx="24" cy="8" r="7" className="dx-gl-node" />
            </svg>
            {zh ? '圈越大 = 暴露评分越高' : 'BIGGER = HIGHER EXPOSURE'}
          </span>
          <span className="dx-gl-item"><i className="dx-gl-dot env-leased" />{zh ? '已绑定身份' : 'BOUND'}</span>
          <span className="dx-gl-item"><i className="dx-gl-dot env-contested" />{zh ? '地址争用' : 'CONTESTED'}</span>
          <span className="dx-gl-item"><i className="dx-gl-dot env-unbound" />{zh ? '有流量·无绑定' : 'UNBOUND'}</span>
          <span className="dx-gl-item"><i className="dx-gl-dot env-silent" />{zh ? '无观测' : 'SILENT'}</span>
          <span className="dx-gl-hint">{zh ? '点击节点 → 联动下方地址与判定' : 'CLICK A NODE → DRIVES THE LATTICE + LEDGER'}</span>
        </div>
        <div className="dx-gl-row">
          <span className="dx-gl-cap">{zh ? '连线 · 已挖掘的关系' : 'EDGES · MINED RELATIONS'}</span>
          {kinds.map((kind) => (
            <button
              type="button"
              key={kind}
              className={`dx-gl is-${kind}${muted.has(kind) ? ' is-off' : ''}`}
              onClick={() => toggleKind(kind)}
              aria-pressed={!muted.has(kind)}
              title={zh ? '点击可隐藏/显示该类关系' : 'click to hide / show this relation'}
            >
              <svg width="26" height="8" aria-hidden="true">
                <line x1="1" y1="4" x2="25" y2="4" />
              </svg>
              {zh ? EDGE_LABEL[kind]?.[0] ?? kind : EDGE_LABEL[kind]?.[1] ?? kind}
            </button>
          ))}
          <span className="dx-gl-hint">{zh ? '实线=硬证据 · 虚线=推断' : 'SOLID = OBSERVED · DASHED = INFERRED'}</span>
        </div>
      </div>

      <svg viewBox={`0 0 ${W} ${height}`} className="dx-graph-svg" role="img"
        aria-label={zh ? '攻击面关系图' : 'Attack surface relation graph'}>
        <defs>
          <pattern id="dx-node-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" />
          </pattern>
        </defs>
        {looseCount ? (
          <g className="dx-graph-band">
            <line x1={PAD} y1={bandTop} x2={W - PAD} y2={bandTop} />
            <text x={PAD} y={bandTop + 16}>
              {zh
                ? `${looseCount} 个资产没有任何已挖掘的关系 —— 不是没有关系,是这批证据里没找到`
                : `${looseCount} ASSETS WITH NO MINED RELATION — NOT PROOF OF ISOLATION, ONLY THAT THIS EVIDENCE FOUND NONE`}
            </text>
          </g>
        ) : null}
        <g className="dx-graph-edges">
          {edges.map((edge, index) => {
            if (muted.has(edge.kind)) return null
            const a = byIp.get(edge.src)
            const b = byIp.get(edge.dst)
            if (!a || !b) return null
            const lit = !!active && (edge.src === active.ip || edge.dst === active.ip)
            return (
              <line
                key={`${edge.src}-${edge.dst}-${index}`}
                x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                className={`dx-ge is-${edge.kind}${edge.observed ? '' : ' is-inferred'}${lit ? ' is-lit' : ''}${active && !lit ? ' is-dim' : ''}`}
                strokeWidth={Math.min(2.4, 0.6 + edge.weight * 0.35)}
              >
                <title>{`${edge.src} ↔ ${edge.dst} · ${edge.kind} · ${edge.evidence}`}</title>
              </line>
            )
          })}
        </g>
        <g className="dx-graph-nodes">
          {nodes.map((node) => {
            const isActive = active?.ip === node.ip
            const near = neighbours.has(node.ip)
            return (
              <g
                key={node.ip}
                className={`dx-gn env-${node.env}${isActive ? ' is-active' : ''}${active && !isActive && !near ? ' is-dim' : ''}`}
                transform={`translate(${node.x} ${node.y})`}
                role="button"
                tabIndex={0}
                aria-label={`${node.ip} ${node.role}`}
                onMouseEnter={() => setHover(node)}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(node)}
                onBlur={() => setHover(null)}
                onClick={() => onSelect(node.ip)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onSelect(node.ip)
                  }
                }}
              >
                <circle r={node.r} className="dx-gn-body" />
                {node.env === 'unbound' ? <circle r={node.r} className="dx-gn-hatch" /> : null}
                {node.risk >= 70 || node.env === 'contested' ? <circle r={node.r + 3.5} className="dx-gn-ring" /> : null}
                <text y={node.r + 11} textAnchor="middle" className="dx-gn-label">
                  {node.ip.split('.').pop()}
                </text>
              </g>
            )
          })}
        </g>
      </svg>

      <div className={`dx-readout${readout ? ` is-${readout.env}` : ' is-idle'}`}>
        {readout ? (
          <>
            <b>{readout.ip}</b>
            <span>{readout.name ?? '—'}</span>
            <span>{readout.vendor} · {readout.role}</span>
            <span>{zh ? '暴露评分' : 'EXPOSURE'} {readout.risk || '—'}{readout.exposure ? ` · ${readout.exposure}` : ''}</span>
            <span>{zh ? '端口' : 'PORTS'} {readout.ports.slice(0, 10).join(' · ') || '—'}</span>
            <span>{zh ? '流量/拒绝' : 'FLOWS/DENY'} {readout.flows}/{readout.deny}</span>
          </>
        ) : (
          <span>
            {zh
              ? `${nodes.length} 个资产 · ${edges.length} 条已挖掘关系 · 悬停读取,点击联动`
              : `${nodes.length} ASSETS · ${edges.length} MINED RELATIONS · HOVER TO READ, CLICK TO DRIVE`}
          </span>
        )}
      </div>
    </div>
  )
}
