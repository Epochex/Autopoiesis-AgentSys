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
 *  NOT fire on the R230 held-out set — render them only if they actually appear. */
export type MemOp = 'ADD' | 'UPDATE' | 'NOOP' | 'REINFORCE' | 'QUARANTINE' | 'INSIGHT' | 'LINK'

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
  included_memory_ids: string[]
  /** Derived set-difference retrieved − included. The kernel does not record WHY. */
  dropped_memory_ids: string[]
  probes: number
  shortcut: boolean
  resolved: boolean
  resolved_memory_ids: string[]
}

/** What the kernel genuinely cannot tell us. Drives honesty labels in the UI —
 *  never render a capability that is false as though it were live. */
export interface MemCapabilities {
  decay_wired: boolean
  retrieval_scores: boolean
  context_drop_reason: boolean
  update_text_mutation: boolean
}

export interface Observatory {
  records: MemRecord[]
  events: MemEvent[]
  recall: MemRecall[]
  capabilities: MemCapabilities
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
