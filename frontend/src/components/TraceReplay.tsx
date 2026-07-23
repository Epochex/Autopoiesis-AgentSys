/* ── EXECUTION REPLAY · per-node observable / replayable / diagnosable ──────────
   Codex's observability ledger records the run as real spans carrying their own
   input and output. This module replays that ledger node by node — but a span is
   NOT rendered as a key/value dump: every node type gets a purpose-built view of
   what it actually did. Retrieval shows query terms landing in memory tiers,
   compile shows the budget packing, reason shows evidence converging on a root
   cause, verify shows the citation check, consolidate shows what was written back.
   The rail is the true wall-clock waterfall and flags the bottleneck. */
import { useCallback, useEffect, useMemo, useState } from 'react'

type IO = Record<string, unknown>
type Node = {
  span_id: string; parent_span_id: string | null; node_name: string; node_type: string
  status: string; started_at: string; duration_ms: number; input: IO; output: IO
}
type TraceSummary = { trace_id: string; session_id: string | null; case_id: string; status: string; duration_ms: number; node_count: number; bottleneck: { span_id: string } | null }
type ObsMeta = { events_written?: number; exporters?: number; export_thread_alive?: boolean }
/* the actual CONTENT behind the span ids — joined from the case + observatory so a
   node shows the evidence text, the memory text and the diagnosis reasoning, not
   just identifiers. */
export type Content = {
  evi: Record<string, { summary: string; source: string }>
  mem: Record<string, string>
  rootCause: string
  actions: string[]
}
const EMPTY_CONTENT: Content = { evi: {}, mem: {}, rootCause: '', actions: [] }

const TYPE: Record<string, [string, string]> = {
  workflow: ['整轮运行', 'RUN'], retrieval: ['记忆召回', 'RECALL'], analysis: ['关系分析', 'ANALYZE'],
  agent: ['技能筛选', 'SKILLS'], tool: ['只读探针', 'PROBE'], context: ['上下文编译', 'COMPILE'],
  reasoner: ['根因推理', 'REASON'], verifier: ['引用核验', 'VERIFY'], memory_write: ['记忆固化', 'CONSOLIDATE'], index: ['索引维护', 'INDEX'],
}
const typeName = (t: string, zh: boolean) => TYPE[t]?.[zh ? 0 : 1] ?? t
const TIER: Record<string, [string, string]> = {
  episodic: ['情景', 'EPISODIC'], semantic: ['语义', 'SEMANTIC'], procedural: ['程序', 'PROCEDURAL'], asset_profile: ['资产', 'ASSET'],
}
const arr = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : [])
const numOf = (v: unknown, d = 0) => (typeof v === 'number' ? v : d)
const shortEv = (s: string) => s.replace(/^ev-/, '')
const shortMem = (s: string) => s.replace(/^(procedural|semantic|episodic|asset_profile)-/, '')

/* ── per-node-type views — each says what that node actually did ── */

/* an evidence row that shows the real observed line + where it came from */
function EviLine({ id, c }: { id: string; c: Content }) {
  const e = c.evi[id]
  return (
    <div className="tv-evline">
      <span className="tv-evid">{shortEv(id)}</span>
      <span className="tv-evtext">{e?.summary ?? '—'}</span>
      {e?.source ? <span className="tv-evsrc">{e.source}</span> : null}
    </div>
  )
}
/* a memory row that shows what the record actually says */
function MemLine({ id, c }: { id: string; c: Content }) {
  return (
    <div className="tv-memline">
      <span className="tv-memid">{shortMem(id)}</span>
      <span className="tv-memtext">{c.mem[id] ?? ''}</span>
    </div>
  )
}

function Chips({ items, kind, empty }: { items: string[]; kind?: string; empty?: string }) {
  if (!items.length) return <span className="tr-empty">{empty ?? '—'}</span>
  return <span className="tr-chips">{items.map((s, i) => <span key={i} className={`tr-chip ${kind ?? ''}`}>{s}</span>)}</span>
}

