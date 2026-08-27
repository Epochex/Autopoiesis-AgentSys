/* ── ② MEMORY INSPECTOR · one record, fully auditable ─────────────────────────
   Every value on this panel is read straight off the serialized kernel run
   (records / events / recall). Nothing is inferred beyond set-differences over
   real snapshots, and every place where the kernel does NOT record something
   (see MemCapabilities) is rendered as an explicit note instead of a number.
   --acid means exactly one thing here: THIS field changed at THIS cursor step. */
import './memory-inspector.css'
import type { ReactNode } from 'react'
import type { MemCapabilities, MemEvent, MemRecall, MemRecord, MemSnapshot, MemTier } from '../types'
import { buildRelationRows } from './memory-relations'

/* ── i18n · EN is uppercased by CSS on label classes, so EN labels stay short.
      Prose notes use .mi-note, which deliberately does NOT uppercase. ── */
const L: Record<string, [string, string]> = {
  kick: ['记录详情 · 每一步改了什么', 'RECORD DETAILS · WHAT CHANGED AT EACH STEP'],
  real: ['R230 · 内网', 'R230 · LAN'],
  emptyT: ['未选中条目', 'NO RECORD SELECTED'],
  emptyB: [
    '在左侧选中一条记录，这里会显示全文、来源、每一步改了什么，以及原始数据没有提供什么。',
    'Select a record on the left to see its full text, source, changes at each step, and anything missing from the source data.',
  ],
  text: ['全文', 'TEXT'],
  noteText: [
    '全文在写入时确定，之后仅更新标签、资产与置信度。',
    'The text is fixed at write time; later updates touch tags, assets and confidence.',
  ],

  unborn: ['此刻尚不存在', 'NOT YET IN MEMORY'],
  unbornB: ['该条目在游标位置尚未写入。首次出现于 seq', 'This record does not exist at the cursor yet. First written at seq'],
  state: ['游标处状态', 'STATE @ CURSOR'],
  from: ['取自 seq', 'FROM SEQ'],
  conf: ['可信度', 'CONFIDENCE'],
  imp: ['重要度', 'IMPORTANCE'],
  str: ['保留强度', 'RETENTION'],
  inert: ['当前不会慢慢淡忘', 'FADING IS OFF'],
  noteDecay: [
    '每条记录的保留强度都是 1.00，当前没有启用慢慢淡忘（decay_wired=false）。',
    'Retention is 1.00 on every record because fading is off (decay_wired=false).',
  ],

  chg: ['本步变更', 'CHANGE @ THIS STEP'],
  last: ['最近一次变更', 'LAST CHANGE'],
  noChg: ['本步该条目未变', 'UNTOUCHED AT THIS STEP'],
  created: ['创建', 'CREATED'],
  noPrior: ['首次写入 · 无前值可比', 'First write · no prior value'],
  initial: ['初始值', 'INITIAL'],
  route: ['为何这样写入', 'WHY IT WAS WRITTEN THIS WAY'],
  edge: ['新增连接', 'EDGE CREATED'],
  noSnap: ['这个动作没有记录修改前后的值', 'This action has no before-and-after values.'],
  absFrom: ['这条总结来自', 'SUMMED FROM'],
  absN: ['件遇到过的事', 'SEEN-BEFORE RECORDS'],
  noteReinf: [
    '这条记录在一次结果正确的排查中又被找到并采用，所以记作“又见到一次”。原始记录没有保存更具体的原因。',
    'This record was found and used again in a successful check, so it is marked SEEN AGAIN. No more specific reason was recorded.',
  ],
  noteRefresh: [
    '系统每轮都会重新计算这组记录的总结和重要度，新值会覆盖“又见到一次”带来的增加，因此单独记作“更新总结”。',
    'Each run recalculates this group summary and its importance. The new value replaces the SEEN AGAIN increase, so it is recorded as SUMMARY UPDATED.',
  ],
  noteQuar: ['打入冷宫的原因原文', 'RAW REASON IT WAS SHELVED'],
  addedT: ['新增标签', 'TAGS ADDED'],
  addedA: ['新增资产', 'ASSETS ADDED'],
  addedL: ['新增连接', 'LINKS ADDED'],

  ctx: ['本次排查用到的记录', 'RECORDS USED FOR THIS CHECK'],
  inCtx: ['本次已采用', 'USED THIS TIME'],
  dropCtx: ['找到但没采用', 'FOUND BUT NOT USED'],
  missCtx: ['本次没找到', 'NOT FOUND THIS TIME'],
  noCtx: ['当前位置没有查找记录', 'NO LOOKUP AT THIS POSITION'],
  ctxCase: ['案例', 'CASE'],
  ctxPass: ['轮次', 'PASS'],
  ctxInc: ['进包', 'INCLUDED'],
  ctxDrop: ['丢弃', 'DROPPED'],
  ctxRet: ['找到', 'FOUND'],
  ctxRes: ['直接命中', 'RESOLVED BY'],
  score: ['最终得分', 'FINAL SCORE'],
  sparse: ['文字匹配分', 'TEXT-MATCH SCORE'],
  dense: ['意思相近分', 'MEANING-MATCH SCORE'],
  graphHop: ['关系距离', 'LINK DISTANCE'],
  noteDrop: [
    '原始记录只说明这条内容没有采用，没有保存具体原因（context_drop_reason=false）。当前上限是 8 条记录、900 token，这只是固定配置。',
    'The source data says only that this record was not used; it does not save the reason (context_drop_reason=false). The fixed limit is 8 records and 900 tokens.',
  ],
  noteScores: [
    '原始记录没有查找得分（retrieval_scores=false），只能看出是否找到，无法查看排名分数。',
    'The source data has no lookup scores (retrieval_scores=false), so it shows whether a record was found but not its ranking score.',
  ],

  prov: ['来源与关系', 'SOURCE AND LINKS'],
  atCur: ['游标处', '@ CURSOR'],
  tags: ['标签', 'TAGS'],
  assets: ['资产', 'ASSETS'],
  links: ['连接', 'LINKS'],
  recFields: ['记录字段 · 每个动作没有单独留底', 'RECORD FIELDS · NO COPY SAVED PER ACTION'],
  evid: ['证据 ID', 'EVIDENCE IDS'],
  trace: ['来源运行', 'SOURCE TRACES'],
  snap: ['当时保存的证据', 'SAVED EVIDENCE'],
  none: ['无', 'NONE'],

  ledger: ['这条记录发生过的全部动作', 'ALL ACTIONS FOR THIS RECORD'],
  quar: ['已打入冷宫', 'SHELVED'],

  /* ── which record this panel is showing, and why. The two modes were
        previously distinguishable only by "the panel stopped changing", which
        only reads as a mode to someone who already knew there was one. ── */
  followT: ['跟随游标', 'FOLLOWING CURSOR'],
  followH: ['点击任一记忆卡片可锁定', 'CLICK ANY MEMORY CARD TO PIN IT HERE'],
  pinT: ['已锁定', 'PINNED'],
  pinH: ['游标继续走 · 此面板停在', 'CURSOR KEEPS RUNNING · PANEL HELD ON'],
  release: ['解除 ESC', 'RELEASE ESC'],
}
const t = (k: string, zh: boolean) => L[k][zh ? 0 : 1]

