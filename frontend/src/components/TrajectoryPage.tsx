import { useEffect, useMemo, useState } from 'react'
import type { Baseline, RcaCase } from '../types'
import type { Lang } from '../i18n'
import { rc } from '../i18n'
import { Scramble, ConfidenceRing } from './Motion'

/* ── real ledger → ordered trajectory steps ────────────────────────────────
   Every field below is a real payload key emitted by core/trace/ledger.py
   (see core/trace/events.py: 8 TraceKind literals). Nothing is synthesized. */

type Step = {
  kind: string
  no: string
  zh: string
  en: string
  head: string // one-line headline (real payload)
  rows: { k: string; v: string }[]
}

const arr = (v: unknown): string[] => (Array.isArray(v) ? (v as unknown[]).map(String) : [])
const num = (v: unknown): number | null => (typeof v === 'number' ? v : null)

function extract(c: RcaCase, lang: string): Step[] {
  const zh = lang === 'zh'
  const out: Step[] = []
  const tools: { skill: string; ev: string[]; cost: number | null }[] = []
  let n = 0
  const push = (s: Omit<Step, 'no'>) => out.push({ ...s, no: String(++n).padStart(2, '0') })

  for (const ev of c.trace) {
    const p = ev.payload
    if (ev.kind === 'tool_called') {
      tools.push({ skill: String(p.skill ?? ''), ev: arr(p.evidence_ids), cost: num(p.cost) })
    }
  }

  for (const ev of c.trace) {
    const p = ev.payload
    switch (ev.kind) {
      case 'alert_received':
        push({
          kind: ev.kind, zh: '告警接入', en: 'ALERT',
          head: String(p.query ?? ''),
          rows: [{ k: 'assets', v: arr(p.assets).join(' · ') || '—' }],
        })
        break
      case 'memory_read': {
        const tiers: [string, string][] = [
          ['episodic', '情景'], ['semantic', '语义'], ['procedural', '程序'], ['asset_profile', '资产画像'],
        ]
        const rows = tiers
          .map(([k, zhk]) => ({ k: zh ? zhk : k, v: arr(p[k]).join(', ') }))
          .filter((r) => r.v)
        const total = rows.reduce((a, r) => a + r.v.split(',').length, 0)
        push({
          kind: ev.kind, zh: '记忆检索', en: 'MEMORY',
          head: zh ? `三层记忆命中 ${total} 条高置信记忆` : `${total} high-confidence memories across tiers`,
          rows: rows.length ? rows : [{ k: zh ? '命中' : 'hit', v: '—' }],
        })
        break
      }
      case 'skills_exposed': {
        const sk = arr(p.skills)
        push({
          kind: ev.kind, zh: '技能暴露', en: 'SKILLS',
          head: zh ? `注意力控制器仅暴露 ${sk.length} 个只读技能` : `attention controller exposed ${sk.length} read-only skills`,
          rows: sk.map((s) => ({ k: '·', v: s })),
        })
        break
      }
      case 'context_compiled': {
        const ratio = num(p.compression_ratio)
        const before = num(p.estimated_tokens_before)
        const after = num(p.estimated_tokens_after)
        const inclE = arr(p.included_evidence_ids)
        const miss = arr(p.missing_evidence)
        push({
          kind: ev.kind, zh: '证据压缩', en: 'CONTEXT',
          head: before != null && after != null
            ? (zh ? `上下文 ${before} → ${after} tokens 装入预算` : `context ${before} → ${after} tokens into budget`)
            : (zh ? '证据感知压缩' : 'evidence-aware compression'),
          rows: [
            { k: zh ? '压缩比' : 'ratio', v: ratio != null ? ratio.toFixed(2) : '—' },
            { k: zh ? '保留证据' : 'evidence', v: String(inclE.length) },
            { k: zh ? '缺失面' : 'missing', v: miss.length ? miss.join(', ') : (zh ? '无' : 'none') },
          ],
        })
        break
      }
      case 'verifier_result':
        push({
          kind: ev.kind, zh: '引用核验', en: 'VERIFY',
          head: p.passed
            ? (zh ? '每条引用证据均被实际观测 · 通过' : 'every cited evidence was observed · pass')
            : (zh ? '存在未观测/矛盾引用 · 拒绝' : 'unobserved/contradictory citation · reject'),
          rows: [
            { k: zh ? '结论' : 'verdict', v: p.passed ? (zh ? '通过' : 'PASS') : (zh ? '拒绝' : 'REJECT') },
            { k: zh ? '证据召回' : 'recall', v: num(p.evidence_recall) != null ? `${Math.round((num(p.evidence_recall) as number) * 100)}%` : '—' },
          ],
        })
        break
      case 'diagnosis_completed': {
        const conf = num(p.confidence)
        push({
          kind: ev.kind, zh: '根因诊断', en: 'DIAGNOSE',
          head: rc(String(p.root_cause_key ?? ''), lang as Lang),
          rows: [
            { k: zh ? '置信' : 'conf', v: conf != null ? conf.toFixed(2) : '—' },
            { k: zh ? '只读' : 'read-only', v: p.readonly ? '✓' : '✕' },
            { k: zh ? '引用' : 'cited', v: arr((p.evidence as { evidence_id?: string }[] | undefined)?.map((e) => e?.evidence_id)).filter(Boolean).join(' ') || '—' },
          ],
        })
        break
      }
      default:
        break
    }
  }

  // fold the collapsed probe step in after SKILLS
  if (tools.length) {
    const idx = out.findIndex((s) => s.kind === 'skills_exposed')
    const probe: Step = {
      kind: 'tool_called', no: '',
      zh: '并行取证', en: 'PROBE',
      head: zh ? `执行 ${tools.length} 次只读探针，逐条落证据` : `${tools.length} read-only probes, each pinned to evidence`,
      rows: tools.map((t) => ({ k: t.skill, v: t.ev.join(', ') || '—' })),
    }
    out.splice(idx + 1, 0, probe)
  }
  // renumber
  out.forEach((s, i) => (s.no = String(i + 1).padStart(2, '0')))
  return out
}

