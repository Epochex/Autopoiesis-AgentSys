/* ── PAGE 4 · 混合检索 / RETRIEVAL — real worked-example cases (data contract) ──
 * GET /api/rca/retrieval → { ok, cases[] }. Each case is a REAL retrieval run you
 * can read end-to-end: the query, every retriever's real hits (real title + real
 * body text + WHY it matched + real score), the fusion/gate for the KB flow, and
 * the real passages assembled into context. Two flows really exist (intent_router):
 *   · memory_recall  (network-RCA, LIVE): signals → tiers (asset/procedural/
 *     episodic/semantic) → graph-hop → context. No RRF/CRAG/rerank.
 *   · kb_hybrid      (eval-only): BM25[+dense]+RRF+CRAG+rerank → context.
 * The page replays a case interactively; every mark maps to a payload field. The
 * fixture is an isolated dev sample (used only if the fetch fails). */

export type RtI18n = { zh: string; en: string }
export type RouteKind = 'lexical' | 'vector' | 'structured'
export type TierKind = 'asset_profile' | 'procedural' | 'episodic' | 'semantic'
export type Verdict = 'correct' | 'ambiguous' | 'incorrect'
export type FlowKind = 'memory_recall' | 'kb_hybrid'

export interface RtHit { doc_id: string; rank: number; score: number; title: string; snippet: string; text: string; matched: string[] }
export interface RtTierHit extends RtHit { via: string[] }
export interface RtRoute { id: string; label: RtI18n; kind: RouteKind; enabled: boolean; note: string | null; hits: RtHit[] }
export interface RtTier { id: string; label: RtI18n; kind: TierKind; hits: RtTierHit[] }
export interface RtGraphEdge { doc_id: string; from: string; hop: number; relation: string }
export interface RtGraph { hops: number; expanded: RtGraphEdge[] }
export interface RtFusedDoc { doc_id: string; rank: number; rrf_score: number; from_routes: string[]; title: string }
export interface RtFusion { method: 'rrf'; k: number; c: number; ranked: RtFusedDoc[] }
export interface RtGate { verdict: Verdict; reason: string; top_score: number; kept: string[]; action: RtI18n }
export interface RtRerankDoc { doc_id: string; rank: number; delta: number }
export interface RtRerank { enabled: boolean; model: string | null; note: string | null; ordered: RtRerankDoc[] }
export interface RtSelected { doc_id: string; title: string; tokens: number; reason: string; text: string }
export interface RtContext { budget_tokens: number; total_tokens: number; selected: RtSelected[] }
export interface RtDoc { title: string; text: string; tags: string[]; source: string }
export interface RtTriggers { count: number; live: boolean; source: string; note: RtI18n }
export interface RtIntent { tier: string; zh: string; en: string }

export interface Case {
  id: string
  flow: FlowKind
  label: RtI18n
  query: string
  intent: RtIntent
  corpus: { size: number; source: string }
  triggers: RtTriggers
  context: RtContext
  docs: Record<string, RtDoc>
  tiers?: RtTier[]
  graph?: RtGraph | null
  query_expansions?: string[]
  routes?: RtRoute[]
  fusion?: RtFusion
  gate?: RtGate
  rerank?: RtRerank
}
export interface RetrievalResp { ok: boolean; cases: Case[] }

/* ── replay steps per flow (the interactive timeline) ─────────────────────── */
export type Step = { id: string; zh: string; en: string }
export const STEPS_MEM: Step[] = [
  { id: 'query', zh: '查询', en: 'QUERY' },
  { id: 'tiers', zh: '分层召回', en: 'TIERED RECALL' },
  { id: 'graph', zh: '图跳扩展', en: 'GRAPH-HOP' },
  { id: 'context', zh: '组装上下文', en: 'CONTEXT' },
]
export const STEPS_KB: Step[] = [
  { id: 'query', zh: '查询 · 扩写', en: 'QUERY · EXPAND' },
  { id: 'routes', zh: '三路召回', en: 'ROUTES' },
  { id: 'fusion', zh: 'RRF 融合', en: 'RRF FUSION' },
  { id: 'gate', zh: 'CRAG 判定', en: 'CRAG GATE' },
  { id: 'rerank', zh: '重排', en: 'RERANK' },
  { id: 'context', zh: '组装上下文', en: 'CONTEXT' },
]
export const stepsOf = (c: Case): Step[] => (c.flow === 'kb_hybrid' ? STEPS_KB : STEPS_MEM)

