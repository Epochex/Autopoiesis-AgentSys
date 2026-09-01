import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Lang } from '../i18n'
import type {
  DataStats,
  Device,
  DeviceProfile,
  GraphAnalysis,
  GraphDevice,
  GraphEdge,
  ProfilePeer,
  Subnet,
  SubnetGraph,
  Topology,
} from '../types'
import { LiveAlerts } from './LiveAlerts'
import { TopologyCanvas, type ProductionTopologyContext } from './TopologyCanvas'
import type { Threat, WanThreat } from './ThreatCard'

interface Segment {
  cidr: string
  name: string
  role: string
  assetCount: number
  active24h: number
}

interface Activity {
  flows24h: number
  bytes24h: number
  denied24h: number
  peers24h: number
  lastSeenAt?: string
  observedOutboundServices?: string[]
}

interface Asset {
  ip: string
  mac?: string | null
  name: string
  segment: string
  active24h: boolean
  risk?: string | null
  activity?: Activity | null
  /** FortiGate live fingerprint (vendor / os / hardware type), when the router typed it. */
  identity?: { vendor?: string | null; os?: string | null; type?: string | null; family?: string | null } | null
  /** Console role derived from the router's hardware_type (camera/mobile/workstation/server). */
  deviceClass?: string | null
}

interface BoundaryRecord {
  source: string
  destination: string
  sourceSegment: string
  destinationSegment: string
  service: string
  port?: number | null
  action: string
  flows: number
  lastSeenAt: string
}

interface EgressRecord {
  source: string
  sourceSegment: string
  destination: string
  country?: string
  service: string
  port?: number | null
  action: string
  flows: number
  bytes: number
  lastSeenAt?: string
}

interface RiskAsset {
  asset: string
  name: string
  segment: string
  severity: string
  reasons: string[]
  caseIds: string[]
  activity?: Activity | null
}

interface ProductionOverview {
  ok: boolean
  mode: 'production_observed'
  observedAt: string
  freshness: { lagSeconds?: number | null }
  inventory: { knownAssets: number; active24h: number; segments: Segment[]; assets: Asset[] }
  changes: { id: string; asset: string; severity: string; title: string }[]
  crossSegment: { records: BoundaryRecord[] }
  internetOutbound?: { records: EgressRecord[] }
  riskFusion: RiskAsset[]
  externalSources: { ip: string; events: number; eventTypes: string[]; ports: number[]; lastSeenAt: string }[]
  cases: { caseId: string; status: string; severity: string; subject: string; title: string }[]
  funnel: { facts: number; security_events: number; alerts: number; cases: number }
}

export interface ProductionProjection {
  topology: Topology
  stats: DataStats
  graphs: Record<string, SubnetGraph>
  context: ProductionTopologyContext
  risks: Record<string, RiskAsset>
}

const severityThreat = (value?: string | null): 'high' | 'watch' | 'ok' => {
  if (value === 'critical' || value === 'high') return 'high'
  if (value === 'medium') return 'watch'
  return 'ok'
}

/* ── device-type classification ──
 * The router's live fingerprint (deviceClass) is authoritative. For hosts the
 * FortiGate never typed, fall back to what the evidence itself shows: hostname
 * conventions and NetBIOS chatter. Anything else stays 未识别 — an unlabelled
 * dot is honest, a guessed icon is not. */
const NAME_MOBILE = /iphone|ipad|xiaomi|redmi|poco[-_]|honor|iqoo|oppo|vivo[-_]|oneplus|galaxy|pixel[-_]|mate\s?\d|nova[-_ ]\d|noh-an|android|-phone/i
const NAME_WORKSTATION = /^desktop-|^laptop-|^win(?![a-z])|-pc\b|^pc-|macbook|imac|thinkpad|-nb\b/i
const NAME_SERVER = /dss|onebox|\bnvr\b|server|-srv\b|^srv|\bnas\b/i
const NAME_CAMERA = /\bipc\b|ipc-|\bcam\b|camera|^dh-/i

export const classifyAsset = (asset: Asset): string => {
  if (asset.deviceClass) return asset.deviceClass
  const name = asset.name && asset.name !== asset.ip ? asset.name : ''
  if (name) {
    if (NAME_MOBILE.test(name)) return 'mobile'
    if (NAME_WORKSTATION.test(name)) return 'workstation'
    if (NAME_SERVER.test(name)) return 'server'
    if (NAME_CAMERA.test(name)) return 'camera'
  }
  const services = asset.activity?.observedOutboundServices ?? []
  if (services.includes('udp/137') || services.includes('udp/138')) return 'workstation'
  return 'unknown'
}