const TIER: Record<MemTier, [string, string]> = {
  episodic: ['遇到过的事', 'SEEN BEFORE'],
  semantic: ['归纳的规律', 'PATTERN'],
  procedural: ['处理办法', 'HOW-TO'],
  asset_profile: ['设备资料', 'ASSET INFO'],
}
const OP_LABEL: Record<string, [string, string]> = {
  ADD: ['写入', 'ADD'], UPDATE: ['改写', 'UPDATE'], NOOP: ['没有变化', 'NO CHANGE'],
  REINFORCE: ['又见到一次', 'SEEN AGAIN'], QUARANTINE: ['打入冷宫', 'SHELVED'],
  INSIGHT: ['总结', 'SUMMARY'], INSIGHT_REFRESH: ['更新总结', 'SUMMARY UPDATED'],
  LINK: ['连接', 'LINK'], DECAY: ['慢慢淡忘', 'FADING'], FORGET: ['忘掉了', 'FORGOTTEN'],
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

function RelationList({ rows, zh }: { rows: ReturnType<typeof buildRelationRows>; zh: boolean }) {
  if (!rows.length) return <span className="mi-nil">—</span>
  return (
    <span className="mi-rel-list" role="list">
      {rows.map((row) => (
        <span className="mi-rel" role="listitem" key={row.targetId}>
          <span className="mi-rel-target" title={row.targetId}>{row.targetId}</span>
          <span className="mi-rel-meta">
            {row.relations.length ? row.relations.map((relation, index) => (
              <span
                className="mi-rel-kind"
                key={`${relation.relation_type}-${index}`}
                title={relation.evidence_ids.length
                  ? `${relation.relation_type} · confidence ${f2(relation.confidence)} · evidence ${relation.evidence_ids.join(', ')}`
                  : `${relation.relation_type} · confidence ${f2(relation.confidence)}`}
              >
                {relation.relation_type} <span aria-hidden="true">·</span> <b>{f2(relation.confidence)}</b>
              </span>
            )) : (
              <span className="mi-rel-kind generic">{zh ? '关联' : 'linked'}</span>
            )}
          </span>
        </span>
      ))}
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
        <span className="mi-op">{OP_LABEL[ev.op]?.[zh ? 0 : 1] ?? ev.op}</span>
        <span className="mi-chg-at">SEQ {ev.seq} · PASS {ev.pass}</span>
        <span className="mi-chg-case">{ev.case_id}</span>
      </div>

      {/* ADD carries the router's real similarity — the same number RouteRuler plots. */}
      {ev.similarity !== null && (
        <Row k={t('route', zh)}>
          <span className="mi-mono">{zh ? '相近程度' : 'similarity'} {f4(ev.similarity)} → {OP_LABEL[ev.op]?.[zh ? 0 : 1] ?? ev.op}</span>
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
      {(ev.op === 'INSIGHT' || ev.op === 'INSIGHT_REFRESH') && !!ev.source_memory_ids?.length && (
        <div className="mi-abs">
          <span className="mi-abs-h">
            {t('absFrom', zh)} <b>{ev.source_memory_ids.length}</b> {t('absN', zh)}
          </span>
          {ev.source_memory_ids.map((id) => <i className="mi-abs-id" key={id}>{id}</i>)}
        </div>
      )}
      {ev.op === 'REINFORCE' && <div className="mi-note">{t('noteReinf', zh)}</div>}
      {ev.op === 'INSIGHT_REFRESH' && <div className="mi-note">{t('noteRefresh', zh)}</div>}
      {ev.op === 'QUARANTINE' && quarReason && (
        <Row k={t('noteQuar', zh)}><span className="mi-mono">{quarReason}</span></Row>
      )}
      {nothing && ev.op !== 'LINK' && <div className="mi-note">{t('noSnap', zh)}</div>}
    </div>
  )
}

/* ── mode strip · WHICH record, WHY this one, HOW to get out ──────────────────
   Sits between the masthead and the scroller so it is on screen at every scroll
   position: it is the answer to "is this thing following the replay or not",
   and that question can be asked at any moment.
   Structure, not accent, carries the state — --acid is spoken for (changed at
   this step), and a pin is not a change. Pinned inverts to a solid ink bar;
   following is a dashed outline. Neither is colour-only: both are labelled. */
function ModeStrip({ pinned, id, onUnpin, zh }: { pinned: boolean; id: string | null; onUnpin: () => void; zh: boolean }) {
  return (
    <div className={pinned ? 'mi-mode pinned' : 'mi-mode'} aria-live="polite">
      <span className="mi-mode-t">{t(pinned ? 'pinT' : 'followT', zh)}</span>
      <span className="mi-mode-h">{t(pinned ? 'pinH' : 'followH', zh)}</span>
      {pinned && id && <span className="mi-mode-id" title={id}>{id}</span>}
      {pinned && (
        <button type="button" className="mi-unpin" onClick={onUnpin}>
          {t('release', zh)}
        </button>
      )}
    </div>
  )
}

export function MemoryInspector({
  record, events, cursorSeq, recall, capabilities, pinned, onUnpin, zh,
}: {
  record: MemRecord | null
  events: MemEvent[]
  cursorSeq: number
  recall: MemRecall | null
  capabilities: MemCapabilities
  pinned: boolean
  onUnpin: () => void
  zh: boolean
}) {
  if (!record) {
    return (
      <section className="mi-root empty">
        <header className="mi-mast">
          <span className="mi-kick">{t('kick', zh)}</span>
        </header>
        <ModeStrip pinned={false} id={null} onUnpin={onUnpin} zh={zh} />
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
  // Never leak final-store relation metadata backwards while the replay cursor
  // is still before this record (or before its relation-bearing snapshot).
  const relationsAt = st?.relations ?? (events.length === 0 ? record.relations ?? [] : [])
  const relationRows = buildRelationRows(linkedAt, relationsAt)

  const inCtx = recall?.included_memory_ids.includes(id) ?? false
  const wasDropped = recall?.dropped_memory_ids.includes(id) ?? false
  const retrieved = recall ? Object.values(recall.retrieved).some((v) => v?.includes(id)) : false
  const retrievalDetail = recall?.retrieval_candidates?.find((item) => item.memory_id === id)
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
      <ModeStrip pinned={pinned} id={id} onUnpin={onUnpin} zh={zh} />

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
          {retrievalDetail && capabilities.retrieval_scores && (
            <>
              <Row k={t('score', zh)}><span className="mi-mono">{f2(retrievalDetail.final_score)}</span></Row>
              <Row k={t('sparse', zh)}><span className="mi-mono">{f2(retrievalDetail.lexical_score)}</span></Row>
              <Row k={t('dense', zh)}><span className="mi-mono">{f2(retrievalDetail.vector_score)}</span></Row>
              <Row k={t('graphHop', zh)}><span className="mi-mono">{retrievalDetail.graph_hop}</span></Row>
            </>
          )}
          {/* the kernel never records WHY a recalled memory was dropped. Say so. */}
          {ctx === 'drop' && !capabilities.context_drop_reason && <div className="mi-note">{t('noteDrop', zh)}</div>}
          {!capabilities.retrieval_scores && retrieved && <div className="mi-note">{t('noteScores', zh)}</div>}
        </section>

        {/* ── provenance ───────────────────────────────────────────────────── */}
        <section className="mi-sec">
          <h4 className="mi-h">{t('prov', zh)}<em className="mi-h-sub">{t('atCur', zh)}</em></h4>
          <Row k={t('tags', zh)}><Chips items={st?.tags ?? []} /></Row>
          <Row k={t('assets', zh)}><Chips items={st?.asset_ids ?? []} /></Row>
          <Row k={t('links', zh)}><RelationList rows={relationRows} zh={zh} /></Row>

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
              <span className="mi-op-c" key={op}><b>{OP_LABEL[op]?.[zh ? 0 : 1] ?? op}</b>{n}</span>
            ))}
          </div>
          <svg className="mi-life" viewBox="0 0 560 24" preserveAspectRatio="xMidYMid meet" role="img"
            aria-label={`${chrono.length} events, seq 0 to ${domain}`}>
            <line className="mi-life-ax" x1={4} y1={16} x2={556} y2={16} />
            {chrono.map((e) => (
              <line key={e.seq} className={`mi-life-t${e.seq === cursorSeq ? ' live' : e.seq <= cursorSeq ? ' past' : ''}`}
                x1={lx(e.seq)} y1={6} x2={lx(e.seq)} y2={16}>
                <title>{`seq ${e.seq} · ${OP_LABEL[e.op]?.[zh ? 0 : 1] ?? e.op}`}</title>
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
