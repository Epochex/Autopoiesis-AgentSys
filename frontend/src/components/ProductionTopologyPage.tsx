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

const stableUnit = (text: string, salt = 0): number => {
  let hash = 2166136261 ^ salt
  for (let i = 0; i < text.length; i += 1) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619)
  return ((hash >>> 0) % 10_000) / 10_000
}

const graphDevice = (asset: Asset, index: number, total: number, peer = false): GraphDevice => {
  const angle = (Math.PI * 2 * index) / Math.max(total, 1) + stableUnit(asset.ip, 17) * 0.32
  const radius = peer ? 0.72 + stableUnit(asset.ip, 31) * 0.18 : 0.16 + Math.sqrt((index + 1) / Math.max(total, 1)) * 0.5
  const activity = asset.activity
  return {
    ip: asset.ip,
    name: asset.name || asset.ip,
    mac: asset.mac ?? null,
    vendor: 'observed asset',
    os: null,
    role: peer ? 'cross-segment peer' : 'asset',
    intf: asset.segment,
    flows: activity?.flows24h ?? 0,
    deny: activity?.denied24h ?? 0,
    accept: Math.max(0, (activity?.flows24h ?? 0) - (activity?.denied24h ?? 0)),
    leases: 0,
    topPorts: [],
    threat: severityThreat(asset.risk),
    seenBy: 'traffic',
    x: Math.cos(angle) * radius + (peer ? 0.08 : -0.08),
    y: Math.sin(angle) * Math.min(radius, 0.82),
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
    const peerAssets = peerIps.map((ip) => assets.get(ip) ?? ({ ip, name: ip, segment: 'cross-segment', active24h: true } as Asset))
    const devices = [
      ...native.map((asset, index) => graphDevice(asset, index, native.length)),
      ...peerAssets.map((asset, index) => graphDevice(asset, index, peerAssets.length, true)),
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
    const risky = devices.filter((device) => device.threat !== 'ok').map((device) => device.ip)
    graphs[segment.cidr] = {
      cidr: segment.cidr,
      devices,
      edges,
      clusters: [
        { id: 'segment-assets', members: native.map((asset) => asset.ip), role: 'asset', vendor: segment.name, size: native.length, boundBy: [], deny: native.reduce((sum, asset) => sum + (asset.activity?.denied24h ?? 0), 0) },
        ...(peerAssets.length ? [{ id: 'boundary-peers', members: peerAssets.map((asset) => asset.ip), role: 'cross-segment', vendor: 'boundary peers', size: peerAssets.length, boundBy: ['codst' as const], deny: 0 }] : []),
      ],
      anomalies: risky.length ? [{ kind: 'production-risk', members: risky, detail: '当前风险融合或未关闭案件命中' }] : [],
      stats: {
        devices: devices.length,
        withTraffic: devices.filter((device) => device.flows > 0).length,
        dhcpOnly: 0,
        edges: edges.length,
        observedEdges: edges.length,
        deny: devices.reduce((sum, device) => sum + device.deny, 0),
        roles: { asset: native.length, 'cross-segment': peerAssets.length },
        vendors: {},
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
      summary: lang === 'zh' ? `${current.devices.length} 个可见资产，${current.edges.length} 条已观测跨区关系，${risky.length} 个风险命中。` : `${current.devices.length} visible assets, ${current.edges.length} observed boundary relations, ${risky.length} risk matches.`,
      communities: current.clusters.map((cluster) => ({ id: cluster.id, label: cluster.vendor, note: `${cluster.size}` })),
      patterns: risky.map((device) => ({ title: projection.risks[device.ip]?.reasons[0] ?? 'risk match', kind: 'production-risk', members: [device.ip], why: projection.risks[device.ip]?.reasons.join('；') ?? '', severity: device.threat === 'high' ? 'high' : 'medium' })),
      corridors: current.edges.slice(0, 12).map((edge) => ({ src: edge.src, dst: edge.dst, why: edge.evidence })),
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