const CLASS_ORDER = ['workstation', 'mobile', 'camera', 'server', 'unknown']

const baseDevice = (asset: Asset, role: string): Omit<GraphDevice, 'x' | 'y'> => {
  const activity = asset.activity
  return {
    ip: asset.ip,
    name: asset.name || asset.ip,
    mac: asset.mac ?? null,
    vendor: asset.identity?.vendor ?? 'unknown',
    os: asset.identity?.os ?? null,
    role,
    intf: asset.segment,
    flows: activity?.flows24h ?? 0,
    deny: activity?.denied24h ?? 0,
    accept: Math.max(0, (activity?.flows24h ?? 0) - (activity?.denied24h ?? 0)),
    leases: 0,
    topPorts: [],
    threat: severityThreat(asset.risk),
    seenBy: 'traffic',
  }
}

/* ── force layout ──
 * Position IS the message: nodes that share measured or inferred relations
 * pull together (spring), everything repels at short range, each device class
 * coheres around its own drifting centroid, silent degree-0 hosts pack tight
 * at their class core while active ones claim space. Deterministic — seeded
 * from stable hashes, fixed iteration count — so the same payload always draws
 * the same map. Output normalized into SubnetGraphLayer's [-1,1] plate space. */
const stableUnit = (text: string, salt = 0): number => {
  let hash = 2166136261 ^ salt
  for (let i = 0; i < text.length; i += 1) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619)
  return ((hash >>> 0) % 10_000) / 10_000
}

interface LayoutSpec {
  ip: string
  group: string
  /** repulsion footprint — active/risky hosts claim space, silent ones pack */
  mass: number
}

const forceLayout = (specs: LayoutSpec[], edges: GraphEdge[]): Map<string, { x: number; y: number }> => {
  const n = specs.length
  const out = new Map<string, { x: number; y: number }>()
  if (!n) return out
  if (n === 1) return out.set(specs[0].ip, { x: 0, y: 0 })
  const index = new Map(specs.map((s, i) => [s.ip, i]))
  const groupNames = [...new Set(specs.map((s) => s.group))]
  const px = new Float64Array(n)
  const py = new Float64Array(n)
  specs.forEach((s, i) => {
    const angle = -Math.PI / 2 + (Math.PI * 2 * groupNames.indexOf(s.group)) / groupNames.length
    px[i] = Math.cos(angle) * 0.45 + (stableUnit(s.ip, 11) - 0.5) * 0.55
    py[i] = Math.sin(angle) * 0.4 + (stableUnit(s.ip, 23) - 0.5) * 0.45
  })
  const springs = edges
    .map((e) => ({ a: index.get(e.src), b: index.get(e.dst), k: e.observed ? 0.055 : 0.02 }))
    .filter((s): s is { a: number; b: number; k: number } => s.a !== undefined && s.b !== undefined)
  const fx = new Float64Array(n)
  const fy = new Float64Array(n)
  for (let iter = 0; iter < 160; iter += 1) {
    fx.fill(0)
    fy.fill(0)
    for (let i = 0; i < n; i += 1) {
      for (let j = i + 1; j < n; j += 1) {
        const dx = px[i] - px[j]
        const dy = py[i] - py[j]
        const d2 = dx * dx + dy * dy + 0.0008
        const f = Math.min(0.05, (0.0011 * specs[i].mass * specs[j].mass) / d2)
        const d = Math.sqrt(d2)
        fx[i] += (dx / d) * f
        fy[i] += (dy / d) * f
        fx[j] -= (dx / d) * f
        fy[j] -= (dy / d) * f
      }
    }
    for (const s of springs) {
      const dx = px[s.b] - px[s.a]
      const dy = py[s.b] - py[s.a]
      const d = Math.sqrt(dx * dx + dy * dy) || 0.001
      const f = (d - 0.14) * s.k
      fx[s.a] += (dx / d) * f
      fy[s.a] += (dy / d) * f
      fx[s.b] -= (dx / d) * f
      fy[s.b] -= (dy / d) * f
    }
    for (const group of groupNames) {
      let cx = 0
      let cy = 0
      let count = 0
      specs.forEach((s, i) => {
        if (s.group !== group) return
        cx += px[i]
        cy += py[i]
        count += 1
      })
      if (!count) continue
      cx /= count
      cy /= count
      specs.forEach((s, i) => {
        if (s.group !== group) return
        const pull = s.mass < 1 ? 0.11 : 0.055
        fx[i] += (cx - px[i]) * pull
        fy[i] += (cy - py[i]) * pull
      })
    }
    const cool = 1 - iter / 160
    for (let i = 0; i < n; i += 1) {
      fx[i] -= px[i] * 0.008
      fy[i] -= py[i] * 0.012
      const step = Math.min(0.05, Math.hypot(fx[i], fy[i])) * (0.35 + 0.65 * cool)
      const norm = Math.hypot(fx[i], fy[i]) || 1
      px[i] += (fx[i] / norm) * step
      py[i] += (fy[i] / norm) * step
    }
  }
  // normalize into the plate, stretching to the letterbox aspect
  const minX = Math.min(...px)
  const maxX = Math.max(...px)
  const minY = Math.min(...py)
  const maxY = Math.max(...py)
  const sx = 1.76 / Math.max(0.05, maxX - minX)
  const sy = 1.56 / Math.max(0.05, maxY - minY)
  specs.forEach((s, i) => {
    out.set(s.ip, {
      x: -0.88 + (px[i] - minX) * sx,
      y: -0.78 + (py[i] - minY) * sy,
    })
  })
  return out
}

