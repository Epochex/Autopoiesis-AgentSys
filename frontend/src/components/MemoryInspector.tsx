/* ── ② MEMORY INSPECTOR · one record, fully auditable ─────────────────────────
   Every value on this panel is read straight off the serialized kernel run
   (records / events / recall). Nothing is inferred beyond set-differences over
   real snapshots, and every place where the kernel does NOT record something
   (see MemCapabilities) is rendered as an explicit note instead of a number.
   --acid means exactly one thing here: THIS field changed at THIS cursor step. */
import './memory-inspector.css'
import type { ReactNode } from 'react'
import type { MemCapabilities, MemEvent, MemRecall, MemRecord, MemSnapshot, MemTier } from '../types'

/* ── i18n · EN is uppercased by CSS on label classes, so EN labels stay short.
      Prose notes use .mi-note, which deliberately does NOT uppercase. ── */
const L: Record<string, [string, string]> = {
  kick: ['记忆条目 · 逐字段可审计', 'MEMORY RECORD · FIELD-LEVEL AUDIT'],
  real: ['真实 · R230', 'REAL · R230'],
  emptyT: ['未选中条目', 'NO RECORD SELECTED'],
  emptyB: [
    '在左侧记忆图中选中一个节点，这里会显示它的全文、来源、每一步的字段级变更，以及内核未记录的部分。',
    'Pick a node in the memory graph. Its full text, provenance, per-step field diff, and the parts the kernel never recorded will appear here.',
  ],
  text: ['全文', 'TEXT'],
  noteText: [
    '文本在 ADD 时写入一次，之后从未改写 —— 内核没有文本改写路径（update_text_mutation=false）。',
    'Text written once at ADD, never rewritten — the kernel has no text-mutation path (update_text_mutation=false).',
  ],

  unborn: ['此刻尚不存在', 'NOT YET IN MEMORY'],
  unbornB: ['该条目在游标位置尚未写入。首次出现于 seq', 'This record does not exist at the cursor yet. First written at seq'],
  state: ['游标处状态', 'STATE @ CURSOR'],
  from: ['取自 seq', 'FROM SEQ'],
  conf: ['置信', 'CONFIDENCE'],
  imp: ['重要度', 'IMPORTANCE'],
  str: ['强度', 'STRENGTH'],
  inert: ['惰性 · 衰减未接线', 'INERT · DECAY NOT WIRED'],
  noteDecay: [
    '强度在每一条记录上都恒为 1.00：衰减未接线（decay_wired=false）。这是一个惰性字段，不代表任何遗忘正在发生。',
    'Strength is 1.00 on every record: decay is not wired (decay_wired=false). Inert field — no forgetting is happening.',
  ],

  chg: ['本步变更', 'CHANGE @ THIS STEP'],
  last: ['最近一次变更', 'LAST CHANGE'],
  noChg: ['本步该条目未变', 'UNTOUCHED AT THIS STEP'],
  created: ['创建', 'CREATED'],
  noPrior: ['无前态 —— 这是写入，不是变更', 'No prior state — this is a creation, not a diff.'],
  initial: ['初始值', 'INITIAL'],
  route: ['写入路由', 'WRITE ROUTER'],
  edge: ['新增连接', 'EDGE CREATED'],
  noSnap: ['该 op 不携带前/后快照', 'This op carries no before/after snapshot.'],
  absFrom: ['抽象自', 'ABSTRACTED FROM'],
  absN: ['条具体情景记忆', 'CONCRETE EPISODES'],
  noteReinf: [
    '该条目在一次通过校验的运行中被召回并被引用 —— 这就是加固的全部依据。内核不记录任何“原因”字符串。',
    'The record was recalled and cited on a verified run — that is the whole basis. The kernel records no reason string.',
  ],
  noteQuar: ['内核记录的隔离原因', 'Quarantine reason as recorded by the kernel'],
  addedT: ['新增标签', 'TAGS ADDED'],
  addedA: ['新增资产', 'ASSETS ADDED'],
  addedL: ['新增连接', 'LINKS ADDED'],

  ctx: ['上下文包', 'CONTEXT PACKET'],
  inCtx: ['已进入上下文', 'IN CONTEXT'],
  dropCtx: ['召回后被丢弃', 'RECALLED, THEN DROPPED'],
  missCtx: ['本次未被召回', 'NOT RECALLED'],
  noCtx: ['游标处无召回记录', 'NO RECALL AT CURSOR'],
  ctxCase: ['案例', 'CASE'],
  ctxPass: ['轮次', 'PASS'],
  ctxInc: ['进包', 'INCLUDED'],
  ctxDrop: ['丢弃', 'DROPPED'],
  ctxRet: ['召回', 'RETRIEVED'],
  ctxRes: ['直接命中', 'RESOLVED BY'],
  noteDrop: [
    '内核不记录丢弃原因（context_drop_reason=false）——本条只知道“被丢弃”，不知道“为什么”。已知配置：ContextCompiler 上限 8 条记忆行 / 900 token 预算；这是配置常量，不是本次丢弃被记录下来的原因。',
    'No drop reason is recorded (context_drop_reason=false) — that it was dropped is known, why is not. Known configuration: the ContextCompiler caps at 8 memory lines / a 900-token budget. That is a config constant, not a recorded reason for this drop.',
  ],
  noteScores: [
    '内核不记录检索得分（retrieval_scores=false）：只知命中与否，不知排名分数。',
    'No retrieval scores are recorded (retrieval_scores=false): hit or miss is known, rank is not.',
  ],

  prov: ['来源与关系', 'PROVENANCE'],
  atCur: ['游标处', '@ CURSOR'],
  tags: ['标签', 'TAGS'],
  assets: ['资产', 'ASSETS'],
  links: ['连接', 'LINKS'],
  recFields: ['条目字段 · 无逐事件快照', 'RECORD FIELDS · NOT SNAPSHOTTED PER-EVENT'],
  evid: ['证据 ID', 'EVIDENCE IDS'],
  trace: ['来源运行', 'SOURCE TRACES'],
  snap: ['证据快照', 'EVIDENCE SNAPSHOT'],
  none: ['无', 'NONE'],

  ledger: ['该条目的全部事件', 'EVENT LEDGER FOR THIS RECORD'],
  quar: ['已隔离', 'QUARANTINED'],
}
const t = (k: string, zh: boolean) => L[k][zh ? 0 : 1]

