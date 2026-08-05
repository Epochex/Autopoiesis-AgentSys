export interface Evidence {
  evidenceId: string
  source: string
  summary: string
}
export interface Diagnosis {
  rootCauseKey: string
  rootCause: string
  confidence: number
  readonly: boolean
  evidence: Evidence[]
  recommendedActions: string[]
}
export interface TraceEvent {
  kind: string
  payload: Record<string, unknown>
}
export interface VerifierResult {
  passed: boolean
  errors: string[]
}
export interface RcaCase {
  id: string
  title: string
  query: string
  assets: string[]
  diagnosis: Diagnosis
  verifier: VerifierResult
  trace: TraceEvent[]
}
export interface Baseline {
  name: string
  rootCauseAccuracy: number
  evidenceRecall: number
  verifierPassRate: number
  cases: number
  notes: string
}
export interface DataStats {
  source: string
  windowDays: string[]
  adminLoginFailed: number
  distinctSrc: number
  topAttackerSrc: [string, number][]
  lockouts: number
  denyCount: number
  topDenyPorts: [string, number][]
  topDenySrc: [string, number][]
  acceptPermit: number
  sessionClash: number
}
export interface Provider {
  id: string
  label: string
  kind: string
  model: string
  reachable: boolean
  note: string
}
export interface Readiness {
  blocked: boolean
  reason: string
  syslogPortOpen: boolean
  manifestValid: boolean
}
export interface Interface {
  name: string
  role: string
  flows: number
  kind: string
}
export interface Device {
  ip: string
  flows: number
  deny: number
  accept: number
  threat: 'high' | 'watch' | 'ok'
  top_ports: string[]
}
export interface Subnet {
  cidr: string
  hosts: number
  flows: number
  accept: number
  intf: string
  devices?: Device[]
}
/* ── per-subnet device graph, mined from raw syslog (build_device_graph.py) ── */
export interface GraphDevice {
  ip: string
  name: string | null
  mac: string | null
  vendor: string
  os: string | null
  role: string
  intf: string | null
  flows: number
  deny: number
  accept: number
  leases: number
  topPorts: string[]
  threat: 'high' | 'watch' | 'ok'
  seenBy: 'traffic' | 'dhcp'
  x: number
  y: number
  /** read-only overlay from the FortiGate live inventory (fills unknowns; mined values win). */
  liveIdentity?: { hostname?: string | null; os?: string | null; vendor?: string | null; type?: string | null; source: string } | null
  /** Benchmark 态势 (cidr `mem://autopoiesis`) ONLY: the evolved MEMORY RECORD this
   *  constellation node maps to (build_bench_snapshot embeds it verbatim). A bench
   *  node is a memory, not a real device, so clicking it shows this record instead of
   *  the live traffic profile. NEVER present on a live-network device. */
  memory?: {
    memory_id: string
    tier: string
    text: string
    root: string | null
    strength: number
    importance: number
    confidence: number
    tags: string[]
    evidence: { source: string; summary: string }[]
    assets: string[]
    links: string[]
    sourceTraces: string[]
    quarantined: boolean
  } | null
}
export type EdgeKind = 'clash' | 'bcast' | 'codst' | 'fleet' | 'family' | 'lease' | 'portfp'
export interface GraphEdge {
  src: string
  dst: string
  kind: EdgeKind
  weight: number
  hits: number
  evidence: string
  observed: boolean
}
export interface GraphCluster {
  id: string
  members: string[]
  role: string
  vendor: string
  size: number
  boundBy: EdgeKind[]
  deny: number
}
export interface GraphAnomaly {
  kind: string
  members: string[]
  detail: string
}
export interface SubnetGraph {
  cidr: string
  devices: GraphDevice[]
  edges: GraphEdge[]
  clusters: GraphCluster[]
  anomalies: GraphAnomaly[]
  stats: {
    devices: number
    withTraffic: number
    dhcpOnly: number
    edges: number
    observedEdges: number
    deny: number
    roles: Record<string, number>
    vendors: Record<string, number>
  }
}
/** One aggregated flow peer of a profiled host (a destination it talks to, or a source that talks at it). */
export interface ProfilePeer {
  ip: string
  hits: number
  accept: number
  deny: number
  bytes: number
  ports: number[]
  services: string[]
  country: string | null
  rdns?: string | null
  external: boolean
  kind: 'host' | 'bcast'
  dir: 'in' | 'out'
}
/** A live destination the router currently sees this host talking to (FortiView realtime). */
export interface LiveDest {
  ip: string
  resolved: string | null
  port: number | null
  sessions: number | null
  country: string | null
  bytes: number
  /** current measured throughput to this destination, bits/s (0 when idle). */
  bps?: number
  /** name of the FortiGate QoS shaper policy on this flow (e.g. "guarantee-100kbps",
   *  "400M"). A policy label, not necessarily a cap — could be a floor or a ceiling. */
  shaper?: string | null
}
export interface ProfileLive {
  destinations: LiveDest[]
  sessions: { dir: 'in' | 'out'; peer: string; port: number | null; proto: string | null; country: string | null; duration: number | null; bps?: number; shaper?: string | null }[]
  sessionCount: number
  /** total current throughput across all of this host's flows, bits/s. */
  totalBps?: number
}
/** Online/idle/offline verdict + last-seen age, from the router's live view. */
export interface DeviceStatus {
  state: 'online' | 'idle' | 'offline' | 'unknown'
  lastSeenSec: number | null
  lastSeenText: string | null
  hasLiveSession: boolean
}
/** One historical destination aggregate for a host (from ClickHouse netops.facts). */
export interface HistoryDest {
  ip: string
  country: string | null
  services: string[]
  flows: number
  bytes: number
  up?: number
  down?: number
  denied: number
}
/** Historical device portrait over the last N days, from the ClickHouse flow store. */
export interface DeviceHistory {
  ok: boolean
  ip: string
  days: number
  flows: number
  empty?: boolean
  bytes?: number
  up?: number
  down?: number
  full?: boolean
  denied?: number
  peers?: number
  first?: string
  last?: string
  destinations?: HistoryDest[]
  daily?: { date: string; flows: number; bytes: number }[]
  hours?: number[]
  topPorts?: PortUse[]
  /** true when the requested window was empty and the query fell back to full retention. */
  widened?: boolean
}
/** One (port, protocol) a profiled host used, with the syslog service label. */
export interface PortUse {
  port: number
  proto: string
  service: string | null
  hits: number
  deny: number
}
/** Traffic portrait of one host, mined from the raw syslog sample. */
export interface DeviceProfile {
  ok: boolean
  loading?: boolean
  ip: string
  device?: GraphDevice | null
  /** live online/offline + last-seen (from FortiGate); null when no creds. */
  status?: DeviceStatus | null
  /** current router-attested behavior (active sessions + resolved destinations); null when no FortiGate creds. */
  live?: ProfileLive | null
  /** every (port, proto) seen in the sample — local = ports ON this host peers hit, remote = destination ports it reaches out to. */
  ports?: { local: PortUse[]; remote: PortUse[] } | null
  outbound: ProfilePeer[]
  inbound: ProfilePeer[]
  hours: number[]
  tags: string[]
  totals: { outHits: number; inHits: number; bytes: number; extPeers: number; intPeers: number }
  window?: { from: string; to: string } | null
  sampled?: boolean
}
export interface GraphPattern {
  title: string
  kind: string
  members: string[]
  why: string
  severity: 'high' | 'medium' | 'low'
  confidence?: number
}
export interface GraphAnalysis {
  cidr: string
  loading?: boolean
  error?: string
  summary?: string
  communities?: { id: string; label: string; note: string }[]
  patterns?: GraphPattern[]
  corridors?: { src: string; dst: string; why: string }[]
  flow?: string
  blindSpot?: string
  actions?: string[]
  model?: string
}

