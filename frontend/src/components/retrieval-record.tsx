/* ── PAGE 4 · 混合检索 — the interactive CASE RECORD ───────────────────────────
 * Renders one real retrieval run as a readable, replayable record: real doc text,
 * why-matched terms highlighted, real scores, real fusion/gate, real assembled
 * context. Steps reveal as the replay plays; any doc expands in place to its full
 * text + journey; hovering/selecting a doc lights it everywhere it appears. */
import type { Case, RtHit, RtTierHit } from './retrieval-data'
import { VIA_LABEL, journeyOf, markMatched } from './retrieval-data'

function MatchedText({ text, matched }: { text: string; matched: string[] }) {
  return <>{markMatched(text, matched).map((p, i) => (p.hit ? <mark key={i} className="rt-mk">{p.t}</mark> : <span key={i}>{p.t}</span>))}</>
}

function HitRow({ c, h, zh, sel, onSel, tone }: { c: Case; h: RtHit | RtTierHit; zh: boolean; sel: string | null; onSel: (id: string | null) => void; tone?: string }) {
  const on = sel === h.doc_id
  const inCtx = c.context.selected.some((s) => s.doc_id === h.doc_id)
  const via = (h as RtTierHit).via
  return (
    <div className={`rt-hit${on ? ' on' : ''}${sel && !on ? ' dim' : ''}`} onMouseEnter={() => onSel(h.doc_id)} onMouseLeave={() => onSel(null)}>
      <button className="rt-hit-head" onClick={() => onSel(on ? null : h.doc_id)} aria-expanded={on}>
        <span className="rt-hit-rank">{h.rank}</span>
        <span className="rt-hit-title">{h.title}</span>
        {via?.length ? <span className="rt-hit-via">{via.map((v) => <i key={v} className={`v-${v}`}>{zh ? VIA_LABEL[v].zh : VIA_LABEL[v].en}</i>)}</span> : null}
        {inCtx ? <span className="rt-hit-inctx" title={zh ? '本次实际采用' : 'used this time'}>→ {zh ? '已采用' : 'USED'}</span> : null}
        <span className="rt-hit-score" style={tone ? { color: tone } : undefined}>{h.score >= 10 ? h.score.toFixed(1) : h.score.toFixed(h.score >= 2 ? 1 : 3)}</span>
      </button>
      <div className="rt-hit-snip"><MatchedText text={on ? h.text : h.snippet || h.text.slice(0, 96)} matched={h.matched} /></div>
      {h.matched.length ? (
        <div className="rt-hit-why"><span>{zh ? '命中' : 'matched'}</span>{h.matched.map((m) => <code key={m}>{m}</code>)}</div>
      ) : via?.includes('graph') ? <div className="rt-hit-why"><span>{zh ? '沿关系找到' : 'found by following a link'}</span></div> : null}
      {on ? (
        <div className="rt-hit-journey">
          {journeyOf(c, h.doc_id, zh).map((b, i) => (
            <span key={i} className={`rt-jb${b.on ? ' on' : ''}`}><i>{b.k}</i>{b.v}</span>
          ))}
        </div>
      ) : null}
    </div>
  )
}