export const KIND_LABEL: Record<RouteKind, RtI18n> = {
  lexical: { zh: '词法召回 · BM25', en: 'LEXICAL · BM25' },
  vector: { zh: '稠密向量召回', en: 'DENSE VECTOR' },
  structured: { zh: '结构化逻辑召回', en: 'STRUCTURED' },
}
export const TIER_LABEL: Record<TierKind, RtI18n> = {
  asset_profile: { zh: '资产画像', en: 'ASSET PROFILE' },
  procedural: { zh: '程序记忆', en: 'PROCEDURAL' },
  episodic: { zh: '情节记忆', en: 'EPISODIC' },
  semantic: { zh: '语义记忆', en: 'SEMANTIC' },
}
export const VIA_LABEL: Record<string, RtI18n> = {
  lexical: { zh: '词法', en: 'lexical' }, vector: { zh: '向量', en: 'vector' }, graph: { zh: '图跳', en: 'graph' },
}

/* one doc's real journey across ITS flow — for the doc detail + hover trace */
export interface JourneyBadge { k: string; v: string; on: boolean }
export function journeyOf(c: Case, id: string, zh: boolean): JourneyBadge[] {
  if (c.flow === 'kb_hybrid') {
    const fr = c.fusion?.ranked.find((r) => r.doc_id === id)
    const rr = c.rerank?.enabled ? c.rerank.ordered.find((r) => r.doc_id === id) : undefined
    const ctx = c.context.selected.find((x) => x.doc_id === id)
    return [
      ...(c.routes ?? []).map((route) => {
        const h = route.enabled ? route.hits.find((x) => x.doc_id === id) : undefined
        return { k: zh ? KIND_LABEL[route.kind].zh : KIND_LABEL[route.kind].en, v: route.enabled ? (h ? `#${h.rank}` : '—') : (zh ? '离线' : 'off'), on: !!h }
      }),
      { k: 'RRF', v: fr ? `#${fr.rank}` : '—', on: !!fr },
      { k: 'CRAG', v: c.gate?.kept.includes(id) ? (zh ? '保留' : 'KEPT') : (zh ? '弃' : 'DROP'), on: !!c.gate?.kept.includes(id) },
      { k: zh ? '重排' : 'RERANK', v: rr ? `#${rr.rank}` : '—', on: !!rr },
      { k: zh ? '上下文' : 'CONTEXT', v: ctx ? `${ctx.tokens}t` : '—', on: !!ctx },
    ]
  }
  const ctx = c.context.selected.find((x) => x.doc_id === id)
  const badges = (c.tiers ?? []).filter((t) => t.hits.some((h) => h.doc_id === id)).map((t) => {
    const h = t.hits.find((x) => x.doc_id === id)!
    return { k: zh ? TIER_LABEL[t.kind].zh : TIER_LABEL[t.kind].en, v: `#${h.rank}`, on: true }
  })
  const g = (c.graph?.expanded ?? []).find((e) => e.doc_id === id)
  if (g) badges.push({ k: zh ? '图跳' : 'GRAPH', v: `hop${g.hop}`, on: true })
  badges.push({ k: zh ? '上下文' : 'CONTEXT', v: ctx ? `${ctx.tokens}t` : '—', on: !!ctx })
  return badges
}

/* highlight matched terms inside a snippet/body for the reader */
export function markMatched(text: string, matched: string[]): { t: string; hit: boolean }[] {
  if (!matched.length) return [{ t: text, hit: false }]
  const esc = matched.map((m) => m.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).filter(Boolean)
  if (!esc.length) return [{ t: text, hit: false }]
  const re = new RegExp(`(${esc.join('|')})`, 'ig')
  return text.split(re).filter((s) => s !== '').map((s) => ({ t: s, hit: matched.some((m) => m.toLowerCase() === s.toLowerCase()) }))
}

