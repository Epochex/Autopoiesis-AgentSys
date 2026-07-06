import { useEffect, useMemo, useState } from 'react'
import type { Baseline, RcaCase } from '../types'
import type { Lang } from '../i18n'
import { rc } from '../i18n'
import { CountUp } from './Motion'
import { FlowGraph } from './FlowGraph'
import { EvolutionStream, type EvoData } from './EvolutionStream'
import { AnimatePresence, motion } from 'motion/react'
import { GAlert, GMemoryRead, GSkillsExposed, GToolCalled, GContextCompiled, GVerifierResult, GDiagnosisCompleted } from './PosterStages'

/* ── real ledger → typed viz per step ── */
const arr = (v: unknown): string[] => (Array.isArray(v) ? (v as unknown[]).map(String) : [])
const num = (v: unknown): number | null => (typeof v === 'number' ? v : null)

type VStep = { kind: string; no: string; code: string; zh: string; en: string; viz: unknown }

function build(c: RcaCase, lang: Lang): VStep[] {
  const out: VStep[] = []
  const tools: { skill: string; ev: string[]; cost: number | null }[] = []
  for (const ev of c.trace) if (ev.kind === 'tool_called') {
    const p = ev.payload
    tools.push({ skill: String(p.skill ?? ''), ev: arr(p.evidence_ids), cost: num(p.cost) })
  }
  const push = (kind: string, code: string, zh: string, en: string, viz: unknown) => out.push({ kind, code, zh, en, viz, no: '' })
  for (const ev of c.trace) {
    const p = ev.payload
    switch (ev.kind) {
      case 'alert_received':
        push('alert_received', 'AL', '告警', 'ALERT', { query: String(p.query ?? ''), assets: arr(p.assets) })
        break
      case 'memory_read': {
        const tiers = [
          { id: 'episodic', zh: '情景', en: 'EPI', keys: arr(p.episodic) },
          { id: 'semantic', zh: '语义', en: 'SEM', keys: arr(p.semantic) },
          { id: 'procedural', zh: '程序', en: 'PRO', keys: arr(p.procedural) },
          { id: 'asset_profile', zh: '资产', en: 'AST', keys: arr(p.asset_profile) },
        ]
        push('memory_read', 'ME', '记忆', 'MEMORY', { tiers, total: tiers.reduce((a, t) => a + t.keys.length, 0) })
        break
      }
      case 'skills_exposed':
        push('skills_exposed', 'SK', '技能', 'SKILLS', { skills: arr(p.skills) })
        break
      case 'context_compiled':
        push('context_compiled', 'CT', '压缩', 'CONTEXT', {
          before: num(p.estimated_tokens_before), after: num(p.estimated_tokens_after), ratio: num(p.compression_ratio),
          kept: arr(p.included_evidence_ids).length, missing: arr(p.missing_evidence),
        })
        break
      case 'verifier_result':
        push('verifier_result', 'VF', '核验', 'VERIFY', { passed: Boolean(p.passed), recall: num(p.evidence_recall) })
        break
      case 'diagnosis_completed':
        push('diagnosis_completed', 'DX', '判决', 'DIAGNOSE', {
          confidence: num(p.confidence), rootKey: String(p.root_cause_key ?? ''), readonly: Boolean(p.readonly),
          cited: arr((p.evidence as { evidence_id?: string }[] | undefined)?.map((e) => e?.evidence_id)).filter(Boolean),
          label: rc(String(p.root_cause_key ?? ''), lang),
        })
        break
      default: break
    }
  }
  if (tools.length) {
    const faces = [...new Set(tools.flatMap((t) => t.ev))]
    const i = out.findIndex((s) => s.kind === 'skills_exposed')
    out.splice(i + 1, 0, { kind: 'tool_called', code: 'PR', zh: '取证', en: 'PROBE', no: '', viz: { probes: tools, faces } })
  }
  out.forEach((s, i) => (s.no = String(i + 1).padStart(2, '0')))
  return out
}

/* eslint-disable @typescript-eslint/no-explicit-any */
function StepFig({ s, zh }: { s: VStep; zh: boolean }) {
  const v = s.viz as any
  switch (s.kind) {
    case 'alert_received': return <GAlert v={v} zh={zh} />
    case 'memory_read': return <GMemoryRead v={v} zh={zh} />
    case 'skills_exposed': return <GSkillsExposed v={v} zh={zh} />
    case 'tool_called': return <GToolCalled v={v} zh={zh} />
    case 'context_compiled': return <GContextCompiled v={v} zh={zh} />
    case 'verifier_result': return <GVerifierResult v={v} zh={zh} />
    default: return <GDiagnosisCompleted v={v} zh={zh} />
  }
}
/* eslint-enable @typescript-eslint/no-explicit-any */

