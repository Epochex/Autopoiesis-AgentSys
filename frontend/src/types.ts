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

/* ── /api/rca/evolution ────────────────────────────────────────────────────
 * Cold-vs-warm recurrence result: one self replayed over a recurring real
 * incident stream. Consumed by TrajectoryPage, which reads `ready` and hands
 * `observatory` to the memory observatory. */
export interface EvoByPass {
  pass: number
  probes: number
  recalled: number
  memory_end: number
  accuracy: number
}

export interface MemHealth {
  active: number
  forgotten: number
  insights: number
  links: number
  by_tier?: Record<string, number>
}

export interface EvoData {
  ready: boolean
  nCases: number
  passes: number
  delta: { probes_cold: number; probes_warm: number; probes_saved_pct: number; memory_grown: number; accuracy_warm: number }
  warm: { by_pass: EvoByPass[] }
  cold: { by_pass: EvoByPass[] }
  memory?: MemHealth
  observatory?: Observatory
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

/* ── bounded historical incidents ──────────────────────────────────────────
 * These records come from /api/rca/incidents. They deliberately do not share
 * the RcaCase shape: a benchmark diagnosis and a documented operational
 * incident have different clocks, provenance, disposition and readback rules.
 * The API currently carries historical/manual/observed/readback evidence plus
 * an isolated simulated detector replay. The frontend must keep those sources
 * visibly separate and must never promote replay signals into observations. */
export type IncidentSourceKind = 'observed' | 'manual' | 'readback' | 'simulated'

export interface IncidentAsset {
  ip: string
  name: string
}

export interface IncidentReadbackCheck {
  check: string
  passed: boolean
  observed: string
  evidence_id: string | null
}

export interface IncidentReadback {
  state: string
  source_kind: IncidentSourceKind | null
  collected_at: string | null
  checks: IncidentReadbackCheck[]
}

export interface IncidentDisposition {
  status: string
  operator_note: string | null
  updated_at: string | null
  readback: IncidentReadback
}

export interface IncidentEvidence {
  sequence: number
  occurred_at: string | null
  source_kind: IncidentSourceKind
  source_type?: string
  evidence_locator?: string
  evidence_id: string
  phase: string
  summary: string
  facts: Record<string, unknown>
}

export interface IncidentSummary {
  id: string
  title: string
  incident_type: string
  severity: string
  asset: IncidentAsset
  classification: 'historical_real_incident'
  summary: string
  root_cause?: string
  impact?: string
  root_cause_evidence_ids?: string[]
  captured_at?: string | null
  window?: { start: string | null; end: string | null; basis: string }
  disposition: IncidentDisposition
  current_online_observation: false
  evidence_count: number
}

export interface IncidentMetricPoint {
  capturedAt: string | null
  value: unknown
  evidenceIds: string[]
}

export interface IncidentMetric {
  id: string
  label: string
  unit: string
  points: IncidentMetricPoint[]
}

export interface IncidentTimelineItem {
  id: string
  sequence: number
  capturedAt: string | null
  phase: string
  summary: string
  evidenceIds: string[]
  sourceKind: IncidentSourceKind
}

export interface IncidentEvidenceRef {
  id: string
  sourceKind: IncidentSourceKind
  sourceType: string
  locator: string
  capturedAt: string | null
  summary: string
}

export interface IncidentTopologyNode {
  id: string
  kind: string
  label: string
  evidenceIds: string[]
  sourceKind: IncidentSourceKind
}

export interface IncidentTopologyEdge {
  source: string
  target: string
  kind: string
  evidenceIds: string[]
  sourceKind: IncidentSourceKind
}

export interface IncidentResponseStep {
  id: string
  status: string
  evidenceIds: string[]
}

export interface IncidentResponseReadback {
  id: string
  passed: boolean
  observed: string
  evidenceIds: string[]
  missingReason: string | null
}

export interface IncidentDetail extends Omit<IncidentSummary, 'evidence_count'> {
  evidence_timeline: IncidentEvidence[]
  detector: Record<string, unknown>
  replay_signals: Record<string, unknown>[]
  detection: Record<string, unknown>
  capturedAt: string | null
  rootCause: string
  rootCauseEvidenceIds: string[]
  metrics: IncidentMetric[]
  timeline: IncidentTimelineItem[]
  evidence: IncidentEvidenceRef[]
  topology: { nodes: IncidentTopologyNode[]; edges: IncidentTopologyEdge[] }
  response: { approvalRequired: boolean; steps: IncidentResponseStep[]; readbacks: IncidentResponseReadback[] }
}

/* ---------------------------------------------------------------- *
 * Environment perception (/api/rca/environment)
 * ---------------------------------------------------------------- */

export type EnvSeverity = 'critical' | 'high' | 'medium' | 'low'
export type EnvCoverageState = 'covered' | 'partial' | 'blind'
export type EnvCellState = 'leased' | 'unbound' | 'contested' | 'silent'

export interface EnvVerification {
  state: 'confirmed' | 'unverifiable' | 'resolved'
  source: string
  note: string
  note_zh: string
  checked_at: string
}

export interface EnvPlaybookStep {
  risk: 'readonly' | 'gated'
  what: string
  what_zh: string
  command: string
}

export interface EnvPlaybook {
  verify: EnvPlaybookStep[]
  fix: EnvPlaybookStep[]
  note: string
}

export interface EnvSource {
  id: string
  label: string
  kind: 'live' | 'historical'
  window_start: string | null
  window_end: string | null
  age_seconds: number | null
  flowing: boolean
  volume: number
  unit: string
  note: string
}

export interface EnvFinding {
  finding_id: string
  detector: string
  fault_class: string
  subject: string
  subject_kind: string
  segment: string | null
  severity: EnvSeverity
  confidence: number
  headline: string
  measured: Record<string, unknown>
  evidence: Record<string, unknown>
  explains: string[]
  cannot_prove: string[]
  cannot_prove_zh: string[]
  next_probe: string
  source_kinds: string[]
  current_online_observation: boolean
  verification: EnvVerification
  playbook: EnvPlaybook
}

export interface EnvCoverageRow {
  fault_class: string
  label: string
  coverage: EnvCoverageState
  requires: string[]
  present: string[]
  missing: string[]
  closes_with: string | null
  closes_with_zh: string | null
}

export interface EnvCell {
  ip: string
  host: number
  state: EnvCellState
  mac: string | null
  hostname: string | null
  flow_events: number
  clash_events: number
}

export interface EnvSegment {
  segment: string
  interface: string | null
  cells: EnvCell[]
  counts: Partial<Record<EnvCellState, number>>
}

export interface EnvironmentReport {
  schema_version: number
  generated_from: string
  checked_at: string
  current_online_observation: boolean
  corpus: { files: string[]; lines_read: number; window_start: string | null; window_end: string | null }
  sources: EnvSource[]
  sensors: {
    present: string[]
    l2_identity_records: number
    l2_identity_env_var: string
    l2_identity_captured_at: string | null
    l2_identity_age_seconds: number | null
    l2_identity_stale: boolean | null
    l2_history_captures: number
    l2_history_env_var: string
    l2_history_window: [string, string] | null
  }
  totals: {
    findings: number
    by_severity: Partial<Record<EnvSeverity, number>>
    by_fault_class: Record<string, number>
    by_verification: Record<string, number>
    resolved_dropped: number
    blind_classes: number
    leased_addresses: number
    served_segments: string[]
  }
  findings: EnvFinding[]
  resolved: EnvFinding[]
  coverage: EnvCoverageRow[]
  address_space: EnvSegment[]
}

export interface OwnershipProbeResult {
  ok: true
  ip: string
  samples: number
  ports: Record<string, string>
  owner_samples: Record<string, number>
  service_profiles: Record<string, string[]>
  verdict: 'contested' | 'inconclusive'
  verdict_note: string
  impact: Record<string, { service: string; served_by: string[]; unreachable_share: number }>
  read_only: true
}