const TIER: Record<MemTier, [string, string]> = {
  episodic: ['情景', 'EPISODIC'],
  semantic: ['语义', 'SEMANTIC'],
  procedural: ['程序', 'PROCEDURAL'],
  asset_profile: ['资产画像', 'ASSET PROFILE'],
}
/* zh gloss for the kernel's op vocabulary. The raw op name is always shown too —
   it is the kernel's own word and belongs in an audit panel verbatim. */
const OP_ZH: Record<string, string> = {
  ADD: '写入', UPDATE: '改写', NOOP: '忽略', REINFORCE: '加固',
  QUARANTINE: '隔离', INSIGHT: '洞察', LINK: '连接',
}

const f2 = (n: number) => n.toFixed(2)
const f4 = (n: number) => n.toFixed(4)
/** Real set-difference over two recorded snapshots. Never a guess. */
const minus = (a: string[], b: string[]) => a.filter((x) => !b.includes(x))
const uniq = (a: string[]) => Array.from(new Set(a))

function Chips({ items, cap }: { items: string[]; cap?: string }) {
  if (!items.length) return <span className="mi-nil">—</span>
  return (
    <span className="mi-chips">
      {items.map((x) => <i key={x} className={cap ? `mi-chip ${cap}` : 'mi-chip'}>{x}</i>)}
    </span>
  )
}

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="mi-row">
      <span className="mi-row-k">{k}</span>
      <span className="mi-row-v">{children}</span>
    </div>
  )
}

/* ── field-level diff over two REAL snapshots · only fields that actually moved ─ */
function scalarDiffs(b: MemSnapshot, a: MemSnapshot, decayWired: boolean) {
  const keys: [keyof MemSnapshot & ('confidence' | 'importance' | 'strength'), string][] = [
    ['confidence', 'conf'], ['importance', 'imp'],
  ]
  // strength only becomes a live scalar if decay is ever actually wired.
  if (decayWired) keys.push(['strength', 'str'])
  return keys.filter(([k]) => b[k] !== a[k]).map(([k, lab]) => ({ k, lab, from: b[k], to: a[k] }))
}