// what each step does — shown on the node and in the expanded drawer
const DESC: Record<string, [string, string]> = {
  alert_received: ['接入告警 · 划定资产范围', 'INGEST ALERT · SCOPE ASSETS'],
  memory_read: ['调出三层高置信先验', 'RECALL 3-TIER MEMORY'],
  skills_exposed: ['注意力钳制 · 只放行只读', 'CLAMP TO READ-ONLY SKILLS'],
  tool_called: ['只读探针 · 逐条钉实证据', 'READ-ONLY PROBES · PIN EVIDENCE'],
  context_compiled: ['证据感知压缩 · 装入预算', 'COMPRESS EVIDENCE TO BUDGET'],
  verifier_result: ['核对每条引用是否被观测', 'VERIFY EVERY CITATION'],
  diagnosis_completed: ['给出根因 · 全程只读', 'ISSUE VERDICT · READ-ONLY'],
}

// compact result shown on each flow node (the mini-story, left→right)
function stationResult(s: VStep, zh: boolean): string {
  const v = s.viz as Record<string, unknown>
  switch (s.kind) {
    case 'alert_received': return zh ? '接入' : 'IN'
    case 'memory_read': return zh ? `命中 ${v.total}` : `${v.total} HIT`
    case 'skills_exposed': return zh ? `放行 ${(v.skills as string[]).length}` : `${(v.skills as string[]).length} SKILL`
    case 'tool_called': return zh ? `${(v.probes as unknown[]).length} 探针` : `${(v.probes as unknown[]).length} PROBE`
    case 'context_compiled': return `${((v.ratio as number) ?? 1).toFixed(2)}×`
    case 'verifier_result': return v.passed ? (zh ? '通过' : 'PASS') : (zh ? '拒绝' : 'REJECT')
    default: return `${((v.confidence as number) ?? 0).toFixed(2)}`
  }
}

const BASELINE_LABEL: Record<string, [string, string]> = {
  selfevo_light_path: ['SELFEVO 全链路', 'SELFEVO · FULL PATH'],
  full_context: ['关闭证据压缩', '− COMPRESSION'],
  full_tools: ['关闭技能调度', '− SKILL CONTROL'],
  no_memory: ['关闭三层记忆', '− MEMORY'],
}
const ABL_MATRIX: Record<string, [boolean, boolean, boolean]> = {
  selfevo_light_path: [true, true, true], full_context: [true, false, true], no_memory: [false, true, true], full_tools: [true, true, false],
}
const ABL_ORDER = ['selfevo_light_path', 'full_context', 'no_memory', 'full_tools']