interface Endpoint {
  ip: string
  country: string
  service: string
  flows: number
  denied: number
  bytes: number
  talkers: Set<string>
}

const endpointBase = (endpoint: Endpoint): Omit<GraphDevice, 'x' | 'y'> => ({
  ip: endpoint.ip,
  name: [endpoint.service, endpoint.country].filter(Boolean).join(' · ') || endpoint.ip,
  mac: null,
  vendor: 'unknown',
  os: null,
  role: 'internet-endpoint',
  intf: 'internet',
  flows: endpoint.flows,
  deny: endpoint.denied,
  accept: Math.max(0, endpoint.flows - endpoint.denied),
  leases: 0,
  topPorts: [],
  threat: 'ok',
  seenBy: 'traffic',
})

const peerFrom = (record: BoundaryRecord, dir: 'in' | 'out'): ProfilePeer => ({
  ip: dir === 'out' ? record.destination : record.source,
  hits: record.flows,
  accept: ['accept', 'allow', 'pass', 'close'].includes(record.action.toLowerCase()) ? record.flows : 0,
  deny: ['deny', 'blocked', 'reject'].includes(record.action.toLowerCase()) ? record.flows : 0,
  bytes: 0,
  ports: record.port ? [record.port] : [],
  services: record.service ? [record.service] : [],
  country: null,
  external: false,
  kind: 'host',
  dir,
})

