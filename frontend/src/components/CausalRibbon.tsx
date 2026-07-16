import { useMemo } from 'react'
import type { JSX } from 'react'
import type { MemRecall, MemCapabilities } from '../types'
import './memory-timeline.css'

/* ═══════════════════════════════════════════════════════════════════════════
   CAUSAL RIBBON — the payoff chain, read left→right:

     COLD PASS 0  →recall→  RECALLED  →compile→  IN CONTEXT
                  →resolve→ RESOLVED FROM MEMORY
                  →cancel→  PROBES NOT RUN      →verify→ ACCURACY HELD

   Every number is derived from the recall rows / byPass rows handed in — no
   metric is hardcoded, no denominator is invented. The cost claim is shown as
   its own arithmetic (probes actually run vs. probes if every pass had probed
   like pass 0) so the reader sees WHY it got cheaper, not just "−75%".

   Honesty: `capabilities` that are false are printed as kernel limits. The
   dropped-from-context record is shown, but the reason is NOT — the kernel
   never records it (`context_drop_reason: false`).
   ═══════════════════════════════════════════════════════════════════════════ */

const T: Record<string, [string, string]> = {
  title: ['记忆 → 行为：因果链', 'MEMORY → BEHAVIOUR · CAUSAL CHAIN'],
  cold: ['冷启动 · 轮次 0', 'COLD · PASS 0'],
  probes: ['探针', 'PROBES'],
  probesRun: ['探针实跑', 'PROBES RUN'],
  resolved: ['命中记忆', 'RESOLVED'],
  runs: ['运行', 'RUNS'],
  recall: ['召回', 'RECALL'],
  recalled: ['召回记忆', 'RECALLED'],
  perRun: ['每运行', '/RUN'],
  compile: ['编入上下文', 'COMPILE'],
  ctx: ['进入上下文', 'IN CONTEXT'],
  dropped: ['被丢弃', 'DROPPED'],
  reasonNr: ['原因未记录', 'REASON NOT RECORDED'],
  resolveV: ['命中', 'RESOLVE'],
  fromMem: ['来自已链记忆', 'FROM LINKED MEM'],
  cancel: ['免于探测', 'CANCEL'],
  notRun: ['探针未跑', 'PROBES NOT RUN'],
  verify: ['校验', 'VERIFY'],
  acc: ['准确率', 'ACCURACY'],
  held: ['保持', 'HELD'],
  across: ['跨轮次', 'ACROSS'],
  cf: ['若每轮都像轮次 0 那样探测', 'IF EVERY PASS PROBED LIKE P0'],
  vs: ['实跑', 'RAN'],
  accShort: ['准确', 'ACC'],
  limits: ['内核限制 · 未接线能力不作数', 'KERNEL LIMITS · NOT LIVE'],
  decay: ['衰减未接线 · 本轮无遗忘', 'DECAY NOT WIRED · NO FORGETTING'],
  scores: ['无检索打分', 'NO RETRIEVAL SCORES'],
  dropWhy: ['丢弃原因未记录', 'DROP REASON NOT RECORDED'],
  mut: ['无文本改写', 'NO TEXT REWRITE'],
  cfg: ['配置：上下文上限 8 条 / 900 token', 'CONFIG: CTX CAP 8 LINES / 900 TOK'],
  mem: ['记忆条数', 'MEM'],
  none: ['无召回数据', 'NO RECALL DATA'],
}
const t = (k: string, zh: boolean) => T[k][zh ? 0 : 1]

const nRet = (r: MemRecall) => Object.values(r.retrieved).reduce((a, v) => a + (v?.length ?? 0), 0)
const sum = (ns: number[]) => ns.reduce((a, b) => a + b, 0)
/** the shared value if every row agrees, else null — keeps us from faking an average */
const uniform = (ns: number[]): number | null =>
  ns.length && ns.every(n => n === ns[0]) ? ns[0] : null
const perRun = (ns: number[], zh: boolean) => {
  const u = uniform(ns)
  return u !== null ? `${u}${t('perRun', zh)}` : `${(sum(ns) / (ns.length || 1)).toFixed(1)}${t('perRun', zh)}`
}