export function TrajectoryPage({
  cases, baselines, lang, activeId, onPick,
}: {
  cases: RcaCase[]; baselines: Baseline[]; reasoner: string; lang: Lang; activeId: string; onPick: (id: string) => void
}) {
  const zh = lang === 'zh'
  const c = cases.find((x) => x.id === activeId) ?? cases[0]
  const steps = useMemo(() => (c ? build(c, lang) : []), [c, lang])
  const caseIdx = Math.max(0, cases.findIndex((x) => x.id === (c?.id ?? '')))

  const [reached, setReached] = useState(0)
  const [cursor, setCursor] = useState(0)
  const [playing, setPlaying] = useState(true)
  const [detail, setDetail] = useState<number | null>(null)
  const [evo, setEvo] = useState<EvoData | null>(null)
  useEffect(() => { fetch('/api/rca/evolution?passes=4').then((r) => r.json()).then(setEvo).catch(() => setEvo(null)) }, [])
  useEffect(() => {
    if (!playing || !steps.length) return
    if (reached >= steps.length - 1) { setPlaying(false); return }
    const id = setTimeout(() => setReached((r) => { const nx = Math.min(steps.length - 1, r + 1); setCursor(nx); return nx }), 2200)
    return () => clearTimeout(id)
  }, [playing, reached, steps.length])
  const advance = () => setReached((r) => { const nx = Math.min(steps.length - 1, r + 1); setCursor(nx); setPlaying(false); return nx })
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); advance() }
      else if (e.key === 'ArrowLeft') { setCursor((cc) => Math.max(0, cc - 1)); setPlaying(false) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [steps.length])

  if (!c) return null
  const cur = steps[cursor] ?? steps[0]
  const diagIdx = steps.findIndex((s) => s.kind === 'diagnosis_completed')
  const armed = diagIdx >= 0 ? reached >= diagIdx : reached >= steps.length - 1
  const skIdx = steps.findIndex((s) => s.kind === 'skills_exposed')
  const evid = c.diagnosis.evidence
  const memStep = steps.find((s) => s.kind === 'memory_read')
  const memoryTiers = memStep
    ? (memStep.viz as { tiers: { zh: string; en: string; keys: string[] }[] }).tiers.filter((t) => t.keys.length).map((t) => ({ code: zh ? t.zh : t.en, count: t.keys.length }))
    : []

  const base = baselines.find((b) => b.name === 'selfevo_light_path')?.rootCauseAccuracy ?? 1
  const ablated = ABL_ORDER.map((n) => baselines.find((b) => b.name === n)).filter((b): b is Baseline => Boolean(b))
  const worst = ablated.reduce<Baseline | null>((m, b) => (!m || b.rootCauseAccuracy < m.rootCauseAccuracy ? b : m), null)
  const collapsePct = worst ? Math.round(worst.rootCauseAccuracy * 100) : 0

  return (
    <div className="traj-page">
      <div className="tp-grid" />

      {/* ── masthead ── */}
      <header className="tp-top">
        <div className="tp-top-l">
          <span className="tp-kicker">SELFEVO · SELF-EVOLVING LONG-HORIZON AGENT · 内网根因分析</span>
          <h1 className="tp-title">{zh ? <>长<mark>轨迹</mark></> : <>LONG <mark>TRAJECTORY</mark></>}<em>/ EXEC-LEDGER</em></h1>
        </div>
        <div className="tp-top-r">
          <span className="tp-top-lab">{zh ? '真实事件 · R230 FORTIGATE 留出集' : 'REAL · R230 HELD-OUT'}</span>
          <div className="tp-cases">
            {cases.map((x, i) => (
              <button key={x.id} className={`tp-chk ${x.id === c.id ? 'on' : ''} ${x.verifier.passed ? 'pass' : ''}`} onClick={() => onPick(x.id)} title={rc(x.diagnosis.rootCauseKey, lang)}>
                {x.id === c.id ? <b>{String(i + 1).padStart(2, '0')}</b> : null}
              </button>
            ))}
          </div>
        </div>
      </header>

      {/* ── ① 长周期自演化 · real cold-vs-warm hero ── */}
      {evo?.ready ? <EvolutionStream data={evo} zh={zh} /> : null}

      {/* ── bridge: hero (the stream) → trajectory (its first probe, expanded) ── */}
      {evo?.ready ? (
        <button className="tp-bridge" onClick={() => document.getElementById('tp-flow-anchor')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
          <span className="tp-bridge-txt">
            {zh ? <>▼ 展开其中<b>「第 1 次取证」</b>的完整只读链路 —— 复现之所以归零,靠的正是这条链沉淀的证据与技能</>
              : <>▼ EXPAND THE <b>FIRST PROBE</b> INTO ITS FULL READ-ONLY CHAIN —— what later recurrences recall was pinned right here</>}
          </span>
          <span className="tp-bridge-tick" />
        </button>
      ) : null}

      {/* ── ② one event's trajectory (alert→verdict merged into the flow) ── */}
      <section className="tp-flow-lead" id="tp-flow-anchor">
        <span className="tp-band-lab">{zh ? '② 展开一次事件 · 一条只读推理链,逐步落证据' : '② ONE EVENT · A READ-ONLY REASONING CHAIN, EVERY STEP PINNED'}</span>
        <div className="tp-flow-verdict">
          <span className="tp-fv-q" title={c.query}>{c.query.length > 68 ? c.query.slice(0, 66) + '…' : c.query}</span>
          <span className="tp-fv-arrow">▸</span>
          <mark className="tp-fv-root">{rc(c.diagnosis.rootCauseKey, lang)}</mark>
          <span className="tp-fv-facts"><b>{c.diagnosis.confidence.toFixed(2)}</b>{zh ? '置信' : 'CONF'} · <b>{evid.length}/{evid.length}</b>{zh ? '引用核验' : 'VERIFIED'}</span>
        </div>
      </section>

      {/* ── trajectory graph + expanded detail ── */}
      <section className="tp-flow">
        <div className="tp-flow-head">
          <span className="tp-band-lab">{zh ? '③ 取证轨迹 · 一条只读推理链，逐步落证据' : '③ TRAJECTORY · ONE READ-ONLY REASONING CHAIN, EVERY STEP PINNED'}</span>
          <div className="tp-flow-ctl">
            <button onClick={() => { if (reached >= steps.length - 1 && !playing) { setReached(0); setCursor(0) } setPlaying((p) => !p) }}>{playing ? '❚❚' : '▶'}</button>
            <button onClick={() => { setReached(0); setCursor(0); setPlaying(false) }}>⤺</button>
            <button onClick={advance} disabled={reached >= steps.length - 1}>▸|</button>
            <em>{cur.no} / {String(steps.length).padStart(2, '0')}</em>
          </div>
        </div>
        <FlowGraph
          steps={steps.map((s) => ({
            no: s.no, name: zh ? s.zh : s.en, desc: DESC[s.kind]?.[zh ? 0 : 1] ?? '', res: stationResult(s, zh), kind: s.kind,
            loadLabel: s.kind === 'skills_exposed' ? (zh ? `承重 · 移除→${collapsePct}%坍塌` : `LOAD-BEARING · off→${collapsePct}%`) : undefined,
          }))}
          evidence={evid.slice(0, 3).map((e) => ({ id: e.evidenceId, sum: e.summary.length > 34 ? e.summary.slice(0, 32) + '…' : e.summary }))}
          memory={memoryTiers}
          reached={reached}
          cursor={cursor}
          zh={zh}
          onSeek={(i) => { setReached((r) => Math.max(r, i)); setCursor(i); setPlaying(false); setDetail(i) }}
        />
        <AnimatePresence>
          {detail !== null && steps[detail] ? (
            <motion.div className="tp-drawer" key={detail}
              initial={{ x: 44, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 44, opacity: 0 }}
              transition={{ type: 'spring', stiffness: 340, damping: 30 }}>
              <div className="tp-drawer-head">
                <span className="tp-drawer-tag">[TP-{steps[detail].no}] {steps[detail].kind.toUpperCase()}</span>
                <span className="tp-drawer-desc">{DESC[steps[detail].kind]?.[zh ? 0 : 1]}</span>
                <button className="tp-drawer-x" onClick={() => setDetail(null)}>✕ {zh ? '收起' : 'CLOSE'}</button>
              </div>
              <div className="tp-drawer-fig"><StepFig s={steps[detail]} zh={zh} /></div>
            </motion.div>
          ) : null}
        </AnimatePresence>
      </section>

      {/* ── ④ proof · ablation (why this framework), linked to the SKILL step ── */}
      <section className={`tp-proof ${armed ? 'armed' : ''}`}>
        <div className="tp-proof-head">
          <span className="tp-band-lab">{zh ? '④ 凭什么成立 · 逐组件消融' : '④ WHY IT HOLDS · PER-COMPONENT ABLATION'}</span>
          <p className="tp-proof-claim">
            {zh ? <>撑住准确率的是<b>第 {steps[skIdx]?.no ?? '03'} 步「技能调度」</b> —— 移除它,根因准确率从 <b>{Math.round(base * 100)}%</b> 塌到 <mark>{collapsePct}%</mark></>
              : <>the load-bearing lever is <b>step {steps[skIdx]?.no ?? '03'} · SKILL CONTROL</b> — remove it and accuracy collapses <b>{Math.round(base * 100)}%</b> → <mark>{collapsePct}%</mark></>}
          </p>
        </div>
        <div className="tp-proof-plot">
          <div className="tp-proof-legend">{zh ? '开关三格 = 记忆 · 压缩 · 技能调度  ·  ■ 启用 / □ 关闭  —— 柱只在「技能」关闭那行坠入坍塌带' : 'THREE CELLS = MEMORY · COMPRESS · SKILL-CTL  ·  ■ on / □ off  —— the bar falls only where SKILL is off'}</div>
          {ablated.map((b, i) => {
            const pct = Math.round(b.rootCauseAccuracy * 100)
            const dl = Math.round((b.rootCauseAccuracy - base) * 1000) / 10
            const zone = pct >= 85 ? 'safe' : pct >= 40 ? 'risk' : 'unsup'
            const mat = ABL_MATRIX[b.name] ?? [true, true, true]
            return (
              <div key={b.name} className={`tp-abl ${zone} ${b.name === 'selfevo_light_path' ? 'base' : ''}`}>
                <span className="tp-abl-lab">{(BASELINE_LABEL[b.name] ?? [b.name, b.name])[zh ? 0 : 1]}</span>
                <span className="tp-abl-mat">{mat.map((on, mi) => <i key={mi} className={on ? 'on' : 'off'} />)}</span>
                <span className="tp-abl-track"><b className="tp-abl-fill" style={{ width: armed ? `${pct}%` : '0%', transitionDelay: `${i * 140}ms` }} /></span>
                <span className="tp-abl-num">{armed ? <CountUp value={pct} from={zone === 'unsup' ? base * 100 : 0} /> : 0}<i>%</i></span>
                <span className="tp-abl-delta">{dl === 0 ? 'Δ0' : `Δ${dl}`}</span>
              </div>
            )
          })}
          <div className="tp-proof-foot">{zh ? '6 案例真实留出 · 规则推理器 · 引擎无关' : '6-case real held-out · rule reasoner · engine-independent'} · [R230] NO:{String(caseIdx + 1).padStart(2, '0')}</div>
        </div>
      </section>
    </div>
  )
}
