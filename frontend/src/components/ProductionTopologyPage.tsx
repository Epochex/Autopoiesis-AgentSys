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

/* ── banded layout ──
 * A console board, not a scatter: each device class is a horizontal band (its
 * header drawn by SubnetGraphLayer), hosts grid-aligned inside it — risky
 * first, then active by traffic, silent hosts trailing — so reading order IS
 * the priority order. Boundary peers hold a column on the right; the space
 * between belongs to the boundary bus every cross-segment edge routes through.
 * Coordinates stay in the [-1,1] space SubnetGraphLayer scales onto the plate. */
const BAND_X0 = -0.92
const BAND_X1 = 0.42
export const PEER_X = 0.68

const assetRank = (asset: Asset): number =>
  (severityThreat(asset.risk) !== 'ok' ? 2 : 0) + (asset.active24h ? 1 : 0)

const clusterDevices = (groups: [string, Asset[]][]): GraphDevice[] => {
  const devices: GraphDevice[] = []
  if (!groups.length) return devices
  const maxN = Math.max(...groups.map(([, members]) => members.length))
  const cols = Math.max(8, Math.min(22, Math.ceil(Math.sqrt(maxN) * 2.6)))
  const pitchX = (BAND_X1 - BAND_X0) / cols
  const rowsFor = (n: number) => Math.ceil(n / cols)
  const HEADER_UNITS = 0.7
  const GAP_UNITS = 0.55
  const totalUnits = groups.reduce((sum, [, members]) => sum + rowsFor(members.length) + HEADER_UNITS, 0)
    + (groups.length - 1) * GAP_UNITS
  const pitchY = Math.min(0.12, 1.72 / totalUnits)
  let cursor = -(totalUnits * pitchY) / 2 + 0.04
  for (const [role, members] of groups) {
    cursor += HEADER_UNITS * pitchY
    const sorted = [...members].sort(
      (a, b) => assetRank(b) - assetRank(a) || (b.activity?.flows24h ?? 0) - (a.activity?.flows24h ?? 0),
    )
    sorted.forEach((asset, i) => {
      devices.push({
        ...baseDevice(asset, role),
        x: BAND_X0 + ((i % cols) + 0.5) * pitchX,
        y: cursor + Math.floor(i / cols) * pitchY,
      })
    })
    cursor += (rowsFor(sorted.length) + GAP_UNITS) * pitchY
  }
  return devices
}

const peerDevice = (asset: Asset, index: number, total: number): GraphDevice => {
  const columns = total > 14 ? 2 : 1
  const rows = Math.ceil(total / columns)
  const pitch = Math.min(0.14, 1.68 / Math.max(rows, 1))
  const column = Math.floor(index / rows)
  const row = index % rows
  return {
    ...baseDevice(asset, 'cross-segment peer'),
    x: PEER_X + column * 0.12,
    y: -((rows - 1) * pitch) / 2 + row * pitch,
  }
}

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
    const bandDevices = clusterDevices(groups)
    /* Peers sort by the vertical position of their heaviest in-segment partner,
     * so each band's trunk fan lands on a contiguous run of the peer column
     * instead of criss-crossing the others'. */
    const deviceY = new Map(bandDevices.map((device) => [device.ip, device.y]))
    const partnerY = new Map<string, { y: number; flows: number }>()
    for (const record of boundary) {
      const peer = nativeIps.has(record.source) ? record.destination : record.source
      const partner = nativeIps.has(record.source) ? record.source : record.destination
      const y = deviceY.get(partner)
      if (y === undefined) continue
      const current = partnerY.get(peer)
      if (!current || record.flows > current.flows) partnerY.set(peer, { y, flows: record.flows })
    }
    const sortedPeers = [...peerAssets].sort(
      (a, b) => (partnerY.get(a.ip)?.y ?? 1) - (partnerY.get(b.ip)?.y ?? 1),
    )
    const devices = [
      ...bandDevices,
      ...sortedPeers.map((asset, index) => peerDevice(asset, index, sortedPeers.length)),
    ]
    const deviceIps = new Set(devices.map((device) => device.ip))

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
    const outbound = data.crossSegment.records.filter((record) => record.source === asset.ip).map((record) => peerFrom(record, 'out'))
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
        extPeers: 0,
        intPeers: new Set([...outbound, ...inbound].map((peer) => peer.ip)).size,
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
