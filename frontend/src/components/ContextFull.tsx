/* ── 完整上下文 · FULL CONTEXT VIEWER ─────────────────────────────────────────
 * A full-screen expansion of the ContextPacket: at the replay cursor, show the
 * COMPLETE assembled context handed to the reasoner — every packed memory with
 * full text + all recorded retrieval signals + evidence, the COMPLETE diff vs the
 * previous recall (added / removed / rank±/ score±), what was dropped after recall,
 * and the root cause this context supports. Every value is real recall/record data
 * (core/evolve/observatory.py); nothing synthesized. Shared by the live and the
 * benchmark 长轨迹 (same MemoryObservatory). */
import { useEffect, useMemo } from 'react'
import type { MemRecall, MemRecord, MemTier } from '../types'
import './context-full.css'

const TIER: Record<MemTier, [string, string]> = {
  episodic: ['情景', 'EPISODIC'], semantic: ['语义', 'SEMANTIC'],
  procedural: ['程序', 'PROCEDURAL'], asset_profile: ['资产', 'ASSET'],
}

const T = (zh: boolean) => ({
  title: zh ? '完整上下文 · 此刻递交推理器的全部记忆' : 'FULL CONTEXT · everything handed to the reasoner at this step',
  close: zh ? '解除 ESC' : 'CLOSE ESC',
  caseL: zh ? '案例' : 'CASE', pass: zh ? '轮次' : 'PASS', root: zh ? '根因' : 'ROOT CAUSE',
  inPk: zh ? '进包' : 'in packet', drop: zh ? '丢弃' : 'dropped', probe: zh ? '探针' : 'probes', sc: zh ? '直达命中' : 'SHORTCUT',
  packet: zh ? '包内记忆 · 按最终得分' : 'PACKET · ranked by final score',
  diff: zh ? '本步 DIFF · 较上一次召回' : 'DIFF · vs previous recall',
  add: zh ? '本次新进' : 'ADDED', rm: zh ? '移出上下文' : 'REMOVED',
  dropped: zh ? '召回后被丢弃(预算/去重)' : 'DROPPED AFTER RECALL (budget/dedup)',
  rootSec: zh ? '根因与证据' : 'ROOT CAUSE & EVIDENCE', noEvid: zh ? '该轮无逐条证据快照' : 'no per-item evidence snapshot',
  none: zh ? '无变化' : 'no change', newB: zh ? '新' : 'NEW',
  fAsset: zh ? '资产命中' : 'asset', fPrior: zh ? '结构先验' : 'prior', fHop: zh ? '关联跳' : 'hop',
  fLex: zh ? '词法' : 'lex', fVec: zh ? '向量' : 'vec', fFinal: zh ? '最终' : 'final', fStr: zh ? '强度' : 'str', fConf: zh ? '置信' : 'conf',
  note: zh ? '全部为真实召回/记忆数据(core/evolve/observatory.py);diff、排名与得分变化均按真实 final_score 计算。' : 'All real recall/record data; diff, rank and score deltas computed from real final_score.',
})