/* ── event-driven full-chain topology theater ────────────────────────────────
 * A NetOps live-feed item promoted onto page 1: the console expands the WHOLE
 * topology (every subnet, every mined device) and plays the event's pipeline
 * chain across it. `device` is the raw src_device_key from the landed record;
 * mapping onto a real topology node happens against `topology.anchors` and is
 * stated in the UI when it fails ("not on the real network"), never invented. */
export interface TheaterEvent {
  kind: 'alert' | 'suggestion' | 'browse'
  id: string
  ts: string
  device: string
  /** Localized display label; `device` remains the raw identity used for anchors. */
  deviceLabel?: string
  severity?: string
  priority?: string
  scenario?: string
  summary?: string
  scope?: string
  /** pipeline stages this event actually lit (from the NetOps stage vocabulary) */
  stageIds: string[]
}

export interface Anchor {
  ip: string
  name: string
  role: string
  intf: string
}
export interface Topology {
  core: { name: string; ip: string; model: string }
  interfaces: Interface[]
  subnets: Subnet[]
  anchors: Anchor[]
}
export interface MeshNode {
  ip: string
  out: number
  deny: number
  accept: number
  ports: string[]
  role: string
  threat: 'high' | 'watch' | 'ok'
}
/* ── memory observatory ───────────────────────────────────────────────────────
 * Item-level memory lifecycle from GET /api/rca/evolution → `observatory`.
 * Every field is serialized from the real kernel run (core/evolve/observatory.py).
 * Nothing here may be synthesized in the frontend.
 * ────────────────────────────────────────────────────────────────────────── */