const BASELINE_LABEL: Record<string, [string, string]> = {
  selfevo_light_path: ['SELFEVO 轻链路', 'SELFEVO · light path'],
  full_context: ['关闭证据压缩', 'no compression'],
  full_tools: ['关闭技能调度', 'all tools exposed'],
  no_memory: ['关闭三层记忆', 'no memory'],
}

export function TrajectoryPage({
  cases, baselines, reasoner, lang, activeId, onPick,
}: {
  cases: RcaCase[]
  baselines: Baseline[]
  reasoner: string
  lang: Lang
  activeId: string
  onPick: (id: string) => void
}) {
  const zh = lang === 'zh'
  const c = cases.find((x) => x.id === activeId) ?? cases[0]
  const steps = useMemo(() => (c ? extract(c, lang) : []), [c, lang])

  const [head, setHead] = useState(0)
  const [playing, setPlaying] = useState(true)
  useEffect(() => { setHead(0); setPlaying(true) }, [activeId, lang])
  useEffect(() => {
    if (!playing || !steps.length) return
    const id = setInterval(() => {
      setHead((h) => {
        if (h >= steps.length - 1) { setPlaying(false); return h }
        return h + 1
      })
    }, 900)
    return () => clearInterval(id)
  }, [playing, steps.length])

  if (!c) return null

  const ctl = baselines.find((b) => b.name === 'selfevo_light_path')
  const maxAcc = Math.max(0.01, ...baselines.map((b) => b.rootCauseAccuracy))

  return (
    <div className="traj-page">
      <div className="tp-scan" />

      {/* ── constructivist masthead ── */}
      <div className="tp-mast">
        <div className="tp-mast-block">
          <span className="tp-idx">01 / EXEC-LEDGER</span>
          <h1 className="tp-title">{zh ? '长轨迹' : 'LONG\nTRAJECTORY'}</h1>
          <span className="tp-sub">{zh ? '可回放执行轨迹 · 统一事实源' : 'replayable execution trace · single source of truth'}</span>
        </div>
        <div className="tp-mast-bar">
          <span className="tp-bar-lab">{zh ? '八类事件 · 逐步落证 · 全程可追溯' : '8 event kinds · every step pinned · fully traceable'}</span>
        </div>
        <div className="tp-cases">
          {cases.map((x, i) => (
            <button key={x.id} className={`tp-case ${x.id === c.id ? 'on' : ''}`} onClick={() => onPick(x.id)}>
              <b>{String(i + 1).padStart(2, '0')}</b>
              <span>{rc(x.diagnosis.rootCauseKey, lang)}</span>
              <i className={x.verifier.passed ? 'ok' : ''} />
            </button>
          ))}
        </div>
      </div>

      <div className="tp-body">
        {/* ── the spine: real ledger, replaying ── */}
        <section className="tp-spine">
          <div className="tp-spine-head">
            <span>{zh ? '轨迹回放' : 'TRAJECTORY REPLAY'}</span>
            <div className="tp-play">
              <button onClick={() => { if (head >= steps.length - 1) setHead(0); setPlaying((p) => !p) }}>
                {playing ? '❚❚' : '▶'}
              </button>
              <button onClick={() => { setHead(0); setPlaying(false) }}>⤺</button>
              <em>{c.id} · {reasoner} · {steps.length} {zh ? '步' : 'steps'}</em>
            </div>
          </div>
          <div className="tp-steps">
            {steps.map((s, i) => {
              const state = i < head ? 'done' : i === head ? 'live' : 'idle'
              const fin = s.kind === 'diagnosis_completed'
              const fail = s.kind === 'verifier_result' && s.rows.some((r) => r.v === 'REJECT' || r.v === '拒绝')
              return (
                <div key={i} className={`tp-step ${state} ${fin ? 'fin' : ''} ${fail ? 'fail' : ''}`}>
                  <div className="tp-no">{s.no}</div>
                  <div className="tp-rail-col"><span className="tp-tick" /></div>
                  <div className="tp-card">
                    <div className="tp-kind">{zh ? s.zh : s.en}<em>{s.kind}</em></div>
                    {state !== 'idle'
                      ? <Scramble className="tp-headline" text={s.head} />
                      : <span className="tp-headline masked">{s.head}</span>}
                    <div className="tp-rows">
                      {s.rows.map((r, k) => (
                        <div key={k} className="tp-row"><span className="tp-k">{r.k}</span><span className="tp-v">{r.v}</span></div>
                      ))}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </section>

        {/* ── stat stack (reconstructed from the old page-1 deck) ── */}
        <aside className="tp-stats">
          <div className="tp-stat ring">
            <ConfidenceRing value={c.diagnosis.confidence} />
            <div className="tp-stat-lab"><b>{zh ? '诊断置信' : 'confidence'}</b><span>{rc(c.diagnosis.rootCauseKey, lang)}</span></div>
          </div>

          <div className="tp-evid">
            <span className="tp-evid-h">{zh ? '可引用证据' : 'cited evidence'}</span>
            {c.diagnosis.evidence.map((e) => (
              <div key={e.evidenceId} className="tp-evid-row">
                <code>{e.evidenceId}</code>
                <span className="tp-evid-src">{e.source}</span>
                <p>{e.summary}</p>
              </div>
            ))}
          </div>

          <div className="tp-ablate">
            <span className="tp-evid-h">{zh ? '逐组件消融 · 根因准确率' : 'component ablation · root-cause acc'}</span>
            {baselines.map((b) => {
              const on = b.name === 'selfevo_light_path'
              return (
                <div key={b.name} className={`tp-abl-row ${on ? 'on' : ''}`}>
                  <span className="tp-abl-lab">{(BASELINE_LABEL[b.name] ?? [b.name, b.name])[zh ? 0 : 1]}</span>
                  <span className="tp-abl-track"><b style={{ width: `${(b.rootCauseAccuracy / maxAcc) * 100}%` }} /></span>
                  <span className="tp-abl-num">{Math.round(b.rootCauseAccuracy * 100)}<i>%</i></span>
                </div>
              )
            })}
            {ctl ? (
              <div className="tp-abl-foot">
                <span>{zh ? '证据召回' : 'recall'} {Math.round(ctl.evidenceRecall * 100)}%</span>
                <span>{zh ? '核验通过' : 'verify'} {Math.round(ctl.verifierPassRate * 100)}%</span>
                <span>{ctl.cases} {zh ? '案例' : 'cases'}</span>
              </div>
            ) : null}
          </div>
        </aside>
      </div>
    </div>
  )
}