function RunViz({ n, zh, c }: { n: Node; zh: boolean; c: Content }) {
  const root = String(n.output.root_cause_key ?? '—')
  const verified = Boolean(n.output.verified), committed = Boolean(n.output.memory_committed)
  return (
    <div className="tv tv-run">
      <div className="tv-side">
        <span className="tv-lab">{zh ? '收到' : 'INTAKE'}</span>
        <p className="tv-query">{String(n.input.query ?? '')}</p>
        <Chips items={arr(n.input.assets)} kind="asset" />
      </div>
      <div className="tv-flow" />
      <div className="tv-side">
        <span className="tv-lab">{zh ? '本轮产出' : 'OUTCOME'}</span>
        <div className="tv-root">{root}</div>
        {c.rootCause ? <p className="tv-why">{c.rootCause}</p> : null}
        <div className="tv-badges">
          <span className={`tv-badge ${verified ? 'ok' : 'no'}`}>{verified ? '✓' : '✕'} {zh ? '已核验' : 'VERIFIED'}</span>
          <span className={`tv-badge ${committed ? 'ok' : 'no'}`}>{committed ? '✓' : '✕'} {zh ? '记忆已固化' : 'COMMITTED'}</span>
        </div>
        {c.actions.length ? (
          <div className="tv-fix">
            <span className="tv-lab2">{zh ? '整改指令 · 需人工审批' : 'REMEDIATION · GATED'}</span>
            {c.actions.map((a, i) => <div key={i} className="tv-fixstep"><b>{i + 1}</b><p>{a}</p></div>)}
          </div>
        ) : null}
      </div>
    </div>
  )
}