function ChangePanel({
  ev, live, capabilities, quarReason, zh,
}: { ev: MemEvent; live: boolean; capabilities: MemCapabilities; quarReason: string | null; zh: boolean }) {
  const { before: b, after: a } = ev
  const sc = b && a ? scalarDiffs(b, a, capabilities.decay_wired) : []
  // set-differences straight off the recorded snapshots, merged with the kernel's
  // own added_* fields. Both are real; neither is fabricated when empty.
  const dTags = uniq([...(b && a ? minus(a.tags, b.tags) : []), ...ev.added_tags])
  const dAssets = uniq([...(b && a ? minus(a.asset_ids, b.asset_ids) : []), ...ev.added_assets])
  const dLinks = b && a ? minus(a.links, b.links) : []
  const creation = !b && !!a
  const nothing = !creation && !sc.length && !dTags.length && !dAssets.length && !dLinks.length

  return (
    <div className={live ? 'mi-chg live' : 'mi-chg'}>
      <div className="mi-chg-head">
        <span className="mi-op">{ev.op}{zh && OP_ZH[ev.op] ? ` ${OP_ZH[ev.op]}` : ''}</span>
        <span className="mi-chg-at">SEQ {ev.seq} · PASS {ev.pass}</span>
        <span className="mi-chg-case">{ev.case_id}</span>
      </div>

      {/* ADD carries the router's real similarity — the same number RouteRuler plots. */}
      {ev.similarity !== null && (
        <Row k={t('route', zh)}>
          <span className="mi-mono">similarity {f4(ev.similarity)} → {ev.op}</span>
        </Row>
      )}

      {/* before === null ⇒ no prior state existed. A creation, not a diff. */}
      {creation && (
        <>
          <div className="mi-note">{t('noPrior', zh)}</div>
          <div className="mi-init">
            <span className="mi-row-k">{t('initial', zh)}</span>
            <span className="mi-mono">
              {t('conf', zh)} {f2(a.confidence)} · {t('imp', zh)} {f2(a.importance)}
            </span>
          </div>
        </>
      )}

      {/* the genuine before → after ladder */}
      {!!sc.length && (
        <div className="mi-diff">
          {sc.map((d) => (
            <div className="mi-d" key={d.k}>
              <span className="mi-d-k">{t(d.lab, zh)}</span>
              <span className="mi-d-a">{f2(d.from)}</span>
              <span className="mi-d-ar" aria-hidden="true">→</span>
              <span className="mi-d-b">{f2(d.to)}</span>
            </div>
          ))}
        </div>
      )}
      {!!dTags.length && <Row k={t('addedT', zh)}><Chips items={dTags} /></Row>}
      {!!dAssets.length && <Row k={t('addedA', zh)}><Chips items={dAssets} /></Row>}
      {!!dLinks.length && <Row k={t('addedL', zh)}><Chips items={dLinks} cap="id" /></Row>}

      {ev.op === 'LINK' && ev.target_id && (
        <Row k={t('edge', zh)}><i className="mi-chip id">{ev.target_id}</i></Row>
      )}
      {ev.op === 'INSIGHT' && !!ev.source_memory_ids?.length && (
        <div className="mi-abs">
          <span className="mi-abs-h">
            {t('absFrom', zh)} <b>{ev.source_memory_ids.length}</b> {t('absN', zh)}
          </span>
          {ev.source_memory_ids.map((id) => <i className="mi-abs-id" key={id}>{id}</i>)}
        </div>
      )}
      {ev.op === 'REINFORCE' && <div className="mi-note">{t('noteReinf', zh)}</div>}
      {ev.op === 'QUARANTINE' && quarReason && (
        <Row k={t('noteQuar', zh)}><span className="mi-mono">{quarReason}</span></Row>
      )}
      {nothing && ev.op !== 'LINK' && <div className="mi-note">{t('noSnap', zh)}</div>}
    </div>
  )
}