/* ── DEV FIXTURE (isolated; used only if the fetch fails) ──────────────────── */
const D = (title: string, text: string, source: string, tags: string[] = []): RtDoc => ({ title, text, tags, source })
const MEM1_DOCS: Record<string, RtDoc> = {
  'asset-r230-profile': D('R230 资产画像', 'Dell R230 (192.168.1.23) · Ubuntu 22.04 · 双网口 eno1/eno2 · showroom↔office 网关角色 · 上联 FortiGate。', 'memory/asset', ['asset']),
  'procedural-carrier-first': D('程序 · unreachable 先判链路载波', '遇到 unreachable / no carrier,先确认物理链路与网卡载波(ethtool link detected),再看路由与策略,最后才怀疑硬件。', 'memory/procedural', ['procedural']),
  'procedural-route-check': D('程序 · 跨网段路由核查', 'showroom(192.168.1.0/24)→office(192.168.16.0/20) 不可达时,核查 FortiGate 上两段间的路由与安全策略是否放行。', 'memory/procedural', ['procedural']),
  'semantic-local-topology': D('语义 · 本地拓扑与网段', 'R230 为两网段之间的转发节点;网段隔离或策略拒绝都会表现为跨段不可达。', 'memory/semantic', ['semantic']),
}
const KB_DOCS: Record<string, RtDoc> = {
  d1: D('管理口锁定策略', 'Admin interface lockout enforces after 5 consecutive failed logins for 60s.', 'runbook', ['policy']),
  d2: D('登录失败阈值与锁定配置', 'lockout-threshold 5; lockout-duration 60; applies to admin sessions.', 'config', ['config']),
  d3: D('R230 syslog · 反复管理口认证失败', 'admin login failed × 27 from 192.168.1.44 within 3m; account locked.', 'R230 syslog', ['evidence']),
  d7: D('FortiGate 锁定时长默认值', 'default admin-lockout-duration is 60 seconds unless overridden.', 'vendor-doc', ['reference']),
  d8: D('历史事件 INC-2041 · 管理口锁定', 'Prior lockout traced to a mistyped automation credential retrying every few seconds.', 'incident-log', ['incident']),
}
const mh = (doc_id: string, rank: number, score: number, via: string[], matched: string[], docs: Record<string, RtDoc>): RtTierHit =>
  ({ doc_id, rank, score, via, matched, title: docs[doc_id].title, snippet: docs[doc_id].text.slice(0, 80), text: docs[doc_id].text })