function RetrieveViz({ n, zh, c }: { n: Node; zh: boolean; c: Content }) {
  const tiers = (n.output.memory_ids_by_tier ?? {}) as Record<string, string[]>
  const order = ['episodic', 'semantic', 'procedural', 'asset_profile']
  return (
    <div className="tv tv-recall">
      <div className="tv-side">
        <span className="tv-lab">{zh ? '用什么去查' : 'QUERY'}</span>
        <Chips items={arr(n.input.query_terms)} kind="term" />
        <span className="tv-lab2">{zh ? '范围资产' : 'ASSETS'}</span>
        <Chips items={arr(n.input.assets)} kind="asset" />
      </div>
      <div className="tv-flow fan" />
      <div className="tv-tiers">
        <span className="tv-lab">{zh ? '各层召回到什么' : 'HITS BY TIER'}</span>
        <div className="tv-tier-grid">
          {order.map((t) => {
            const ids = arr(tiers[t])
            return (
              <div key={t} className={`tv-tier ${ids.length ? 'hit' : 'miss'}`}>
                <span className="tv-tier-h">{TIER[t]?.[zh ? 0 : 1] ?? t}<b>{ids.length}</b></span>
                {ids.length ? ids.map((i) => <MemLine key={i} id={i} c={c} />) : <span className="tv-tier-none">{zh ? '未命中' : 'none'}</span>}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function SkillsViz({ n, zh, c }: { n: Node; zh: boolean; c: Content }) {
  return (
    <div className="tv tv-skills">
      <div className="tv-side">
        <span className="tv-lab">{zh ? '选中的只读技能' : 'SKILLS CHOSEN'}</span>
        <Chips items={arr(n.input.preferred_skills)} kind="skill" />
      </div>
      <div className="tv-flow" />
      <div className="tv-side">
        <span className="tv-lab">{zh ? '产出证据' : 'EVIDENCE PRODUCED'}</span>
        {arr(n.output.evidence_ids).map((e) => <EviLine key={e} id={e} c={c} />)}
      </div>
    </div>
  )
}

function ToolViz({ n, zh, c }: { n: Node; zh: boolean; c: Content }) {
  return (
    <div className="tv tv-tool">
      <div className="tv-side">
        <span className="tv-lab">{zh ? '探测目标' : 'TARGET'}</span>
        <Chips items={arr(n.input.assets)} kind="asset" />
        <span className="tv-ro">{n.output.readonly === false ? (zh ? '写操作' : 'WRITE') : (zh ? '只读 · 不改变系统' : 'READ-ONLY')}</span>
      </div>
      <div className="tv-flow" />
      <div className="tv-side">
        <span className="tv-lab">{zh ? '取回证据 · 只读回读结果' : 'EVIDENCE READ BACK'}</span>
        {arr(n.output.evidence_ids).map((e) => <EviLine key={e} id={e} c={c} />)}
      </div>
    </div>
  )
}

function ContextViz({ n, zh, c }: { n: Node; zh: boolean; c: Content }) {
  const mem = arr(n.output.included_memory_ids), ev = arr(n.output.included_evidence_ids)
  const missing = arr(n.output.missing_evidence)
  const total = mem.length + ev.length
  return (
    <div className="tv tv-ctx">
      <div className="tv-pack">
        <span className="tv-lab">{zh ? '装进上下文包' : 'PACKED INTO CONTEXT'}</span>
        <div className="tv-pack-list">
          {mem.map((m) => <MemLine key={m} id={m} c={c} />)}
          {ev.map((e) => <EviLine key={e} id={e} c={c} />)}
          {!total ? <span className="tv-empty">—</span> : null}
        </div>
        <div className="tv-pack-foot">
          <span><b>{mem.length}</b>{zh ? '条记忆' : 'memories'}</span>
          <span><b>{ev.length}</b>{zh ? '条证据' : 'evidence'}</span>
          <span className={missing.length ? 'bad' : ''}><b>{missing.length}</b>{zh ? '条缺失' : 'missing'}</span>
          <span className="tv-in">{zh ? '入口' : 'IN'} {numOf(n.input.retrieved_memories)}{zh ? '记忆' : 'mem'} · {numOf(n.input.current_evidence)}{zh ? '证据' : 'ev'}</span>
        </div>
      </div>
    </div>
  )
}

function ReasonViz({ n, zh, c }: { n: Node; zh: boolean; c: Content }) {
  const ev = arr(n.output.evidence_ids), missing = arr(n.output.missing_evidence)
  const root = String(n.output.root_cause_key ?? '—')
  return (
    <div className="tv tv-reason">
      <div className="tv-conv">
        <span className="tv-lab">{zh ? '据以推理的证据' : 'EVIDENCE IN'}</span>
        <div className="tv-conv-list">{ev.map((e) => <EviLine key={e} id={e} c={c} />)}</div>
        <span className="tv-ctxtok">{zh ? '上下文' : 'CONTEXT'} <b>{numOf(n.input.context_tokens)}</b> tok</span>
      </div>
      <div className="tv-converge" />
      <div className="tv-verdict">
        <span className="tv-lab">{zh ? '推出根因' : 'ROOT CAUSE'}</span>
        <div className="tv-root big">{root}</div>
        {c.rootCause ? <p className="tv-why">{c.rootCause}</p> : null}
        {missing.length ? <span className="tv-missing">{zh ? '缺失证据' : 'missing'} {missing.length}</span> : <span className="tv-nomiss">{zh ? '无缺失证据' : 'no gaps'}</span>}
      </div>
    </div>
  )
}

function VerifyViz({ n, zh, c }: { n: Node; zh: boolean; c: Content }) {
  const ev = arr(n.input.evidence_ids), errors = arr(n.output.errors)
  const passed = Boolean(n.output.passed)
  return (
    <div className="tv tv-verify">
      <div className="tv-side wide">
        <span className="tv-lab">{zh ? '逐条核对引用' : 'CITATION CHECK'}</span>
        {ev.map((e) => <div key={e} className="tv-check"><span className="tv-tick">✓</span><EviLine id={e} c={c} /></div>)}
        {errors.map((e, i) => <div key={i} className="tv-check bad"><span className="tv-tick">✕</span>{e}</div>)}
      </div>
      <div className={`tv-stamp ${passed ? 'ok' : 'no'}`}>{passed ? (zh ? '通过' : 'PASSED') : (zh ? '未通过' : 'FAILED')}</div>
    </div>
  )
}

function ConsolidateViz({ n, zh }: { n: Node; zh: boolean }) {
  const ops: [string, string, string][] = [
    ['added', '新增', 'ADDED'], ['reinforced', '强化', 'REINFORCED'], ['updated', '更新', 'UPDATED'],
    ['linked', '关联', 'LINKED'], ['insights', '洞见', 'INSIGHTS'], ['superseded', '版本替换', 'SUPERSEDED'], ['quarantined', '隔离', 'QUARANTINED'],
  ]
  return (
    <div className="tv tv-consol">
      <span className="tv-lab">{zh ? '这一轮往记忆里写了什么' : 'WHAT THIS RUN WROTE BACK'}</span>
      <div className="tv-ops">
        {ops.map(([k, z, e]) => {
          const v = arr(n.output[k]); const c = v.length
          return (
            <div key={k} className={`tv-op ${c ? 'on' : ''}`}>
              <b>{c}</b><span>{zh ? z : e}</span>
              {c ? <em>{v.map(shortMem).join(', ')}</em> : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function AnalyzeViz({ n, zh }: { n: Node; zh: boolean }) {
  const rel = arr(n.output.relation_types), chain = arr(n.output.chain)
  const hidden = n.output.hidden_failure_pattern
  return (
    <div className="tv tv-analyze">
      <div className="tv-side">
        <span className="tv-lab">{zh ? '关系类型' : 'RELATION TYPES'}</span>
        <Chips items={rel} kind="rel" empty={zh ? '本轮无类型化关系' : 'none'} />
        <span className="tv-lab2">{zh ? '传播链' : 'CHAIN'}</span>
        <Chips items={chain.map(shortMem)} empty={zh ? '无链' : 'none'} />
      </div>
      <div className="tv-side">
        <span className="tv-lab">{zh ? '隐性故障模式' : 'HIDDEN PATTERN'}</span>
        <div className={`tv-hidden ${hidden ? 'on' : ''}`}>{hidden ? String(hidden) : (zh ? '未发现' : 'none found')}</div>
      </div>
    </div>
  )
}

function CompactViz({ n, zh }: { n: Node; zh: boolean }) {
  // background / infrastructure spans: a few meaningful counters, never a dump
  const out = n.output ?? {}
  const pick = Object.entries(out).filter(([, v]) => typeof v === 'number' || typeof v === 'boolean').slice(0, 6)
  return (
    <div className="tv tv-compact">
      <span className="tv-lab">{zh ? '后台节点 · 关键计数' : 'BACKGROUND SPAN · KEY COUNTERS'}</span>
      <div className="tv-ops">
        {pick.map(([k, v]) => <div key={k} className="tv-op on"><b>{typeof v === 'boolean' ? (v ? 'Y' : 'N') : String(v)}</b><span>{k}</span></div>)}
      </div>
    </div>
  )
}

function NodeStage({ n, zh, c }: { n: Node; zh: boolean; c: Content }) {
  if (n.node_type === 'workflow') return <RunViz n={n} zh={zh} c={c} />
  if (n.node_type === 'retrieval') return <RetrieveViz n={n} zh={zh} c={c} />
  if (n.node_type === 'agent') return <SkillsViz n={n} zh={zh} c={c} />
  if (n.node_type === 'tool') return <ToolViz n={n} zh={zh} c={c} />
  if (n.node_type === 'context') return <ContextViz n={n} zh={zh} c={c} />
  if (n.node_type === 'reasoner') return <ReasonViz n={n} zh={zh} c={c} />
  if (n.node_type === 'verifier') return <VerifyViz n={n} zh={zh} c={c} />
  if (n.node_type === 'memory_write') return <ConsolidateViz n={n} zh={zh} />
  if (n.node_type === 'analysis') return <AnalyzeViz n={n} zh={zh} />
  return <CompactViz n={n} zh={zh} />
}

const BEAT = 1500

export function TraceReplay({ zh }: { zh: boolean }) {
  const [summ, setSumm] = useState<TraceSummary | null>(null)
  const [nodes, setNodes] = useState<Node[]>([])
  const [obs, setObs] = useState<ObsMeta | null>(null)
  const [cursor, setCursor] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [state, setState] = useState<'load' | 'none' | 'ok' | 'running'>('load')
  const [content, setContent] = useState<Content>(EMPTY_CONTENT)

  const load = useCallback(async () => {
    // limit=500: background index-maintenance spans fire continuously and would
    // otherwise push the real diagnostic run out of a short window.
    const d = await fetch('/api/rca/observability/traces?limit=500').then((r) => r.json())
    setObs(d.observability ?? null)
    const runs = (d.traces ?? []).filter((t: TraceSummary) => t.case_id !== 'index-maintenance' && t.node_count > 1)
    const pick = runs.sort((a: TraceSummary, b: TraceSummary) => b.node_count - a.node_count)[0]
    if (!pick) { setState('none'); return }
    setSumm(pick)
    const t = await fetch(`/api/rca/observability/traces/${pick.trace_id}`).then((r) => r.json())
    setNodes((t.nodes ?? []).filter((n: Node) => n.node_type !== 'storage'))
    setCursor(0); setState('ok'); setPlaying(true)
    // join the span ids with what they actually refer to: evidence lines from the
    // case, memory text from the observatory, and the diagnosis reasoning itself.
    // The case snapshot is milliseconds and carries the evidence lines and the
    // diagnosis reasoning, so it lands first. The evolution stream takes seconds
    // and only adds memory text, so it must not hold the rest of the content back.
    fetch('/api/rca/snapshot').then((r) => r.json()).then((snap) => {
      const kase = (snap?.cases ?? []).find((x: { id: string }) => x.id === pick.case_id)
      if (!kase) return
      const evi: Content['evi'] = {}
      for (const e of kase.diagnosis?.evidence ?? []) evi[e.evidenceId] = { summary: e.summary, source: e.source }
      setContent((c) => ({ ...c, evi, rootCause: kase.diagnosis?.rootCause ?? '', actions: kase.diagnosis?.recommendedActions ?? [] }))
    }).catch(() => { /* content is additive */ })
    fetch('/api/rca/evolution?passes=4').then((r) => r.json()).then((evo) => {
      const mem: Content['mem'] = {}
      for (const r of evo?.observatory?.records ?? []) mem[r.memory_id] = r.text
      setContent((c) => ({ ...c, mem }))
    }).catch(() => { /* memory text is additive */ })
  }, [])

  useEffect(() => { load().catch(() => setState('none')) }, [load])

  const runOnce = async () => {
    setState('running')
    try {
      const snap = await fetch('/api/rca/snapshot').then((r) => r.json())
      const caseId = snap?.cases?.[0]?.id
      if (!caseId) { setState('none'); return }
      await fetch('/api/rca/diagnose', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ case_id: caseId, session_id: 'console-replay' }),
      })
      await load()
    } catch { setState('none') }
  }

  const rail = useMemo(() => {
    if (!nodes.length) return []
    const base = Math.min(...nodes.map((n) => new Date(n.started_at).getTime()))
    const span = Math.max(1, ...nodes.map((n) => new Date(n.started_at).getTime() - base + n.duration_ms))
    return nodes.map((n) => ({ n, off: new Date(n.started_at).getTime() - base, span }))
  }, [nodes])

  const atEnd = cursor >= nodes.length - 1
  useEffect(() => {
    if (!playing || !nodes.length || atEnd) return
    const id = setTimeout(() => setCursor((c) => Math.min(nodes.length - 1, c + 1)), BEAT)
    return () => clearTimeout(id)
  }, [playing, cursor, nodes.length, atEnd])

  if (state === 'load') return <section className="tr"><div className="tr-wait">{zh ? '正在读取执行账本…' : 'READING LEDGER…'}</div></section>
  if (state === 'none' || state === 'running') return (
    <section className="tr">
      <div className="tr-empty-state">
        <span className="tr-lead">{zh ? '执行回放 · 逐节点可观测' : 'EXECUTION REPLAY'}</span>
        <p>{zh ? '账本里还没有完整的诊断轨迹。跑一次真实诊断即可回放它的每个节点。' : 'No full diagnostic run in the ledger yet. Run one to replay every node.'}</p>
        <button className="tr-run-btn" onClick={runOnce} disabled={state === 'running'}>
          {state === 'running' ? (zh ? '正在运行…' : 'RUNNING…') : (zh ? '跑一次诊断' : 'RUN A DIAGNOSIS')}
        </button>
      </div>
    </section>
  )
  if (!summ || !nodes.length) return null

  const cur = nodes[cursor] ?? nodes[0]
  const bottleIdx = nodes.findIndex((n) => n.span_id === summ.bottleneck?.span_id)
  const langfuseLive = (obs?.exporters ?? 0) > 0

  return (
    <section className="tr">
      <div className="tr-head">
        <div className="tr-title">
          <span className="tr-lead">{zh ? '执行回放 · 逐节点可观测' : 'EXECUTION REPLAY · PER-NODE OBSERVABILITY'}</span>
          <span className="tr-sub">{zh ? '回放账本里的真实节点，看每一环到底做了什么' : 'REPLAY THE REAL SPANS · SEE WHAT EACH NODE ACTUALLY DID'}</span>
        </div>
        <div className="tr-meta">
          <span><b>{nodes.length}</b>{zh ? '环' : 'NODES'}</span>
          <span><b>{summ.duration_ms.toFixed(1)}</b>ms</span>
          <span className="tr-run">{summ.case_id}</span>
          <span className={`tr-lf ${langfuseLive ? 'on' : ''}`}><b>Langfuse</b>{langfuseLive ? (zh ? '导出中' : 'LIVE') : (zh ? '就绪' : 'READY')}</span>
        </div>
      </div>

      <div className="tr-rail">
        {rail.map(({ n, off, span }, i) => (
          <button key={n.span_id} className={`tr-seg ${i === cursor ? 'on' : ''} ${i === bottleIdx ? 'bottle' : ''} ${i < cursor ? 'done' : ''}`}
            onClick={() => { setCursor(i); setPlaying(false) }} title={n.node_name}>
            <span className={`tr-seg-bar t-${n.node_type}`} style={{ left: `${(off / span) * 100}%`, width: `${Math.max(1.2, (n.duration_ms / span) * 100)}%` }} />
            <span className="tr-seg-lab" style={{ left: `${(off / span) * 100}%` }}>{typeName(n.node_type, zh)}{i === bottleIdx ? ` · ${zh ? '瓶颈' : 'SLOW'}` : ''}</span>
          </button>
        ))}
      </div>

      <div className="tr-stage" key={cursor}>
        <div className="tr-node-head">
          <span className={`tr-node-type t-${cur.node_type}`}>{typeName(cur.node_type, zh)}</span>
          <span className="tr-node-name">{cur.node_name}</span>
          <span className="tr-node-facts"><b>{cur.duration_ms.toFixed(cur.duration_ms < 10 ? 2 : 1)}</b>ms · <span className={cur.status === 'ok' ? 'ok' : 'bad'}>{cur.status.toUpperCase()}</span>{cursor === bottleIdx ? <em className="tr-bottle-tag">{zh ? '本轮瓶颈' : 'BOTTLENECK'}</em> : null}</span>
        </div>
        <NodeStage n={cur} zh={zh} c={content} />
      </div>

      <div className="tr-transport">
        <button onClick={() => { if (atEnd) { setCursor(0); setPlaying(true) } else setPlaying((p) => !p) }}>{playing && !atEnd ? '❚❚' : '▶'}</button>
        <button onClick={() => { setCursor(0); setPlaying(false) }} title="reset">⤺</button>
        <button onClick={() => { setCursor((c) => Math.max(0, c - 1)); setPlaying(false) }} disabled={cursor <= 0}>◀</button>
        <button onClick={() => { setCursor((c) => Math.min(nodes.length - 1, c + 1)); setPlaying(false) }} disabled={atEnd}>▶</button>
        <span className="tr-transport-pos">{zh ? '第' : 'NODE'} <b>{cursor + 1}</b> / {nodes.length} {zh ? '环' : ''}</span>
        {obs ? <span className="tr-transport-lf">{zh ? '同一份 trace → Langfuse' : 'SAME TRACE → LANGFUSE'} · {obs.events_written ?? 0} {zh ? '事件' : 'events'} · {obs.export_thread_alive ? (zh ? '导出中' : 'exporting') : (zh ? '门控未开' : 'gated off')}</span> : null}
      </div>
    </section>
  )
}