export function CaseRecord({ c, step, zh, sel, onSel }: { c: Case; step: number; zh: boolean; sel: string | null; onSel: (id: string | null) => void }) {
  const kb = c.flow === 'kb_hybrid'
  const S = (i: number, cls = '') => `rt-step ${cls}${step >= i ? ' in' : ''}${step === i ? ' now' : ''}`

  return (
    <div className="rt-record">
      {/* step 0 · query (+ expand for KB) */}
      <section className={S(0)}>
        <div className="rt-step-k">01 · {zh ? '查询' : 'QUERY'}</div>
        <p className="rt-q">{c.query}</p>
        <div className="rt-q-meta"><span className="rt-tier-tag">{zh ? '查找方式' : 'LOOKUP'}</span>{zh ? c.intent.zh : c.intent.en}</div>
        {kb && c.query_expansions?.length ? (
          <div className="rt-exp"><span>{zh ? '扩写' : 'EXPAND'} →</span>{c.query_expansions.map((e, i) => <code key={i}>{e}</code>)}</div>
        ) : null}
        <div className="rt-trig">{c.triggers.count}× · {c.triggers.live ? (zh ? '真实运行' : 'LIVE') : (zh ? '仅台架' : 'EVAL-ONLY')} · <em>{zh ? c.triggers.note.zh : c.triggers.note.en}</em></div>
      </section>

      {kb ? (
        <>
          {/* routes */}
          <section className={S(1)}>
            <div className="rt-step-k">02 · {zh ? '同时用三种方法查资料' : 'SEARCH THREE WAYS AT ONCE'}</div>
            {(c.routes ?? []).map((r) => (
              <div key={r.id} className="rt-route-blk">
                <div className="rt-route-hd">{zh ? r.label.zh : r.label.en}{r.enabled ? <em>{zh ? `命中 ${r.hits.length}` : `${r.hits.length} hits`}</em> : <em className="off">{zh ? '离线' : 'offline'}</em>}</div>
                {r.enabled ? r.hits.map((h) => <HitRow key={h.doc_id} c={c} h={h} zh={zh} sel={sel} onSel={onSel} />)
                  : <div className="rt-offline">{r.note}</div>}
              </div>
            ))}
          </section>
          {/* fusion */}
          <section className={S(2)}>
            <div className="rt-step-k">03 · {zh ? '把三路结果合到一起' : 'MERGE THE THREE RESULT LISTS'}</div>
            <div className="rt-formula">score(d) = Σ 1 / (c + rankᵣ) · c={c.fusion?.c ?? 60}</div>
            {(c.fusion?.ranked ?? []).map((f) => {
              const on = sel === f.doc_id
              return (
                <div key={f.doc_id} className={`rt-fuse${on ? ' on' : ''}${sel && !on ? ' dim' : ''}`} onMouseEnter={() => onSel(f.doc_id)} onMouseLeave={() => onSel(null)} onClick={() => onSel(on ? null : f.doc_id)}>
                  <span className="rt-hit-rank">{f.rank}</span><span className="rt-hit-title">{f.title}</span>
                  <span className="rt-fuse-src">{f.from_routes.join(' + ')}</span>
                  <span className="rt-hit-score">{f.rrf_score.toFixed(4)}</span>
                </div>
              )
            })}
          </section>
          {/* gate */}
          <section className={S(3)}>
            <div className="rt-step-k">04 · {zh ? '检查结果够不够可靠' : 'CHECK WHETHER THE RESULTS ARE RELIABLE'}</div>
            {c.gate ? (
              <div className={`rt-gate v-${c.gate.verdict}`}>
                <span className="rt-gate-v">{c.gate.verdict.toUpperCase()}</span>
                <span className="rt-gate-top">{zh ? '最高分' : 'top score'} {c.gate.top_score.toFixed(2)} · {zh ? '采用' : 'used'} {c.gate.kept.length}</span>
                <p className="rt-gate-why">{c.gate.reason}</p>
              </div>
            ) : null}
          </section>
          {/* rerank */}
          <section className={S(4)}>
            <div className="rt-step-k">05 · {zh ? '重新排序' : 'SORT AGAIN'}</div>
            {c.rerank?.enabled ? c.rerank.ordered.map((o) => (
              <div key={o.doc_id} className={`rt-rr${sel === o.doc_id ? ' on' : ''}`} onMouseEnter={() => onSel(o.doc_id)} onMouseLeave={() => onSel(null)}>
                <span className="rt-hit-rank">{o.rank}</span><span className="rt-hit-title">{c.docs[o.doc_id]?.title ?? o.doc_id}</span>
                <span className={`rt-delta ${o.delta > 0 ? 'up' : o.delta < 0 ? 'down' : 'flat'}`}>{o.delta > 0 ? `▲${o.delta}` : o.delta < 0 ? `▼${Math.abs(o.delta)}` : '·'}</span>
              </div>
            )) : <div className="rt-offline">{c.rerank?.note} · {zh ? '沿用合并后的顺序' : 'using the merged order'}</div>}
          </section>
        </>
      ) : (
        <>
          {/* tiers */}
          <section className={S(1)}>
            <div className="rt-step-k">02 · {zh ? '按记录类型分别查找' : 'SEARCH EACH RECORD TYPE'}</div>
            {(c.tiers ?? []).map((t) => (
              <div key={t.id} className="rt-route-blk">
                <div className="rt-route-hd">{zh ? t.label.zh : t.label.en}<em>{t.hits.length ? (zh ? `命中 ${t.hits.length}` : `${t.hits.length}`) : (zh ? '无命中' : 'none')}</em></div>
                {t.hits.map((h) => <HitRow key={h.doc_id} c={c} h={h} zh={zh} sel={sel} onSel={onSel} />)}
              </div>
            ))}
          </section>
          {/* graph-hop */}
          <section className={S(2)}>
            <div className="rt-step-k">03 · {zh ? '沿已有关系继续查找' : 'FOLLOW EXISTING LINKS'} <em>{zh ? `最多 ${c.graph?.hops ?? 0} 层关系` : `up to ${c.graph?.hops ?? 0} links away`}</em></div>
            {c.graph?.expanded.length ? c.graph.expanded.map((e, i) => (
              <div key={i} className="rt-hop" onMouseEnter={() => onSel(e.doc_id)} onMouseLeave={() => onSel(null)}>
                <span className="rt-hop-from">{c.docs[e.from]?.title ?? e.from}</span>
                <span className="rt-hop-rel">—{e.relation} · hop{e.hop}→</span>
                <span className="rt-hop-to">{c.docs[e.doc_id]?.title ?? e.doc_id}</span>
              </div>
            )) : <div className="rt-offline">{zh ? '这次没有继续找到内容，已找到的记录之间没有可沿用的关系。' : 'Nothing else was found because the matching records have no links to follow.'}</div>}
          </section>
        </>
      )}

      {/* context — the payload actually handed downstream */}
      <section className={S(kb ? 5 : 3)}>
        <div className="rt-step-k">{kb ? '06' : '04'} · {zh ? '实际交给下游的内容' : 'WHAT DOWNSTREAM ACTUALLY GETS'}</div>
        <ContextTray c={c} sel={sel} onSel={onSel} filled />
      </section>
    </div>
  )
}

