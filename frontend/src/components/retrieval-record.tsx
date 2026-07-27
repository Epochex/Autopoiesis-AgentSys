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
        {inCtx ? <span className="rt-hit-inctx" title={zh ? '进入上下文' : 'in context'}>→ {zh ? '上下文' : 'CTX'}</span> : null}
        <span className="rt-hit-score" style={tone ? { color: tone } : undefined}>{h.score >= 10 ? h.score.toFixed(1) : h.score.toFixed(h.score >= 2 ? 1 : 3)}</span>
      </button>
      <div className="rt-hit-snip"><MatchedText text={on ? h.text : h.snippet || h.text.slice(0, 96)} matched={h.matched} /></div>
      {h.matched.length ? (
        <div className="rt-hit-why"><span>{zh ? '命中' : 'matched'}</span>{h.matched.map((m) => <code key={m}>{m}</code>)}</div>
      ) : via?.includes('graph') ? <div className="rt-hit-why"><span>{zh ? '经图跳带入' : 'via graph-hop'}</span></div> : null}
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
        <div className="rt-q-meta"><span className="rt-tier-tag">{zh ? '路由' : 'ROUTED'} · {c.intent.tier}</span>{zh ? c.intent.zh : c.intent.en}</div>
        {kb && c.query_expansions?.length ? (
          <div className="rt-exp"><span>{zh ? '扩写' : 'EXPAND'} →</span>{c.query_expansions.map((e, i) => <code key={i}>{e}</code>)}</div>
        ) : null}
        <div className="rt-trig">{c.triggers.count}× · {c.triggers.live ? (zh ? '真实运行' : 'LIVE') : (zh ? '仅台架' : 'EVAL-ONLY')} · <em>{zh ? c.triggers.note.zh : c.triggers.note.en}</em></div>
      </section>

      {kb ? (
        <>
          {/* routes */}
          <section className={S(1)}>
            <div className="rt-step-k">02 · {zh ? '三路并行召回' : 'PARALLEL ROUTES'}</div>
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
            <div className="rt-step-k">03 · {zh ? 'RRF 融合' : 'RRF FUSION'}</div>
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
            <div className="rt-step-k">04 · {zh ? 'CRAG 置信闸门' : 'CRAG GATE'}</div>
            {c.gate ? (
              <div className={`rt-gate v-${c.gate.verdict}`}>
                <span className="rt-gate-v">{c.gate.verdict.toUpperCase()}</span>
                <span className="rt-gate-top">{zh ? '首位置信' : 'top'} {c.gate.top_score.toFixed(2)} · {zh ? '保留' : 'kept'} {c.gate.kept.length}</span>
                <p className="rt-gate-why">{c.gate.reason}</p>
              </div>
            ) : null}
          </section>
          {/* rerank */}
          <section className={S(4)}>
            <div className="rt-step-k">05 · {zh ? '交叉编码重排' : 'CROSS-ENCODER RERANK'}</div>
            {c.rerank?.enabled ? c.rerank.ordered.map((o) => (
              <div key={o.doc_id} className={`rt-rr${sel === o.doc_id ? ' on' : ''}`} onMouseEnter={() => onSel(o.doc_id)} onMouseLeave={() => onSel(null)}>
                <span className="rt-hit-rank">{o.rank}</span><span className="rt-hit-title">{c.docs[o.doc_id]?.title ?? o.doc_id}</span>
                <span className={`rt-delta ${o.delta > 0 ? 'up' : o.delta < 0 ? 'down' : 'flat'}`}>{o.delta > 0 ? `▲${o.delta}` : o.delta < 0 ? `▼${Math.abs(o.delta)}` : '·'}</span>
              </div>
            )) : <div className="rt-offline">{c.rerank?.note} · {zh ? '沿用融合序' : 'passthrough (fused order)'}</div>}
          </section>
        </>
      ) : (
        <>
          {/* tiers */}
          <section className={S(1)}>
            <div className="rt-step-k">02 · {zh ? '分层记忆召回' : 'TIERED MEMORY RECALL'}</div>
            {(c.tiers ?? []).map((t) => (
              <div key={t.id} className="rt-route-blk">
                <div className="rt-route-hd">{zh ? t.label.zh : t.label.en}<em>{t.hits.length ? (zh ? `命中 ${t.hits.length}` : `${t.hits.length}`) : (zh ? '无命中' : 'none')}</em></div>
                {t.hits.map((h) => <HitRow key={h.doc_id} c={c} h={h} zh={zh} sel={sel} onSel={onSel} />)}
              </div>
            ))}
          </section>
          {/* graph-hop */}
          <section className={S(2)}>
            <div className="rt-step-k">03 · {zh ? '图跳扩展' : 'GRAPH-HOP EXPANSION'} <em>{zh ? `深度 ${c.graph?.hops ?? 0}` : `depth ${c.graph?.hops ?? 0}`}</em></div>
            {c.graph?.expanded.length ? c.graph.expanded.map((e, i) => (
              <div key={i} className="rt-hop" onMouseEnter={() => onSel(e.doc_id)} onMouseLeave={() => onSel(null)}>
                <span className="rt-hop-from">{c.docs[e.from]?.title ?? e.from}</span>
                <span className="rt-hop-rel">—{e.relation} · hop{e.hop}→</span>
                <span className="rt-hop-to">{c.docs[e.doc_id]?.title ?? e.doc_id}</span>
              </div>
            )) : <div className="rt-offline">{zh ? '本次未扩展 —— 命中记忆之间无关系边可跳。' : 'no expansion — no relation edges between the hits to hop along.'}</div>}
          </section>
        </>
      )}

      {/* context — the payload actually handed downstream */}
      <section className={S(kb ? 5 : 3)}>
        <div className="rt-step-k">{kb ? '06' : '04'} · {zh ? '组装上下文 · 实际喂给下游的内容' : 'ASSEMBLED CONTEXT · what downstream actually gets'}</div>
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