export const RETRIEVAL_FIXTURE: RetrievalResp = {
  ok: true,
  cases: [
    {
      id: 'case_showroom_unreachable', flow: 'memory_recall',
      label: { zh: 'showroom→office 不可达', en: 'showroom→office unreachable' },
      query: 'Traffic from 192.168.1.0/24 showroom to 192.168.16.0/20 office is unreachable.',
      intent: { tier: 'library_recall', zh: '库召回 · 命中既有资产画像与程序', en: 'library recall · hits asset profile + procedures' },
      corpus: { size: 5, source: 'R230 运行记忆(seed)' },
      triggers: { count: 5, live: true, source: 'artifacts/network_rca_phase1_trace.jsonl · memory_read', note: { zh: '一期落盘 trace 中 5 个真实网络 RCA 案例各触发一次', en: '5 real network-RCA cases in the one persisted phase-1 trace' } },
      tiers: [
        { id: 'asset_profile', kind: 'asset_profile', label: TIER_LABEL.asset_profile, hits: [mh('asset-r230-profile', 1, 12.1, ['lexical', 'graph'], ['192.168.1', 'showroom', 'office', 'r230'], MEM1_DOCS)] },
        { id: 'procedural', kind: 'procedural', label: TIER_LABEL.procedural, hits: [
          mh('procedural-route-check', 1, 9.8, ['lexical'], ['route', 'unreachable', 'office'], MEM1_DOCS),
          mh('procedural-carrier-first', 2, 4.1, ['lexical'], ['unreachable'], MEM1_DOCS),
        ] },
        { id: 'episodic', kind: 'episodic', label: TIER_LABEL.episodic, hits: [] },
        { id: 'semantic', kind: 'semantic', label: TIER_LABEL.semantic, hits: [mh('semantic-local-topology', 1, 6.2, ['graph'], [], MEM1_DOCS)] },
      ],
      graph: { hops: 2, expanded: [{ doc_id: 'semantic-local-topology', from: 'asset-r230-profile', hop: 1, relation: 'forwards-between' }] },
      context: { budget_tokens: 512, total_tokens: 300, selected: [
        { doc_id: 'asset-r230-profile', title: 'R230 资产画像', tokens: 120, reason: '锚定资产与转发角色', text: MEM1_DOCS['asset-r230-profile'].text },
        { doc_id: 'procedural-route-check', title: '程序 · 跨网段路由核查', tokens: 100, reason: '直接对应跨段不可达', text: MEM1_DOCS['procedural-route-check'].text },
        { doc_id: 'semantic-local-topology', title: '语义 · 本地拓扑与网段', tokens: 80, reason: '解释不可达机理', text: MEM1_DOCS['semantic-local-topology'].text },
      ] },
      docs: MEM1_DOCS,
    },
    {
      id: 'case_eno1_no_carrier', flow: 'memory_recall',
      label: { zh: 'eno1 无载波', en: 'eno1 no carrier' },
      query: 'Dell R230 eno1 reports no carrier; determine whether this is a NIC fault.',
      intent: { tier: 'library_recall', zh: '库召回 · 命中资产画像与排障程序', en: 'library recall · asset profile + troubleshooting procedure' },
      corpus: { size: 5, source: 'R230 运行记忆(seed)' },
      triggers: { count: 5, live: true, source: 'artifacts/network_rca_phase1_trace.jsonl · memory_read', note: { zh: '与上一案例同属一期落盘的 5 个真实案例', en: 'same persisted phase-1 batch of 5 real cases' } },
      tiers: [
        { id: 'asset_profile', kind: 'asset_profile', label: TIER_LABEL.asset_profile, hits: [mh('asset-r230-profile', 1, 14.2, ['lexical'], ['r230', 'eno1'], MEM1_DOCS)] },
        { id: 'procedural', kind: 'procedural', label: TIER_LABEL.procedural, hits: [mh('procedural-carrier-first', 1, 9.8, ['lexical'], ['carrier'], MEM1_DOCS)] },
        { id: 'episodic', kind: 'episodic', label: TIER_LABEL.episodic, hits: [] },
        { id: 'semantic', kind: 'semantic', label: TIER_LABEL.semantic, hits: [] },
      ],
      graph: { hops: 2, expanded: [] },
      context: { budget_tokens: 512, total_tokens: 199, selected: [
        { doc_id: 'asset-r230-profile', title: 'R230 资产画像', tokens: 120, reason: '锚定网卡与角色', text: MEM1_DOCS['asset-r230-profile'].text },
        { doc_id: 'procedural-carrier-first', title: '程序 · 先判链路载波', tokens: 79, reason: '排障首选程序', text: MEM1_DOCS['procedural-carrier-first'].text },
      ] },
      docs: MEM1_DOCS,
    },
    {
      id: 'kb_hybrid', flow: 'kb_hybrid',
      label: { zh: '管理口锁定 · KB', en: 'admin lockout · KB' },
      query: 'administrator login lockout after repeated failed login attempts',
      intent: { tier: 'library_recall', zh: '库召回 · 语料/知识库问答', en: 'library recall · corpus/KB question' },
      corpus: { size: 15, source: 'FortiOS 运维知识(demo 语料)' },
      triggers: { count: 0, live: false, source: '无生产 trace · eval-only', note: { zh: '仅 eval 台架(LongMemEval)与单测调用,旗舰场景不走此流', en: 'eval-only (LongMemEval + unit tests); live scenario does not use it' } },
      query_expansions: ['admin login failure lockout', 'lockout threshold duration', 'repeated failed admin auth'],
      routes: [
        { id: 'bm25', kind: 'lexical', label: KIND_LABEL.lexical, enabled: true, note: null, hits: [
          { doc_id: 'd3', rank: 1, score: 17.8, matched: ['login', 'failed', 'lockout'], title: KB_DOCS.d3.title, snippet: KB_DOCS.d3.text, text: KB_DOCS.d3.text },
          { doc_id: 'd2', rank: 2, score: 15.1, matched: ['lockout', 'failed'], title: KB_DOCS.d2.title, snippet: KB_DOCS.d2.text, text: KB_DOCS.d2.text },
          { doc_id: 'd7', rank: 3, score: 12.7, matched: ['lockout'], title: KB_DOCS.d7.title, snippet: KB_DOCS.d7.text, text: KB_DOCS.d7.text },
          { doc_id: 'd1', rank: 4, score: 9.9, matched: ['lockout', 'login', 'failed'], title: KB_DOCS.d1.title, snippet: KB_DOCS.d1.text, text: KB_DOCS.d1.text },
        ] },
        { id: 'dense', kind: 'vector', label: KIND_LABEL.vector, enabled: false, note: 'dense index requires the `dense` extra (torch missing)', hits: [] },
        { id: 'logical', kind: 'structured', label: KIND_LABEL.structured, enabled: true, note: null, hits: [
          { doc_id: 'd1', rank: 1, score: 1.0, matched: ['admin_iface', 'lockout'], title: KB_DOCS.d1.title, snippet: KB_DOCS.d1.text, text: KB_DOCS.d1.text },
          { doc_id: 'd2', rank: 2, score: 1.0, matched: ['lockout-threshold'], title: KB_DOCS.d2.title, snippet: KB_DOCS.d2.text, text: KB_DOCS.d2.text },
        ] },
      ],
      fusion: { method: 'rrf', k: 60, c: 60, ranked: [
        { doc_id: 'd1', rank: 1, rrf_score: 0.0323, from_routes: ['bm25', 'logical'], title: KB_DOCS.d1.title },
        { doc_id: 'd3', rank: 2, rrf_score: 0.0164, from_routes: ['bm25'], title: KB_DOCS.d3.title },
        { doc_id: 'd2', rank: 3, rrf_score: 0.0323, from_routes: ['bm25', 'logical'], title: KB_DOCS.d2.title },
        { doc_id: 'd7', rank: 4, rrf_score: 0.0159, from_routes: ['bm25'], title: KB_DOCS.d7.title },
      ] },
      gate: { verdict: 'correct', reason: '融合首位由 BM25 与结构化两路共同支持,置信足够,直接采用,不触发扩池。', top_score: 17.8, kept: ['d1', 'd3', 'd2', 'd7'], action: { zh: '判定 CORRECT · 保留 4 篇进入重排', en: 'CORRECT · keep 4 for rerank' } },
      rerank: { enabled: false, model: null, note: 'cross-encoder reranker requires the `rerank` extra (torch missing)', ordered: [] },
      context: { budget_tokens: 240, total_tokens: 219, selected: [
        { doc_id: 'd1', title: KB_DOCS.d1.title, tokens: 62, reason: '权威策略定义', text: KB_DOCS.d1.text },
        { doc_id: 'd3', title: KB_DOCS.d3.title, tokens: 75, reason: '原始证据 · 直接命中', text: KB_DOCS.d3.text },
        { doc_id: 'd2', title: KB_DOCS.d2.title, tokens: 51, reason: '阈值参数', text: KB_DOCS.d2.text },
        { doc_id: 'd7', title: KB_DOCS.d7.title, tokens: 31, reason: '默认值佐证', text: KB_DOCS.d7.text },
      ] },
      docs: KB_DOCS,
    },
  ],
}