export function ContextTray({ c, sel, onSel, filled }: { c: Case; sel: string | null; onSel: (id: string | null) => void; filled: boolean }) {
  const pct = Math.round((c.context.total_tokens / c.context.budget_tokens) * 100)
  return (
    <div className="rt-ctx">
      <div className="rt-ctx-bar"><span className="rt-ctx-fill" style={{ width: filled ? `${Math.min(100, pct)}%` : '0%' }} /></div>
      <div className="rt-ctx-cap">{c.context.total_tokens}<i>/{c.context.budget_tokens}t · {pct}%</i></div>
      {c.context.selected.map((sd, i) => {
        const on = sel === sd.doc_id
        return (
          <div key={sd.doc_id} className={`rt-passage${on ? ' on' : ''}${sel && !on ? ' dim' : ''}${filled ? ' in' : ''}`} style={{ transitionDelay: `${0.08 * i}s` }}
            onMouseEnter={() => onSel(sd.doc_id)} onMouseLeave={() => onSel(null)}>
            <div className="rt-passage-hd"><span className="rt-passage-t">{sd.title}</span><span className="rt-passage-tok">{sd.tokens}t</span></div>
            <div className="rt-passage-why">{sd.reason}</div>
            <div className="rt-passage-txt">{sd.text}</div>
          </div>
        )
      })}
    </div>
  )
}
