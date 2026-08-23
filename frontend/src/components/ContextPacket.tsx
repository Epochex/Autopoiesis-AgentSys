/* ── 上下文包 · CONTEXT PACKET ────────────────────────────────────────────────
 * What the replay was missing: you could watch memory FILL, but not watch the
 * CONTEXT that memory assembles for each run — what actually gets handed to the
 * reasoner, how it changes run-to-run, and why each memory earned its place.
 *
 * This panel follows the replay cursor. At each recall it shows the assembled
 * packet as ranked rows: the memory's real text (content, not just an id), its
 * tier, and the retrieval score that ranked it — decomposed into the signals the
 * kernel actually recorded (asset hits · structural prior · graph hops · lexical
 * · vector). It diffs against the PREVIOUS recall so the growth is legible: rows
 * new this step are marked, dropped memories are listed with the kernel's own
 * reason, and the header states the count delta. Everything is real recall data
 * (core/evolve/observatory.py) joined to real record text — nothing synthesized.
 *
 * Honesty: on the R230 held-out set retrieval is asset+structural driven, so
 * lexical/vector are 0 for most rows. That is shown, not hidden — it is the true
 * shape of how this context gets built. */
import { useMemo, useState } from 'react'
import type { MemCapabilities, MemRecall, MemRecord, MemTier } from '../types'
import { ContextFull } from './ContextFull'
import './context-packet.css'

const TIER: Record<MemTier, [string, string]> = {
  episodic: ['遇到过的事', 'SEEN BEFORE'],
  semantic: ['归纳的规律', 'PATTERN'],
  procedural: ['处理办法', 'HOW-TO'],
  asset_profile: ['设备资料', 'ASSET INFO'],
}

const L = (zh: boolean) => ({
  title: zh ? '本次排查用到的记录' : 'RECORDS USED FOR THIS CHECK',
  sub: zh ? '跟随回放，显示每轮找到了什么、采用了什么，以及和上轮有什么变化' : 'Follows the replay: what each run found, what it used, and what changed since the last run',
  none: zh ? '当前位置只有写入动作，本次排查没有读取已有记录。' : 'This position only writes a record; no existing records were read for the check.',
  empty: zh ? '当前没有可用的已有记录，回放刚开始时会出现这种情况。' : 'No existing records are available yet. This is expected at the start of the replay.',
  case: zh ? '案例' : 'CASE', pass: zh ? '轮次' : 'PASS',
  packet: zh ? '实际采用' : 'USED',
  grew: zh ? '较上次' : 'vs last',
  same: zh ? '与上次持平' : 'unchanged vs last',
  first: zh ? '首次查找' : 'first lookup',
  newRow: zh ? '本次新进' : 'NEW',
  dropped: zh ? '找到但没采用' : 'FOUND BUT NOT USED',
  score: zh ? '最终得分' : 'final',
  asset: zh ? '设备匹配' : 'asset match', prior: zh ? '固定加分' : 'fixed boost', hop: zh ? '关系距离' : 'link distance',
  lex: zh ? '文字匹配' : 'text match', vec: zh ? '意思相近' : 'meaning match',
  shortcut: zh ? '直达命中' : 'SHORTCUT', probes: zh ? '探针' : 'probes',
  note: zh
    ? '这组留出案例主要靠设备匹配和固定加分找到记录，文字匹配与意思相近分数大多为 0。'
    : 'On these held-out cases, records are found mainly through asset matches and fixed boosts; text and meaning-match scores are mostly 0.',
})