export function ContextFull({ recall, prevRecall, records, caseRoot, zh, onClose }: {
  recall: MemRecall
  prevRecall: MemRecall | null
  records: MemRecord[]
  caseRoot?: string
  zh: boolean
  onClose: () => void
}) {
  const l = T(zh)
  useEffect(() => {
    const h = (e: globalThis.KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  const textOf = useMemo(() => new Map(records.map((r) => [r.memory_id, r])), [records])
  const candOf = useMemo(() => new Map(recall.retrieval_candidates.map((c) => [c.memory_id, c])), [recall])
  const prevCandOf = useMemo(() => new Map((prevRecall?.retrieval_candidates ?? []).map((c) => [c.memory_id, c])), [prevRecall])

  const inc = recall.included_memory_ids
  const prevInc = prevRecall?.included_memory_ids ?? []
  const incSet = new Set(inc)
  const prevIncSet = new Set(prevInc)

  const ranked = useMemo(() => inc.map((id) => ({ id, cand: candOf.get(id) ?? null }))
    .sort((a, b) => (b.cand?.final_score ?? 0) - (a.cand?.final_score ?? 0)), [inc, candOf])
  const prevRankOf = useMemo(() => {
    const pr = [...prevInc].map((id) => ({ id, s: prevCandOf.get(id)?.final_score ?? 0 })).sort((a, b) => b.s - a.s)
    return new Map(pr.map((r, i) => [r.id, i + 1] as const))
  }, [prevInc, prevCandOf])

  const added = inc.filter((id) => !prevIncSet.has(id))
  const removed = prevInc.filter((id) => !incSet.has(id))
  const drops = (recall.context_drops && recall.context_drops.length)
    ? recall.context_drops
    : (recall.dropped_memory_ids ?? []).map((id) => ({ memory_id: id, reason: '' as string | undefined }))

  const rootTag = (id: string) => {
    const t = (textOf.get(id)?.tags ?? []).find((x) => x.startsWith('root:'))
    return t ? t.slice(5) : ''
  }
  const rc = caseRoot || rootTag(ranked[0]?.id ?? '') || recall.case_id
  const maxScore = Math.max(1e-6, ...ranked.map((r) => r.cand?.final_score ?? 0))
  const topEvid = textOf.get(ranked[0]?.id ?? '')?.evidence_snapshot ?? []

  return (
    <div className="cf-overlay" role="dialog" aria-label={l.title}>
      <header className="cf-head">
        <div className="cf-title">
          <span className="cf-k">{l.title}</span>
          <span className="cf-meta">{l.caseL} <b>{recall.case_id}</b> · {l.pass} <b>{recall.pass}</b> · {l.root} <mark>{rc}</mark></span>
        </div>
        <div className="cf-stats">
          <span><b>{inc.length}</b> {l.inPk}</span>
          {added.length ? <span className="up"><b>+{added.length}</b> {l.add}</span> : null}
          {removed.length ? <span className="dn"><b>−{removed.length}</b> {l.rm}</span> : null}
          <span><b>{drops.length}</b> {l.drop}</span>
          <span><b>{recall.probes}</b> {l.probe}</span>
          {recall.shortcut ? <span className="sc">{l.sc}</span> : null}
        </div>
        <button className="cf-x" onClick={onClose}>✕ {l.close}</button>
      </header>

      <div className="cf-body">
        <section className="cf-main">
          <div className="cf-sec">{l.packet} · {inc.length}</div>
          {ranked.map(({ id, cand }, i) => {
            const rec = textOf.get(id); const tier = rec?.tier ?? 'episodic'
            const fresh = !prevIncSet.has(id)
            const pr = prevRankOf.get(id); const rd = pr ? pr - (i + 1) : null
            const pscore = prevCandOf.get(id)?.final_score
            const sd = cand && pscore != null ? cand.final_score - pscore : null
            const score = cand?.final_score ?? 0
            return (
              <div key={id} className={`cf-row ${fresh ? 'fresh' : ''}`}>
                <div className="cf-row-h">
                  <span className="cf-rank">{String(i + 1).padStart(2, '0')}</span>
                  <span className={`cf-tier t-${tier}`}>{TIER[tier][zh ? 0 : 1]}</span>
                  <span className="cf-mid">{id}</span>
                  {fresh ? <span className="cf-new">{l.newB}</span> : rd ? <span className={`cf-rd ${rd > 0 ? 'up' : 'dn'}`}>{rd > 0 ? `▲${rd}` : `▼${-rd}`}</span> : null}
                  <span className="cf-fscore">{l.fFinal} <b>{score.toFixed(2)}</b>{sd != null && Math.abs(sd) > 0.001 ? <em className={sd > 0 ? 'up' : 'dn'}>{sd > 0 ? '+' : ''}{sd.toFixed(2)}</em> : null}</span>
                </div>
                <p className="cf-text">{rec?.text ?? id}</p>
                {cand ? (
                  <div className="cf-sig">
                    <span>{l.fAsset} {cand.asset_hits}</span>
                    <span>{l.fPrior} {cand.structural_prior.toFixed(2)}</span>
                    <span>{l.fHop} {cand.graph_hop}</span>
                    <span className={cand.lexical_score ? '' : 'z'}>{l.fLex} {cand.lexical_score.toFixed(2)}</span>
                    <span className={cand.vector_score ? '' : 'z'}>{l.fVec} {cand.vector_score.toFixed(2)}</span>
                    {rec ? <span>{l.fStr} {rec.strength.toFixed(2)}</span> : null}
                    {rec ? <span>{l.fConf} {rec.confidence.toFixed(2)}</span> : null}
                  </div>
                ) : null}
                <span className="cf-bar"><i style={{ width: `${Math.round((score / maxScore) * 100)}%` }} /></span>
                {rec?.evidence_snapshot?.length ? (
                  <div className="cf-evid">
                    {rec.evidence_snapshot.slice(0, 3).map((e, k) => (
                      <div key={k} className="cf-ev"><span className="cf-ev-src">{e.source ?? e.evidence_id ?? ''}</span><span className="cf-ev-sum">{e.summary ?? ''}</span></div>
                    ))}
                  </div>
                ) : null}
              </div>
            )
          })}
        </section>

        <aside className="cf-side">
          <div className="cf-sec">{l.diff}</div>
          <div className="cf-dl">
            {added.length ? <><div className="cf-dl-h up">{l.add} · {added.length}</div>{added.map((id) => <div key={id} className="cf-dl-row"><i>+</i>{textOf.get(id)?.text?.slice(0, 48) ?? id}</div>)}</> : null}
            {removed.length ? <><div className="cf-dl-h dn">{l.rm} · {removed.length}</div>{removed.map((id) => <div key={id} className="cf-dl-row"><i>−</i>{textOf.get(id)?.text?.slice(0, 48) ?? id}</div>)}</> : null}
            {!added.length && !removed.length ? <div className="cf-dl-none">{l.none}</div> : null}
          </div>

          {drops.length ? (
            <>
              <div className="cf-sec">{l.dropped} · {drops.length}</div>
              <div className="cf-dl">
                {drops.map((d) => <div key={d.memory_id} className="cf-dl-row drop">{textOf.get(d.memory_id)?.text?.slice(0, 44) ?? d.memory_id}{d.reason ? ` · ${d.reason}` : ''}</div>)}
              </div>
            </>
          ) : null}

          <div className="cf-sec">{l.rootSec}</div>
          <div className="cf-root">
            <div className="cf-root-k"><mark>{rc}</mark></div>
            {topEvid.length ? topEvid.slice(0, 5).map((e, k) => (
              <div key={k} className="cf-ev"><span className="cf-ev-src">{e.source ?? ''}</span><span className="cf-ev-sum">{e.summary ?? ''}</span></div>
            )) : <div className="cf-dl-none">{l.noEvid}</div>}
          </div>
          <div className="cf-note">{l.note}</div>
        </aside>
      </div>
    </div>
  )
}