export type MemTier = 'episodic' | 'semantic' | 'procedural' | 'asset_profile'

/** Ops the kernel can emit. UPDATE/NOOP/QUARANTINE are real code paths that do
 *  NOT fire on the R230 held-out set — render them only if they actually appear.
 *
 *  INSIGHT_REFRESH is distinct from REINFORCE on purpose: REINFORCE is a fixed
 *  `+=` increment from a recall hit, whereas reflection re-derives an insight's
 *  importance ABSOLUTELY from family salience and routinely overwrites a
 *  REINFORCE increment. Collapsing the two would misreport both the code path
 *  and the arithmetic. */
export type MemOp =
  | 'ADD'
  | 'UPDATE'
  | 'NOOP'
  | 'REINFORCE'
  | 'QUARANTINE'
  | 'INSIGHT'
  | 'INSIGHT_REFRESH'
  | 'LINK'
  | 'DECAY'
  | 'FORGET'

export interface MemRecord {
  memory_id: string
  tier: MemTier
  text: string
  tags: string[]
  asset_ids: string[]
  evidence_ids: string[]
  confidence: number
  importance: number
  strength: number
  quarantined: boolean
  quarantine_reason: string | null
  source_trace_ids: string[]
  links: string[]
  evidence_snapshot: { evidence_id?: string; source?: string; summary?: string }[]
}

/** Scalar+list snapshot taken either side of an in-place mutation. */
export interface MemSnapshot {
  confidence: number
  importance: number
  strength: number
  tags: string[]
  asset_ids: string[]
  links: string[]
}

export interface MemEvent {
  seq: number
  pass: number
  case_id: string
  run_id: string
  op: MemOp
  memory_id: string
  tier: MemTier
  /** Real RouteDecision.similarity. null on paths where route() never ran. */
  similarity: number | null
  target_id: string | null
  before: MemSnapshot | null
  after: MemSnapshot | null
  added_tags: string[]
  added_assets: string[]
  /** INSIGHT only: the real episodic members reflection abstracted from. */
  source_memory_ids?: string[]
}

export interface MemRecall {
  seq: number
  pass: number
  case_id: string
  run_id: string
  retrieved: Partial<Record<MemTier, string[]>>
  retrieval_candidates: {
    memory_id: string
    tier: MemTier
    lexical_score: number
    vector_score: number
    asset_hits: number
    graph_hop: number
    graph_parent_id: string | null
    structural_prior: number
    final_score: number
  }[]
  included_memory_ids: string[]
  /** Derived set-difference retrieved − included. The kernel does not record WHY. */
  dropped_memory_ids: string[]
  /** Per-memory drop records when the ContextCompiler evicts a retrieved memory
   *  from the assembled packet (budget/cap). Empty when nothing was dropped. */
  context_drops?: { memory_id: string; reason?: string; stage?: string }[]
  probes: number
  shortcut: boolean
  resolved: boolean
  resolved_memory_ids: string[]
}

/** What the kernel genuinely cannot tell us. Drives honesty labels in the UI —
 *  never render a capability that is false as though it were live. */
export interface MemCapabilities {
  decay_wired: boolean
  eviction_wired?: boolean
  conflict_update_wired?: boolean
  retrieval_scores: boolean
  context_drop_reason: boolean
  update_text_mutation: boolean
}

export interface RuntimeCapabilityStatus {
  implemented: boolean
  configured: boolean
  fired: boolean
}

export interface Observatory {
  records: MemRecord[]
  events: MemEvent[]
  recall: MemRecall[]
  capabilities: MemCapabilities
  capability_status?: Record<string, RuntimeCapabilityStatus>
}

export interface RcaSnapshot {
  readiness: Readiness
  datasetReady: boolean
  provider: string
  reasonerMode: string
  providers: Provider[]
  providerError: string | null
  topology: Topology | null
  meshes: Record<string, MeshNode[]>
  dataStats: DataStats | null
  cases: RcaCase[]
  baselines: Baseline[]
  note: string
}