export function MemoryInspector({
  record, events, cursorSeq, recall, capabilities, zh,
}: {
  record: MemRecord | null
  events: MemEvent[]
  cursorSeq: number
  recall: MemRecall | null
  capabilities: MemCapabilities
  zh: boolean
}) {
  if (!record) {
    return (
      <section className="mi-root empty">
        <header className="mi-mast">
          <span className="mi-kick">{t('kick', zh)}</span>
        </header>
        <div className="mi-empty">
          <span className="mi-empty-t">{t('emptyT', zh)}</span>
          <p className="mi-empty-b">{t('emptyB', zh)}</p>
        </div>
      </section>
    )
  }

  const id = record.memory_id
  const chrono = [...events].sort((a, b) => a.seq - b.seq)
  const past = chrono.filter((e) => e.seq <= cursorSeq)
  const exists = past.length > 0
  const atCursor = chrono.find((e) => e.seq === cursorSeq) ?? null
  const lastChange = past.length ? past[past.length - 1] : null
  const shown = atCursor ?? lastChange
  // state at the cursor = the most recent snapshot the kernel actually recorded.
  const stateEv = [...past].reverse().find((e) => e.after) ?? null
  const st = stateEv?.after ?? null

  // LINK carries no snapshot, so fold its real targets in on top of the last snapshot.
  const linkedAt = uniq([...(st?.links ?? []), ...past.filter((e) => e.op === 'LINK' && e.target_id).map((e) => e.target_id as string)])

  const inCtx = recall?.included_memory_ids.includes(id) ?? false
  const wasDropped = recall?.dropped_memory_ids.includes(id) ?? false
  const retrieved = recall ? Object.values(recall.retrieved).some((v) => v?.includes(id)) : false
  const resolvedBy = recall?.resolved_memory_ids.includes(id) ?? false
  const ctx = !recall ? 'no' : inCtx ? 'in' : wasDropped ? 'drop' : retrieved ? 'ret' : 'miss'

  const ops = chrono.reduce<Record<string, number>>((m, e) => ({ ...m, [e.op]: (m[e.op] ?? 0) + 1 }), {})
  const lastSeq = chrono.length ? chrono[chrono.length - 1].seq : 0
  const domain = Math.max(lastSeq, cursorSeq, 1)
  const lx = (s: number) => 4 + (s / domain) * 552

  return (
    <section className="mi-root">
      <header className="mi-mast">
        <span className="mi-kick">{t('kick', zh)}</span>
        <span className="mi-real">{t('real', zh)}</span>
      </header>

      <div className="mi-scroll">
        {/* ── identity ─────────────────────────────────────────────────────── */}
        <div className="mi-idbar">
          <span className="mi-tier">{TIER[record.tier][zh ? 0 : 1]}</span>
          <span className="mi-id">{id}</span>
          {record.quarantined && <span className="mi-quar">{t('quar', zh)}</span>}
        </div>

        <section className="mi-sec">
          <h4 className="mi-h">{t('text', zh)}</h4>
          <p className="mi-text">{record.text}</p>
          {!capabilities.update_text_mutation && <div className="mi-note">{t('noteText', zh)}</div>}
        </section>

        {/* ── state at cursor ──────────────────────────────────────────────── */}
        <section className="mi-sec">
          <h4 className="mi-h">
            {t('state', zh)}
            {stateEv && <em className="mi-h-sub">{t('from', zh)} {stateEv.seq}</em>}
          </h4>
          {!exists ? (
            <div className="mi-unborn">
              <span className="mi-unborn-t">{t('unborn', zh)}</span>
              <span className="mi-unborn-b">{t('unbornB', zh)} {chrono[0]?.seq ?? '—'}</span>
            </div>
          ) : (
            <>
              <div className="mi-scal">
                {(['confidence', 'importance'] as const).map((k) => {
                  const moved = !!atCursor?.before && !!atCursor?.after && atCursor.before[k] !== atCursor.after[k]
                  return (
                    <div className={moved ? 'mi-sc live' : 'mi-sc'} key={k}>
                      <b>{st ? f2(st[k]) : '—'}</b>
                      <span>{t(k === 'confidence' ? 'conf' : 'imp', zh)}</span>
                    </div>
                  )
                })}
                {/* strength is 1.0 on every record: decay was never wired. Shown as
                    inert, never as a live scalar, never as evidence of forgetting. */}
                <div className={capabilities.decay_wired ? 'mi-sc' : 'mi-sc dead'}>
                  <b>{st ? f2(st.strength) : '—'}</b>
                  <span>{t('str', zh)}</span>
                  {!capabilities.decay_wired && <i className="mi-dead-tag">{t('inert', zh)}</i>}
                </div>
              </div>
              {!capabilities.decay_wired && <div className="mi-note">{t('noteDecay', zh)}</div>}
            </>
          )}
        </section>

        {/* ── the diff ─────────────────────────────────────────────────────── */}
        <section className="mi-sec">
          <h4 className="mi-h">{atCursor ? t('chg', zh) : t('last', zh)}</h4>
          {shown ? (
            <ChangePanel
              ev={shown}
              live={!!atCursor}
              capabilities={capabilities}
              quarReason={record.quarantine_reason}
              zh={zh}
            />
          ) : (
            <div className="mi-note">{t('noChg', zh)}</div>
          )}
        </section>

        {/* ── context packet ───────────────────────────────────────────────── */}
        <section className="mi-sec">
          <h4 className="mi-h">{t('ctx', zh)}</h4>
          <div className={`mi-ctx ${ctx}`}>
            <span className="mi-ctx-t">
              {ctx === 'in' ? t('inCtx', zh)
                : ctx === 'drop' ? t('dropCtx', zh)
                  : ctx === 'no' ? t('noCtx', zh) : t('missCtx', zh)}
            </span>
            {recall && (
              <span className="mi-ctx-m">
                {t('ctxCase', zh)} {recall.case_id} · {t('ctxPass', zh)} {recall.pass} ·{' '}
                {t('ctxInc', zh)} {recall.included_memory_ids.length} · {t('ctxDrop', zh)} {recall.dropped_memory_ids.length}
              </span>
            )}
          </div>
          {resolvedBy && <Row k={t('ctxRes', zh)}><span className="mi-mono">shortcut · probes {recall?.probes}</span></Row>}
          {/* the kernel never records WHY a recalled memory was dropped. Say so. */}
          {ctx === 'drop' && !capabilities.context_drop_reason && <div className="mi-note">{t('noteDrop', zh)}</div>}
          {!capabilities.retrieval_scores && retrieved && <div className="mi-note">{t('noteScores', zh)}</div>}
        </section>

        {/* ── provenance ───────────────────────────────────────────────────── */}
        <section className="mi-sec">
          <h4 className="mi-h">{t('prov', zh)}<em className="mi-h-sub">{t('atCur', zh)}</em></h4>
          <Row k={t('tags', zh)}><Chips items={st?.tags ?? []} /></Row>
          <Row k={t('assets', zh)}><Chips items={st?.asset_ids ?? []} /></Row>
          <Row k={t('links', zh)}><Chips items={linkedAt} cap="id" /></Row>

          <h4 className="mi-h thin">{t('recFields', zh)}</h4>
          <Row k={t('evid', zh)}><Chips items={record.evidence_ids} cap="id" /></Row>
          <Row k={t('trace', zh)}><Chips items={record.source_trace_ids} cap="id" /></Row>
        </section>

        {/* ── evidence snapshot ────────────────────────────────────────────── */}
        {!!record.evidence_snapshot.length && (
          <section className="mi-sec">
            <h4 className="mi-h">{t('snap', zh)}</h4>
            {record.evidence_snapshot.map((e, i) => (
              <div className="mi-ev" key={e.evidence_id ?? i}>
                <span className="mi-ev-id">{e.evidence_id ?? '—'}</span>
                {e.source && <span className="mi-ev-src">{e.source}</span>}
                {e.summary && <p className="mi-ev-sum">{e.summary}</p>}
              </div>
            ))}
          </section>
        )}

        {/* ── ledger · only ops that actually fired on this record ──────────── */}
        <section className="mi-sec last">
          <h4 className="mi-h">{t('ledger', zh)}</h4>
          <div className="mi-ops">
            {Object.entries(ops).map(([op, n]) => (
              <span className="mi-op-c" key={op}><b>{op}</b>{n}</span>
            ))}
          </div>
          <svg className="mi-life" viewBox="0 0 560 24" preserveAspectRatio="xMidYMid meet" role="img"
            aria-label={`${chrono.length} events, seq 0 to ${domain}`}>
            <line className="mi-life-ax" x1={4} y1={16} x2={556} y2={16} />
            {chrono.map((e) => (
              <line key={e.seq} className={`mi-life-t${e.seq === cursorSeq ? ' live' : e.seq <= cursorSeq ? ' past' : ''}`}
                x1={lx(e.seq)} y1={6} x2={lx(e.seq)} y2={16}>
                <title>{`seq ${e.seq} · ${e.op}`}</title>
              </line>
            ))}
            <path className="mi-life-cur" d={`M${lx(cursorSeq)} 2 v18`} />
            <text className="mi-life-n" x={4} y={23}>0</text>
            <text className="mi-life-n" x={556} y={23} textAnchor="end">{domain}</text>
          </svg>
        </section>
      </div>
    </section>
  )
}