export function ContextPacket({
  recall, prevRecall, records, capabilities, caseRoot, zh,
}: {
  recall: MemRecall | null
  prevRecall: MemRecall | null
  records: MemRecord[]
  capabilities: MemCapabilities
  caseRoot?: string
  zh: boolean
}) {
  const l = L(zh)
  const [full, setFull] = useState(false)
  const textOf = useMemo(() => {
    const m = new Map<string, MemRecord>()
    for (const r of records) m.set(r.memory_id, r)
    return m
  }, [records])

  // rows = the assembled packet, ranked by the real final_score (desc). Fall back
  // to included-id order when a candidate score is missing.
  const rows = useMemo(() => {
    if (!recall) return []
    const byId = new Map(recall.retrieval_candidates.map((c) => [c.memory_id, c]))
    const prevInc = new Set(prevRecall?.included_memory_ids ?? [])
    return recall.included_memory_ids
      .map((id) => ({ id, cand: byId.get(id) ?? null, fresh: !prevInc.has(id) }))
      .sort((a, b) => (b.cand?.final_score ?? 0) - (a.cand?.final_score ?? 0))
  }, [recall, prevRecall])

  const maxScore = useMemo(() => Math.max(1e-6, ...rows.map((r) => r.cand?.final_score ?? 0)), [rows])

  const drops = recall?.context_drops ?? []
  const delta = recall && prevRecall ? recall.included_memory_ids.length - prevRecall.included_memory_ids.length : null

  return (
    <section className="cp" aria-label={zh ? '本次排查用到的记录' : 'Records used for this check'}>
      <header className="cp-head">
        <div className="cp-head-l">
          <span className="cp-title">{l.title}</span>
          <span className="cp-sub">{l.sub}</span>
        </div>
        {recall ? (
          <div className="cp-head-r">
            <span className="cp-meta">{l.case} {recall.case_id}</span>
            <span className="cp-meta">{l.pass} {recall.pass}</span>
            <span className="cp-count"><b>{recall.included_memory_ids.length}</b> {l.packet}</span>
            <span className={`cp-delta ${delta && delta > 0 ? 'up' : ''}`}>
              {delta === null ? l.first : delta > 0 ? `${l.grew} +${delta}` : l.same}
            </span>
            {recall.shortcut && <span className="cp-badge">{l.shortcut}</span>}
            <span className="cp-badge dim">{l.probes} {recall.probes}</span>
            <button className="cp-full" onClick={() => setFull(true)}>{zh ? '查看全部 ▸' : 'VIEW ALL ▸'}</button>
          </div>
        ) : null}
      </header>

      {!recall ? (
        <div className="cp-msg">{l.none}</div>
      ) : rows.length === 0 ? (
        <div className="cp-msg">{l.empty}</div>
      ) : (
        <>
          <div className="cp-rows">
            {rows.map(({ id, cand, fresh }, i) => {
              const rec = textOf.get(id)
              const tier = rec?.tier ?? 'episodic'
              const score = cand?.final_score ?? 0
              return (
                <div key={id} className={`cp-row ${fresh ? 'fresh' : ''}`}>
                  <span className="cp-rank">{String(i + 1).padStart(2, '0')}</span>
                  <span className={`cp-tier t-${tier}`}>{TIER[tier][zh ? 0 : 1]}</span>
                  <div className="cp-body">
                    <p className="cp-text">{rec?.text ?? id}</p>
                    {cand ? (
                      <div className="cp-signals">
                        <span className="cp-sig strong">{l.score} <b>{score.toFixed(2)}</b></span>
                        <span className="cp-sig">{l.asset} {cand.asset_hits}</span>
                        <span className="cp-sig">{l.prior} {cand.structural_prior.toFixed(2)}</span>
                        <span className="cp-sig">{l.hop} {cand.graph_hop}</span>
                        <span className={`cp-sig ${cand.lexical_score ? '' : 'zero'}`}>{l.lex} {cand.lexical_score.toFixed(2)}</span>
                        <span className={`cp-sig ${cand.vector_score ? '' : 'zero'}`}>{l.vec} {cand.vector_score.toFixed(2)}</span>
                      </div>
                    ) : null}
                  </div>
                  <div className="cp-score-col">
                    {fresh ? <span className="cp-new">{l.newRow}</span> : null}
                    <span className="cp-bar"><i style={{ width: `${Math.round((score / maxScore) * 100)}%` }} /></span>
                  </div>
                </div>
              )
            })}
          </div>

          {drops.length > 0 ? (
            <div className="cp-drops">
              <span className="cp-drops-h">{l.dropped}</span>
              {drops.map((d) => (
                <span key={d.memory_id} className="cp-drop" title={d.reason}>
                  {textOf.get(d.memory_id)?.text?.slice(0, 40) ?? d.memory_id}{d.reason ? ` · ${d.reason}` : ''}
                </span>
              ))}
            </div>
          ) : null}

          {capabilities.retrieval_scores ? <div className="cp-note">{l.note}</div> : null}
        </>
      )}
      {full && recall ? (
        <ContextFull recall={recall} prevRecall={prevRecall} records={records} caseRoot={caseRoot} zh={zh} onClose={() => setFull(false)} />
      ) : null}
    </section>
  )
}