export function projectProductionOverview(data: ProductionOverview): ProductionProjection {
  const assets = new Map(data.inventory.assets.map((asset) => [asset.ip, asset]))
  const risks = Object.fromEntries(data.riskFusion.map((risk) => [risk.asset, risk]))
  for (const [ip, risk] of Object.entries(risks)) {
    const asset = assets.get(ip)
    if (asset) asset.risk = risk.severity
  }

  const interfaces = data.inventory.segments.map((segment, index) => ({
    name: segment.name || `segment-${index + 1}`,
    role: segment.role,
    flows: data.inventory.assets
      .filter((asset) => asset.segment === segment.cidr)
      .reduce((sum, asset) => sum + (asset.activity?.flows24h ?? 0), 0),
    kind: 'lan',
  }))
  const subnets = data.inventory.segments.map((segment, index) => ({
    cidr: segment.cidr,
    hosts: segment.assetCount,
    flows: interfaces[index].flows,
    accept: data.inventory.assets
      .filter((asset) => asset.segment === segment.cidr)
      .reduce((sum, asset) => sum + Math.max(0, (asset.activity?.flows24h ?? 0) - (asset.activity?.denied24h ?? 0)), 0),
    intf: interfaces[index].name,
  }))
  const topology: Topology = {
    core: { name: 'FortiGate', ip: '192.168.1.1', model: 'production observation' },
    interfaces,
    subnets,
    anchors: data.inventory.assets.map((asset) => ({ ip: asset.ip, name: asset.name, role: 'asset', intf: asset.segment })),
  }

  const graphs: Record<string, SubnetGraph> = {}
  for (const segment of data.inventory.segments) {
    const native = data.inventory.assets.filter((asset) => asset.segment === segment.cidr)
    const boundary = data.crossSegment.records.filter((record) => record.sourceSegment === segment.cidr || record.destinationSegment === segment.cidr)
    const peerIps = [...new Set(boundary.map((record) => record.sourceSegment === segment.cidr ? record.destination : record.source))]
    const nativeIps = new Set(native.map((asset) => asset.ip))
    const peerAssets = peerIps.filter((ip) => !nativeIps.has(ip)).map((ip) => assets.get(ip) ?? ({ ip, name: ip, segment: 'cross-segment', active24h: true } as Asset))

    const byClass = new Map<string, Asset[]>()
    for (const asset of native) {
      const role = classifyAsset(asset)
      byClass.set(role, [...(byClass.get(role) ?? []), asset])
    }
    const groups = [...byClass.entries()].sort(
      (a, b) => (CLASS_ORDER.indexOf(a[0]) + 1 || 99) - (CLASS_ORDER.indexOf(b[0]) + 1 || 99),
    )
    /* Internet egress, clustered by destination: every measured private→public
     * pair from this segment rolls up into one endpoint node (country/service/
     * talker count), so N hosts sharing one destination read as one relation. */
    const egress = (data.internetOutbound?.records ?? []).filter((record) => nativeIps.has(record.source))
    const endpointMap = new Map<string, Endpoint>()
    for (const record of egress) {
      const endpoint = endpointMap.get(record.destination) ?? {
        ip: record.destination,
        country: record.country ?? '',
        service: record.service || (record.port ? `:${record.port}` : ''),
        flows: 0, denied: 0, bytes: 0, talkers: new Set<string>(),
      }
      endpoint.flows += record.flows
      endpoint.bytes += record.bytes
      if (['deny', 'blocked', 'reject'].includes(record.action)) endpoint.denied += record.flows
      if (!endpoint.country && record.country) endpoint.country = record.country
      endpoint.talkers.add(record.source)
      endpointMap.set(record.destination, endpoint)
    }
    const endpoints = [...endpointMap.values()].sort((a, b) => b.flows - a.flows).slice(0, 12)
    const endpointIps = new Set(endpoints.map((endpoint) => endpoint.ip))

    /* Node roster first (no positions yet): the force layout needs the edge
     * list, and edges only need IPs. Active/risky hosts get more repulsion
     * mass than silent ones so the important dots claim space. */
    const roster: { base: Omit<GraphDevice, 'x' | 'y'>; group: string; mass: number }[] = [
      ...groups.flatMap(([role, members]) => members.map((asset) => ({
        base: baseDevice(asset, role),
        group: role,
        mass: severityThreat(asset.risk) !== 'ok' ? 1.7 : asset.active24h ? 1.2 : 0.55,
      }))),
      ...peerAssets.map((asset) => ({ base: baseDevice(asset, 'cross-segment peer'), group: 'peers', mass: 1.1 })),
      ...endpoints.map((endpoint) => ({ base: endpointBase(endpoint), group: 'wan', mass: 1.6 })),
    ]
    const deviceIps = new Set(roster.map((item) => item.base.ip))

    const edges: GraphEdge[] = boundary
      .filter((record) => deviceIps.has(record.source) && deviceIps.has(record.destination))
      .map((record) => ({
        src: record.source,
        dst: record.destination,
        kind: 'codst',
        weight: Math.max(0.7, Math.min(4, Math.log10(record.flows + 1))),
        hits: record.flows,
        evidence: `${record.action} · ${record.service || (record.port ? `:${record.port}` : 'service unknown')} · ${record.lastSeenAt}`,
        observed: true,
      }))

    /* Same-destination inference: the FortiGate routes, so lateral L2 traffic
     * never reaches the log — but two hosts measured against the SAME boundary
     * destination+service are related, and that is exactly what codst asserts.
     * Star per destination (hub = heaviest talker), dashed as inferred. */
    const seen = new Set(edges.map((edge) => [edge.src, edge.dst].sort().join('|')))
    const byDest = new Map<string, { src: string; flows: number }[]>()
    for (const record of boundary) {
      if (record.sourceSegment !== segment.cidr || !nativeIps.has(record.source)) continue
      const key = `${record.destination} · ${record.service || (record.port ? `:${record.port}` : '')}`
      byDest.set(key, [...(byDest.get(key) ?? []), { src: record.source, flows: record.flows }])
    }
    let inferredBudget = 60
    for (const [dest, talkers] of byDest) {
      const sources = [...new Map(talkers.map((t) => [t.src, t])).values()].sort((a, b) => b.flows - a.flows)
      if (sources.length < 2) continue
      const hub = sources[0]
      for (const other of sources.slice(1, 5)) {
        const pair = [hub.src, other.src].sort().join('|')
        if (seen.has(pair) || inferredBudget <= 0) continue
        seen.add(pair)
        inferredBudget -= 1
        edges.push({
          src: hub.src,
          dst: other.src,
          kind: 'codst',
          weight: Math.max(0.6, Math.min(2.2, Math.log10(Math.min(hub.flows, other.flows) + 1) * 0.7)),
          hits: Math.min(hub.flows, other.flows),
          evidence: `共同目的 ${dest}`,
          observed: false,
        })
      }
    }

    /* Measured internet egress edges — merged per (host, endpoint). */
    const wanPairs = new Map<string, { src: string; dst: string; hits: number; evidence: string }>()
    for (const record of egress) {
      if (!endpointIps.has(record.destination)) continue
      const key = `${record.source}|${record.destination}`
      const svc = record.service || (record.port ? `:${record.port}` : '')
      const pair = wanPairs.get(key) ?? {
        src: record.source, dst: record.destination, hits: 0,
        evidence: `${record.action} · ${svc}${record.country ? ` · ${record.country}` : ''} · ${record.lastSeenAt ?? ''}`,
      }
      pair.hits += record.flows
      wanPairs.set(key, pair)
    }
    for (const pair of wanPairs.values()) {
      edges.push({
        src: pair.src, dst: pair.dst, kind: 'wan',
        weight: Math.max(0.7, Math.min(4, Math.log10(pair.hits + 1))),
        hits: pair.hits, evidence: pair.evidence, observed: true,
      })
    }

    const positions = forceLayout(
      roster.map((item) => ({ ip: item.base.ip, group: item.group, mass: item.mass })),
      edges,
    )
    const devices: GraphDevice[] = roster.map((item) => ({
      ...item.base,
      ...(positions.get(item.base.ip) ?? { x: 0, y: 0 }),
    }))

    const observedEdges = edges.filter((edge) => edge.observed).length
    const risky = devices.filter((device) => device.threat !== 'ok').map((device) => device.ip)
    const vendors: Record<string, number> = {}
    for (const asset of native) {
      const vendor = asset.identity?.vendor
      if (vendor) vendors[vendor] = (vendors[vendor] ?? 0) + 1
    }
    graphs[segment.cidr] = {
      cidr: segment.cidr,
      devices,
      edges,
      clusters: [
        ...groups.map(([role, members]) => ({
          id: `class-${role}`,
          members: members.map((asset) => asset.ip),
          role,
          vendor: '',
          size: members.length,
          boundBy: [],
          deny: members.reduce((sum, asset) => sum + (asset.activity?.denied24h ?? 0), 0),
        })),
        ...(peerAssets.length ? [{ id: 'boundary-peers', members: peerAssets.map((asset) => asset.ip), role: 'cross-segment', vendor: 'boundary peers', size: peerAssets.length, boundBy: ['codst' as const], deny: 0 }] : []),
        ...(endpoints.length ? [{ id: 'internet-endpoints', members: endpoints.map((endpoint) => endpoint.ip), role: 'internet', vendor: '', size: endpoints.length, boundBy: ['wan' as const], deny: endpoints.reduce((sum, endpoint) => sum + endpoint.denied, 0) }] : []),
      ],
      anomalies: risky.length ? [{ kind: 'production-risk', members: risky, detail: '当前风险融合或未关闭案件命中' }] : [],
      stats: {
        devices: devices.length,
        withTraffic: devices.filter((device) => device.flows > 0).length,
        dhcpOnly: 0,
        edges: edges.length,
        observedEdges,
        deny: devices.reduce((sum, device) => sum + device.deny, 0),
        roles: Object.fromEntries(groups.map(([role, members]) => [role, members.length])),
        vendors,
      },
    }
  }

  const profiles: Record<string, DeviceProfile> = {}
  for (const asset of data.inventory.assets) {
    /* The traffic portrait lists internet destinations next to boundary peers —
     * measured private→public pairs with country/service, marked external so
     * the ego overlay fans them into its WAN column. */
    const egressPeers = (data.internetOutbound?.records ?? [])
      .filter((record) => record.source === asset.ip)
      .map((record): ProfilePeer => ({
        ip: record.destination,
        hits: record.flows,
        accept: ['accept', 'allow', 'pass', 'close'].includes(record.action) ? record.flows : 0,
        deny: ['deny', 'blocked', 'reject'].includes(record.action) ? record.flows : 0,
        bytes: record.bytes,
        ports: record.port ? [record.port] : [],
        services: record.service ? [record.service] : [],
        country: record.country || null,
        external: true,
        kind: 'host',
        dir: 'out',
      }))
    const outbound = [
      ...data.crossSegment.records.filter((record) => record.source === asset.ip).map((record) => peerFrom(record, 'out')),
      ...egressPeers,
    ]
    const inbound = data.crossSegment.records.filter((record) => record.destination === asset.ip).map((record) => peerFrom(record, 'in'))
    const graphDev = Object.values(graphs).flatMap((graph) => graph.devices).find((device) => device.ip === asset.ip)
    profiles[asset.ip] = {
      ok: true,
      ip: asset.ip,
      device: graphDev ?? null,
      status: { state: asset.active24h ? 'idle' : 'offline', lastSeenSec: null, lastSeenText: asset.activity?.lastSeenAt ?? null, hasLiveSession: false },
      ports: null,
      outbound,
      inbound,
      hours: [],
      tags: [asset.segment, ...(asset.risk ? [`risk:${asset.risk}`] : [])],
      totals: {
        outHits: outbound.reduce((sum, peer) => sum + peer.hits, 0),
        inHits: inbound.reduce((sum, peer) => sum + peer.hits, 0),
        bytes: asset.activity?.bytes24h ?? 0,
        extPeers: new Set(egressPeers.map((peer) => peer.ip)).size,
        intPeers: new Set([...outbound, ...inbound].filter((peer) => !peer.external).map((peer) => peer.ip)).size,
      },
      window: { from: 'latest 24h', to: data.observedAt },
      sampled: false,
    }
  }

  const activeCases = data.cases.filter((item) => ['open', 'investigating', 'escalated'].includes(item.status)).length
  const externalEvents = data.externalSources.reduce((sum, source) => sum + source.events, 0)
  return {
    topology,
    graphs,
    risks,
    stats: {
      source: 'ClickHouse · FortiGate · 生产事实流',
      windowDays: [data.observedAt.slice(0, 10)],
      adminLoginFailed: externalEvents,
      distinctSrc: data.externalSources.length,
      topAttackerSrc: data.externalSources.slice(0, 5).map((source) => [source.ip, source.events]),
      lockouts: 0,
      denyCount: data.inventory.assets.reduce((sum, asset) => sum + (asset.activity?.denied24h ?? 0), 0),
      topDenyPorts: [],
      topDenySrc: [],
      acceptPermit: 0,
      sessionClash: 0,
    },
    context: {
      observedAt: data.observedAt,
      lagSeconds: data.freshness.lagSeconds ?? null,
      activeCases,
      changes: data.changes.length,
      externalSources: data.externalSources,
      profiles,
    },
  }
}