export function CausalRibbon(props: {
  recall: MemRecall[]
  byPass: { pass: number; probes: number; recalled: number; accuracy: number; memory_end: number }[]
  capabilities: MemCapabilities
  zh: boolean
}): JSX.Element {
  const { recall, byPass, capabilities, zh } = props

  const d = useMemo(() => {
    const passes = [...new Set(recall.map(r => r.pass))].sort((a, b) => a - b)
    const first = passes.length ? passes[0] : 0
    const cold = recall.filter(r => r.pass === first)
    const warm = recall.filter(r => r.pass > first)
    const bp = [...byPass].sort((a, b) => a.pass - b.pass)
    const p0 = bp[0]
    const probesRun = sum(bp.map(b => b.probes))
    // counterfactual: the pass-0 probe bill, repeated for every pass
    const cf = p0 ? p0.probes * bp.length : 0
    const saved = cf > 0 ? 1 - probesRun / cf : null
    const accs = bp.map(b => b.accuracy)
    return {
      cold, warm, bp,
      coldProbes: sum(cold.map(r => r.probes)),
      coldResolved: cold.filter(r => r.resolved).length,
      warmRet: warm.map(nRet),
      warmInc: warm.map(r => r.included_memory_ids.length),
      warmDrop: warm.map(r => r.dropped_memory_ids.length),
      warmResolved: warm.filter(r => r.resolved).length,
      resolvers: new Set(warm.flatMap(r => r.resolved_memory_ids)).size,
      warmProbes: sum(warm.map(r => r.probes)),
      probesRun, cf, saved,
      accUniform: uniform(accs),
      accMin: accs.length ? Math.min(...accs) : null,
    }
  }, [recall, byPass])

  const limits: string[] = []
  if (!capabilities.decay_wired) limits.push(t('decay', zh))
  if (!capabilities.retrieval_scores) limits.push(t('scores', zh))
  if (!capabilities.context_drop_reason) limits.push(`${t('dropWhy', zh)} · ${t('cfg', zh)}`)
  if (!capabilities.update_text_mutation) limits.push(t('mut', zh))

  if (!recall.length) {
    return <section className="cr empty" aria-label={t('title', zh)}><p className="cr-none">{t('none', zh)}</p></section>
  }

  const arrow = (verb: string) => (
    <span className="cr-arrow" aria-hidden="true"><em>{verb}</em><i /></span>
  )

  return (
    <section className="cr" aria-label={t('title', zh)}>
      <div className="cr-chain">
        {/* 1 · the cold pass paid in probes and had nothing to resolve from */}
        <div className="cr-cell">
          <span className="cr-lab">{t('cold', zh)}</span>
          <span className="cr-big">{d.coldProbes}<i>{t('probes', zh)}</i></span>
          <span className="cr-sub">{t('resolved', zh)} {d.coldResolved}/{d.cold.length} {t('runs', zh)}</span>
        </div>

        {arrow(t('recall', zh))}

        {/* 2 · later passes pull the store back */}
        <div className="cr-cell">
          <span className="cr-lab">{t('recalled', zh)}</span>
          <span className="cr-big">{perRun(d.warmRet, zh)}</span>
          <span className="cr-sub">{d.warm.length} {t('runs', zh)} · {sum(d.warmRet)} {zh ? '次' : 'HITS'}</span>
        </div>

        {arrow(t('compile', zh))}

        {/* 3 · what survived into the ContextPacket — the drop is shown, not explained */}
        <div className="cr-cell">
          <span className="cr-lab">{t('ctx', zh)}</span>
          <span className="cr-big">{perRun(d.warmInc, zh)}</span>
          <span className="cr-sub drop">
            <i className="cr-hatch" />−{perRun(d.warmDrop, zh)} {t('dropped', zh)} · {t('reasonNr', zh)}
          </span>
        </div>

        {arrow(t('resolveV', zh))}

        {/* 4 · the case closed off provenance-linked memory */}
        <div className="cr-cell">
          <span className="cr-lab">{t('resolved', zh)}</span>
          <span className="cr-big">{d.warmResolved}<i>/{d.warm.length} {t('runs', zh)}</i></span>
          <span className="cr-sub">{t('fromMem', zh)} · {d.resolvers} ID</span>
        </div>

        {arrow(t('cancel', zh))}

        {/* 5 · therefore the probes pass 0 had to run were never run — the payoff */}
        <div className="cr-cell hot">
          <span className="cr-lab">{t('notRun', zh)}</span>
          <span className="cr-big">{d.coldProbes} → {d.warmProbes}</span>
          <span className="cr-sub">
            {t('vs', zh)} {d.probesRun} / {d.cf} {t('cf', zh)}
            {d.saved !== null ? <b> −{Math.round(d.saved * 100)}%</b> : null}
          </span>
        </div>

        {arrow(t('verify', zh))}

        {/* 6 · and the verifier still passed */}
        <div className="cr-cell">
          <span className="cr-lab">{t('acc', zh)}</span>
          <span className="cr-big">
            {(d.accUniform !== null ? d.accUniform : (d.accMin ?? 0)).toFixed(2)}
            <i>{d.accUniform !== null ? t('held', zh) : (zh ? '最低' : 'MIN')}</i>
          </span>
          <span className="cr-sub">{t('across', zh)} {d.bp.length} {zh ? '轮' : 'PASSES'}</span>
        </div>
      </div>

      <div className="cr-foot">
        <div className="cr-passes">
          {d.bp.map(b => (
            <span key={b.pass} className="cr-p">
              <b>P{b.pass}</b>
              {t('probes', zh)} <em>{b.probes}</em>
              {t('recall', zh)} <em>{b.recalled}</em>
              {t('mem', zh)} <em>{b.memory_end}</em>
              {t('accShort', zh)} <em>{b.accuracy.toFixed(2)}</em>
            </span>
          ))}
        </div>
        {limits.length ? (
          <div className="cr-limits">
            <span className="cr-limits-lead">{t('limits', zh)}</span>
            {limits.map(l => <span key={l} className="cr-limit">{l}</span>)}
          </div>
        ) : null}
      </div>
    </section>
  )
}