/* The gateway can be intentionally absent in a standalone frontend review. Keep
 * the fallback useful in both languages: retrieval scores and topology stay
 * identical, while the authored fixture prose is projected to English here. */
const FIXTURE_EN: Record<string, string> = {
  'Dell R230 (192.168.1.23) · Ubuntu 22.04 · 双网口 eno1/eno2 · showroom↔office 网关角色 · 上联 FortiGate。':
    'Dell R230 (192.168.1.23) · Ubuntu 22.04 · dual NICs eno1/eno2 · showroom↔office gateway role · uplink to FortiGate.',
  'FortiGate 锁定时长默认值': 'FortiGate default lockout duration',
  'FortiOS 运维知识(demo 语料)': 'FortiOS operations knowledge (demo corpus)',
  'R230 syslog · 反复管理口认证失败': 'R230 syslog · repeated management-login failures',
  'R230 为两网段之间的转发节点;网段隔离或策略拒绝都会表现为跨段不可达。':
    'R230 forwards traffic between the two networks; network isolation or a policy denial can both appear as cross-network unreachability.',
  'R230 资产画像': 'R230 asset profile',
  'R230 运行记忆(seed)': 'R230 operational memory (seed)',
  'eno1 无载波': 'eno1 no carrier',
  'showroom(192.168.1.0/24)→office(192.168.16.0/20) 不可达时,核查 FortiGate 上两段间的路由与安全策略是否放行。':
    'When showroom (192.168.1.0/24) cannot reach office (192.168.16.0/20), verify the inter-network route and the allowing security policy on FortiGate.',
  'showroom→office 不可达': 'showroom→office unreachable',
  '一期落盘 trace 中 5 个真实网络 RCA 案例各触发一次':
    'Each of the 5 real network-RCA cases in the persisted phase-1 trace triggered once',
  '与上一案例同属一期落盘的 5 个真实案例': 'Same persisted phase-1 batch of 5 real cases',
  '仅 eval 台架(LongMemEval)与单测调用,旗舰场景不走此流':
    'Used only by the LongMemEval bench and unit tests; the flagship scenario does not use this flow',
  '判定 CORRECT · 保留 4 篇进入重排': 'CORRECT · keep 4 documents for reranking',
  '历史事件 INC-2041 · 管理口锁定': 'Historical incident INC-2041 · management lockout',
  '原始证据 · 直接命中': 'Raw evidence · direct match',
  '库召回 · 命中既有资产画像与程序': 'Library recall · existing asset profile + procedures',
  '库召回 · 命中资产画像与排障程序': 'Library recall · asset profile + troubleshooting procedure',
  '库召回 · 语料/知识库问答': 'Library recall · corpus/KB question',
  '排障首选程序': 'Primary troubleshooting procedure',
  '无生产 trace · eval-only': 'No production trace · eval-only',
  '权威策略定义': 'Authoritative policy definition',
  '登录失败阈值与锁定配置': 'Failed-login threshold and lockout configuration',
  '直接对应跨段不可达': 'Directly addresses cross-network unreachability',
  '程序 · unreachable 先判链路载波': 'Procedure · check link carrier first for unreachable',
  '程序 · 先判链路载波': 'Procedure · check link carrier first',
  '程序 · 跨网段路由核查': 'Procedure · cross-network route checks',
  '管理口锁定 · KB': 'Management lockout · KB',
  '管理口锁定策略': 'Management-interface lockout policy',
  '融合首位由 BM25 与结构化两路共同支持,置信足够,直接采用,不触发扩池。':
    'The top fused result is supported by both BM25 and the structured route; confidence is sufficient, so the result is accepted without widening the pool.',
  '解释不可达机理': 'Explains the unreachability mechanism',
  '语义 · 本地拓扑与网段': 'Semantic · local topology and networks',
  '遇到 unreachable / no carrier,先确认物理链路与网卡载波(ethtool link detected),再看路由与策略,最后才怀疑硬件。':
    'For unreachable / no carrier, first verify the physical link and NIC carrier (ethtool link detected), then inspect routes and policies, and only then investigate hardware.',
  '锚定网卡与角色': 'Anchors the NIC and device role',
  '锚定资产与转发角色': 'Anchors the asset and forwarding role',
  '阈值参数': 'Threshold parameters',
  '默认值佐证': 'Default-value corroboration',
}

const fixtureValueEn = (value: unknown): unknown => {
  if (typeof value === 'string') return FIXTURE_EN[value] ?? value
  if (Array.isArray(value)) return value.map(fixtureValueEn)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, fixtureValueEn(v)]))
  }
  return value
}

const RETRIEVAL_FIXTURE_EN = fixtureValueEn(RETRIEVAL_FIXTURE) as RetrievalResp
export const retrievalFixture = (zh: boolean): RetrievalResp => (zh ? RETRIEVAL_FIXTURE : RETRIEVAL_FIXTURE_EN)