function ProductionSearch({ data, onPick, lang }: { data: ProductionOverview; onPick: (ip: string, cidr: string) => void; lang: Lang }) {
  const [query, setQuery] = useState('')
  const hits = useMemo(() => {
    const value = query.trim().toLowerCase()
    if (!value) return []
    return data.inventory.assets.filter((asset) => `${asset.ip} ${asset.name} ${asset.mac ?? ''} ${asset.segment}`.toLowerCase().includes(value)).slice(0, 12)
  }, [data, query])
  return (
    <div className="topo-search">
      <div className="ts-box"><span className="ts-ico">⌕</span><input className="ts-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={lang === 'zh' ? '搜索当前资产 · IP / 主机名 / MAC / 网段' : 'search current assets'} /></div>
      {query && hits.length ? <div className="ts-results">{hits.map((asset) => <button key={asset.ip} className={`ts-hit t-${severityThreat(asset.risk)}`} onClick={() => { onPick(asset.ip, asset.segment); setQuery('') }}><span className="ts-hit-ip">{asset.ip}</span><span className="ts-hit-name">{asset.name}</span><span className="ts-hit-meta">{asset.segment} · {asset.activity?.flows24h ?? 0} flows/24h</span><span className="ts-hit-th">{asset.risk ?? 'observed'}</span></button>)}</div> : null}
    </div>
  )
}

export function ProductionTopologyPage({ lang, onOpenCase }: { lang: Lang; onOpenCase: (subject: string, caseId?: string) => void }) {
  const [data, setData] = useState<ProductionOverview | null>(null)
  const [error, setError] = useState(false)
  const [drillSub, setDrillSub] = useState<string | null>(null)
  const [drillDev, setDrillDev] = useState<string | null>(null)
  const [hoverDev, setHoverDev] = useState<string | null>(null)
  const [analysis, setAnalysis] = useState<GraphAnalysis | null>(null)
  const [threat, setThreat] = useState<Threat | null>(null)
  const [wan, setWan] = useState<WanThreat | null>(null)
  const [marks, setMarks] = useState<Record<string, { severity: string; verdict: string }>>({})
  const [rate, setRate] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      const response = await fetch('/api/rca/situational-overview', { headers: { Accept: 'application/json' } })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const next = await response.json() as ProductionOverview
      setData(next)
      setError(false)
    } catch {
      setError((current) => current || !data)
    }
  }, [data])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 10_000)
    return () => window.clearInterval(timer)
  }, [load])

  useEffect(() => {
    let active = true
    const tick = () => fetch('/api/rca/pulse').then((response) => response.ok ? response.json() : null).then((pulse) => {
      if (active && pulse?.live && typeof pulse.eventsPerSec === 'number') setRate(pulse.eventsPerSec)
    }).catch(() => {})
    void tick()
    const timer = window.setInterval(tick, 4_500)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  const projection = useMemo(() => data ? projectProductionOverview(data) : null, [data])
  const graph = drillSub && projection ? projection.graphs[drillSub] ?? null : null
  const tempo = rate == null ? 1 : Math.max(0.6, Math.min(3, rate / 12))

  if (!data || !projection) return <div className={`boot ${error ? 'err' : ''}`}>{error ? (lang === 'zh' ? '生产态势接口不可达' : 'PRODUCTION SITUATION UNREACHABLE') : <span className="orbit" />}</div>

  const openSubnet = (subnet: Subnet | null) => {
    setDrillSub(subnet?.cidr ?? null)
    setDrillDev(null)
    setThreat(null)
    setWan(null)
    setAnalysis(null)
  }
  const inspectRisk = (device: Device | null) => {
    if (!device) { setThreat(null); return }
    const risk = projection.risks[device.ip]
    setThreat({
      ip: device.ip,
      loading: false,
      severity: risk?.severity ?? 'low',
      verdict: risk ? (lang === 'zh' ? '生产风险融合命中' : 'PRODUCTION RISK MATCH') : (lang === 'zh' ? '当前无风险融合命中' : 'NO CURRENT RISK MATCH'),
      analysis: risk?.reasons.join('；') || (lang === 'zh' ? '当前生产投影中没有该资产的未关闭案件或行为偏差。' : 'No open case or behavior deviation in the current projection.'),
      impactPeers: graph?.edges.filter((edge) => edge.src === device.ip || edge.dst === device.ip).map((edge) => ({ ip: edge.src === device.ip ? edge.dst : edge.src, relation: edge.evidence })).slice(0, 6),
      mostLikely: risk?.reasons[0] ?? '',
      worstCase: '',
      recovery: { action: lang === 'zh' ? '打开关联案件核验' : 'open associated case', eta: 'operator' },
      model: 'production evidence projection',
    })
  }
  const inspectExternal = (ip: string) => {
    const source = data.externalSources.find((item) => item.ip === ip)
    if (!source) return
    setWan({
      ip,
      loading: false,
      attempts: source.events,
      verdict: lang === 'zh' ? '生产事实流中的公网来源' : 'EXTERNAL SOURCE IN PRODUCTION FACTS',
      severity: source.eventTypes.includes('management_exposure') ? 'critical' : source.eventTypes.includes('admin_login_failed') ? 'high' : 'medium',
      campaign: `${source.eventTypes.join(' · ') || 'event'} · ${source.events} events · ${source.lastSeenAt}`,
      attribution: lang === 'zh' ? '仅依据已落库事件分类' : 'landed event classification only',
      blast: source.ports.length ? `ports ${source.ports.join(', ')}` : (lang === 'zh' ? '端口信息缺失' : 'ports unavailable'),
      actions: [],
      impactNodes: [],
      model: 'production evidence projection',
    })
  }
  const analyze = (cidr: string) => {
    const current = projection.graphs[cidr]
    const risky = current.devices.filter((device) => device.threat !== 'ok')
    setAnalysis({
      cidr,
      summary: lang === 'zh'
        ? `${current.devices.length} 个可见资产，${current.stats.observedEdges} 条实测跨区关系 + ${current.edges.length - current.stats.observedEdges} 条同目的推断，${risky.length} 个风险命中。`
        : `${current.devices.length} visible assets, ${current.stats.observedEdges} observed boundary relations + ${current.edges.length - current.stats.observedEdges} shared-destination inferences, ${risky.length} risk matches.`,
      communities: current.clusters.map((cluster) => ({ id: cluster.id, label: cluster.vendor, note: `${cluster.size}` })),
      patterns: risky.map((device) => ({ title: projection.risks[device.ip]?.reasons[0] ?? 'risk match', kind: 'production-risk', members: [device.ip], why: projection.risks[device.ip]?.reasons.join('；') ?? '', severity: device.threat === 'high' ? 'high' : 'medium' })),
      corridors: current.edges.filter((edge) => edge.observed).slice(0, 12).map((edge) => ({ src: edge.src, dst: edge.dst, why: edge.evidence })),
      flow: 'ClickHouse facts → production topology projection',
      blindSpot: lang === 'zh' ? '同网段横向通信需要交换机镜像、NetFlow 或端点网络探针。' : 'Same-segment movement requires switch telemetry or endpoint sensors.',
      actions: [],
      model: 'production evidence projection',
    })
  }

  return (
    <>
      <LiveAlerts lang={lang} theaterActive={false} onOpen={(subject) => onOpenCase(subject)} />
      <section className={`canvas-wrap ${threat || wan ? 'tall' : drillSub ? 'mid' : ''}`}>
        <ProductionSearch data={data} lang={lang} onPick={(ip, cidr) => { setDrillSub(cidr); setDrillDev(ip) }} />
        <TopologyCanvas
          topo={projection.topology}
          stats={projection.stats}
          activeKey="production_observed"
          drillSub={drillSub}
          drillDev={drillDev}
          tempo={tempo}
          marks={marks}
          threat={threat}
          lang={lang}
          meshCount={data.inventory.knownAssets}
          meshLoading={false}
          hover3D={null}
          hover3DCidr={null}
          topoAlert={null}
          wan={wan}
          graph={graph}
          graphAnalysis={analysis}
          hoverDev={hoverDev}
          onHoverDev={setHoverDev}
          onGraphAnalyze={analyze}
          onCloseGraphAnalysis={() => setAnalysis(null)}
          onWan={inspectExternal}
          onCloseWan={() => setWan(null)}
          onOpen3D={() => {}}
          onCloseThreat={() => setThreat(null)}
          onSub={openSubnet}
          onDev={(device) => inspectRisk(device)}
          onBatch={(cidr) => {
            const next = { ...marks }
            for (const device of projection.graphs[cidr]?.devices ?? []) {
              const risk = projection.risks[device.ip]
              if (risk) next[device.ip] = { severity: risk.severity, verdict: risk.reasons[0] ?? risk.severity }
            }
            setMarks(next)
          }}
          onDiagnose={() => threat && onOpenCase(threat.ip, projection.risks[threat.ip]?.caseIds[0])}
          production={projection.context}
        />
        {rate !== null ? <div className="live-rate"><span className="rate-dot" />{rate}/s · {lang === 'zh' ? '生产事实流' : 'PRODUCTION FACTS'}</div> : null}
      </section>
    </>
  )
}
