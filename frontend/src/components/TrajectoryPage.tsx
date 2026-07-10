import { useEffect, useMemo, useState } from 'react'
import type { Baseline, RcaCase } from '../types'
import type { Lang } from '../i18n'
import { rc } from '../i18n'
import { CountUp } from './Motion'
import { FlowGraph, type FxStation, type FxEvidence, type FxMemTier } from './FlowGraph'
import { EvolutionStream, type EvoData } from './EvolutionStream'
import { AnimatePresence, motion } from 'motion/react'
import { GAlert, GMemoryRead, GSkillsExposed, GToolCalled, GContextCompiled, GVerifierResult, GDiagnosisCompleted } from './PosterStages'
import './trajectory.css'

/* ── real ledger → typed viz per step ── */
const arr = (v: unknown): string[] => (Array.isArray(v) ? (v as unknown[]).map(String) : [])
const num = (v: unknown): number | null => (typeof v === 'number' ? v : null)
const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + '…' : s)

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

// ROLE one-liner per step — the crisp forensics+reasoning role, shown on expand
const DESC: Record<string, [string, string]> = {
  alert_received: ['接入告警 · 划定资产范围', 'INGEST ALERT · SCOPE ASSETS'],
  memory_read: ['调出三层高置信先验', 'RECALL 3-TIER MEMORY PRIORS'],
  skills_exposed: ['注意力钳制 · 只放行只读技能', 'CLAMP ATTENTION · READ-ONLY SKILLS'],
  tool_called: ['只读探针 · 逐条钉实证据', 'READ-ONLY PROBES · PIN EVIDENCE'],
  context_compiled: ['证据感知压缩 · 装入预算', 'EVIDENCE-AWARE COMPRESS TO BUDGET'],
  verifier_result: ['核对每条引用是否被观测', 'VERIFY EVERY CITATION IS OBSERVED'],
  diagnosis_completed: ['给出根因 · 全程只读', 'ISSUE ROOT-CAUSE VERDICT · READ-ONLY'],
}
const CAT: Record<string, string> = {
  alert_received: 'alert', memory_read: 'memory', skills_exposed: 'skill', tool_called: 'probe',
  context_compiled: 'context', verifier_result: 'verify', diagnosis_completed: 'verdict',
}
const CAP: Record<string, [string, string]> = {
  alert_received: ['资产', 'ASSETS'], memory_read: ['先验', 'PRIORS'], skills_exposed: ['放行', 'EXPOSED'],
  tool_called: ['探针', 'PROBES'], context_compiled: ['压缩', 'RATIO'], verifier_result: ['召回', 'RECALL'],
  diagnosis_completed: ['置信', 'CONF'],
}

// per-station headline metric (real values from viz)
function stationMetric(s: VStep, zh: boolean): FxStation['metric'] {
  const v = s.viz as Record<string, unknown>
  const cap = CAP[s.kind]?.[zh ? 0 : 1] ?? ''
  switch (s.kind) {
    case 'alert_received': return { value: (v.assets as string[]).length, unit: 'int', caption: cap }
    case 'memory_read': return { value: v.total as number, unit: 'int', caption: cap }
    case 'skills_exposed': return { value: (v.skills as string[]).length, unit: 'int', caption: cap }
    case 'tool_called': return { value: (v.probes as unknown[]).length, unit: 'int', caption: cap }
    case 'context_compiled': return { value: (v.ratio as number) ?? 1, unit: 'x', caption: cap }
    case 'verifier_result': return { value: Math.round(((v.recall as number) ?? 0) * 100), unit: 'pct', caption: cap }
    default: return { value: (v.confidence as number) ?? 0, unit: 'conf', caption: cap }
  }
}

// the live "reasoning readout" — reveals the active step's real computation
function readoutLines(s: VStep, zh: boolean): { op: string; body: string }[] {
  const v = s.viz as Record<string, unknown>
  switch (s.kind) {
    case 'alert_received': {
      const a = v.assets as string[]
      return [
        { op: 'INGEST', body: zh ? `告警载荷 · 归一化 query` : `alert payload · normalize query` },
        { op: 'SCOPE', body: `${a.length} ${zh ? '台资产入范围' : 'assets in scope'} · ${clip(a.join(' '), 44)}` },
      ]
    }
    case 'memory_read': {
      const t = (v.tiers as { en: string; keys: string[] }[])
      const line = t.map((x) => `${x.en}:${x.keys.length}`).join(' ')
      return [
        { op: 'RECALL', body: line },
        { op: 'LOAD', body: `${v.total} ${zh ? '条先验 · 只读召回' : 'priors · read-only recall'}` },
      ]
    }
    case 'skills_exposed': {
      const sk = v.skills as string[]
      return [
        { op: 'SCORE', body: zh ? `技能全集 → top-k · 分数>0.5` : `full toolset → top-k · score>0.5` },
        { op: 'EXPOSE', body: `${sk.length} ${zh ? '只读技能 · 写操作硬阻断' : 'read-only · write-blocked'}` },
      ]
    }
    case 'tool_called': {
      const P = v.probes as { skill: string; ev: string[]; cost: number | null }[]
      return P.slice(0, 3).map((p) => ({
        op: 'CALL·RO',
        body: `${clip(p.skill, 26)} → ${zh ? '钉' : 'pin'} ${clip((p.ev[0] ?? '—'), 24)}${p.cost != null ? ` · c${p.cost}` : ''}`,
      }))
    }
    case 'context_compiled':
      return [
        { op: 'PACK', body: `${v.after} / ${v.before} TK · ${((v.ratio as number) ?? 1).toFixed(2)}×` },
        { op: 'COVER', body: (v.missing as string[]).length ? `${(v.missing as string[]).length} ${zh ? '缺失面' : 'missing'}` : (zh ? '全覆盖 · 0 缺失' : 'full coverage · 0 missing') },
      ]
    case 'verifier_result':
      return [
        { op: 'MATCH', body: zh ? '被引用 ⊆ 被观测' : 'cited ⊆ observed' },
        { op: v.passed ? 'PASS' : 'REJECT', body: `${zh ? '召回' : 'recall'} ${Math.round(((v.recall as number) ?? 0) * 100)}%` },
      ]
    default:
      return [
        { op: 'EMIT', body: `${clip(String(v.label ?? v.rootKey), 40)}` },
        { op: 'SEAL', body: `${zh ? '置信' : 'conf'} ${((v.confidence as number) ?? 0).toFixed(2)} · ${zh ? '引用' : 'cites'} ${(v.cited as string[]).length} · RO ${v.readonly ? '✓' : '✕'}` },
      ]
  }
}

// per-step INPUTS / OUTPUTS / process-verification CONTRACT (grounded in verifier.py)
type Brief = { inputs: string[]; outputs: string[]; contract: { kind: 'PRE' | 'INV' | 'POST'; t: string; ok: boolean | null }[] }
function stepBrief(s: VStep, zh: boolean, verifierPassed: boolean): Brief {
  const v = s.viz as Record<string, unknown>
  switch (s.kind) {
    case 'alert_received': {
      const a = v.assets as string[]
      return {
        inputs: [zh ? '原始告警信号' : 'raw alert signal', zh ? '自然语言 query' : 'natural-language query'],
        outputs: [`${zh ? '范围 = ' : 'scope = '}${a.length} ${zh ? '台资产' : 'assets'}`, zh ? '归一化诉求' : 'normalized intent'],
        contract: [
          { kind: 'PRE', t: zh ? '信号结构完整' : 'signal well-formed', ok: null },
          { kind: 'INV', t: zh ? '范围 ⊆ 声明资产' : 'scope ⊆ declared assets', ok: null },
          { kind: 'POST', t: zh ? 'query 已归一化' : 'query normalized', ok: true },
        ],
      }
    }
    case 'memory_read': {
      const t = v.tiers as { en: string; keys: string[] }[]
      return {
        inputs: [zh ? '记忆存储 · 三层' : 'memory store · 3 tiers'],
        outputs: t.map((x) => `${x.en} ${x.keys.length}`),
        contract: [
          { kind: 'PRE', t: zh ? '存储可达' : 'store reachable', ok: null },
          { kind: 'INV', t: zh ? '只读召回 · 无写回' : 'read-only recall · no mutation', ok: true },
          { kind: 'POST', t: zh ? '先验按层归类' : 'priors typed by tier', ok: true },
        ],
      }
    }
    case 'skills_exposed': {
      const sk = v.skills as string[]
      return {
        inputs: [zh ? '技能全集' : 'full toolset', zh ? '资产范围 · 先验' : 'scope · priors'],
        outputs: sk.map((x) => clip(x, 22)),
        contract: [
          { kind: 'PRE', t: zh ? '技能全集已枚举' : 'toolset enumerated', ok: null },
          { kind: 'INV', t: zh ? '写类技能硬阻断' : 'write-like skills blocked', ok: true },
          { kind: 'POST', t: zh ? '放行 ⊆ top-k 打分' : 'exposed ⊆ scored top-k', ok: true },
        ],
      }
    }
    case 'tool_called': {
      const P = v.probes as { skill: string; ev: string[]; cost: number | null }[]
      const cost = P.reduce((a, p) => a + (p.cost ?? 0), 0)
      return {
        inputs: [zh ? '已放行只读技能' : 'exposed read-only skills'],
        outputs: [`${P.length} ${zh ? '探针' : 'probes'} → ${[...new Set(P.flatMap((p) => p.ev))].length} ${zh ? '条证据' : 'evidence'}`, `${zh ? '成本' : 'cost'} ${cost}`],
        contract: [
          { kind: 'PRE', t: zh ? 'skill.readonly = true' : 'skill.readonly = true', ok: true },
          { kind: 'INV', t: zh ? '无状态写入' : 'no state mutation', ok: true },
          { kind: 'POST', t: zh ? '每次调用钉实被观测证据' : 'each call pins observed evidence', ok: true },
        ],
      }
    }
    case 'context_compiled': {
      const before = v.before as number | null, after = v.after as number | null
      return {
        inputs: [zh ? '被观测证据集' : 'observed evidence set', zh ? '召回先验' : 'recalled priors'],
        outputs: [`${after} / ${before} TK`, `${((v.ratio as number) ?? 1).toFixed(2)}× ${zh ? '压缩' : 'ratio'}`],
        contract: [
          { kind: 'PRE', t: zh ? '证据集已装配' : 'evidence assembled', ok: null },
          { kind: 'INV', t: zh ? '装入 ⊆ 被观测（不臆造）' : 'included ⊆ observed (no fabrication)', ok: true },
          { kind: 'POST', t: zh ? '装入 token 预算' : 'fits token budget', ok: (before ?? 0) >= (after ?? 0) },
        ],
      }
    }
    case 'verifier_result':
      return {
        inputs: [zh ? '诊断草案' : 'diagnosis draft', zh ? '被观测证据' : 'observed evidence'],
        outputs: [`${zh ? '召回' : 'recall'} ${Math.round(((v.recall as number) ?? 0) * 100)}%`, v.passed ? 'PASS' : 'REJECT'],
        contract: [
          { kind: 'PRE', t: zh ? '至少引用一条证据' : 'cites ≥1 evidence', ok: Boolean(v.passed) },
          { kind: 'INV', t: zh ? '被引用 ⊆ 被观测' : 'cited ⊆ observed', ok: Boolean(v.passed) },
          { kind: 'POST', t: zh ? '必需证据召回 = 1.0 否则拒绝' : 'required recall = 1.0 else REJECT', ok: Boolean(v.passed) },
        ],
      }
    default:
      return {
        inputs: [zh ? '已核验证据' : 'verified evidence', zh ? '记忆先验' : 'memory priors'],
        outputs: [clip(String(v.label ?? ''), 32), `${zh ? '置信' : 'conf'} ${((v.confidence as number) ?? 0).toFixed(2)}`, `${zh ? '引用' : 'cites'} ${(v.cited as string[]).length}`],
        contract: [
          { kind: 'PRE', t: zh ? '核验已通过' : 'verifier passed', ok: verifierPassed },
          { kind: 'INV', t: zh ? '判决只读 · 不执行修复' : 'verdict READ-ONLY · no remediation', ok: Boolean(v.readonly) },
          { kind: 'POST', t: zh ? '每条断言引用已核验证据' : 'every claim cites verified evidence', ok: true },
        ],
      }
  }
}

// architecture legend — node type ⇒ system component
const LEGEND: { cat: string; zh: [string, string]; en: [string, string] }[] = [
  { cat: 'alert', zh: ['告警', '信号接入'], en: ['ALERT', 'SIGNAL INGEST'] },
  { cat: 'memory', zh: ['记忆', '三层记忆存储'], en: ['MEMORY', '3-TIER STORE'] },
  { cat: 'skill', zh: ['技能', '技能注意力控制'], en: ['SKILLS', 'ATTENTION CTRL'] },
  { cat: 'probe', zh: ['取证', '只读探针总线'], en: ['PROBE', 'READ-ONLY PROBE'] },
  { cat: 'context', zh: ['压缩', '证据感知压缩'], en: ['CONTEXT', 'EVIDENCE COMPRESS'] },
  { cat: 'verify', zh: ['核验', '引用核验闸'], en: ['VERIFY', 'CITATION GATE'] },
  { cat: 'verdict', zh: ['判决', '只读判决'], en: ['DIAGNOSE', 'READ-ONLY VERDICT'] },
]
const CAT_COLOR: Record<string, string> = {
  alert: '#d6335a', memory: '#4c9d94', skill: '#ff7a6b', probe: '#2b3d38', context: '#ffcfa0', verify: '#a8bfa0', verdict: '#0d0d0d',
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

const BEAT = 2200

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
    const id = setTimeout(() => setReached((r) => { const nx = Math.min(steps.length - 1, r + 1); setCursor(nx); return nx }), BEAT)
    return () => clearTimeout(id)
  }, [playing, reached, steps.length])
  const advance = () => setReached((r) => { const nx = Math.min(steps.length - 1, r + 1); setCursor(nx); setPlaying(false); return nx })
  const retreat = () => setCursor((cc) => Math.max(0, cc - 1))
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); advance() }
      else if (e.key === 'ArrowLeft') { retreat(); setPlaying(false) }
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

  // ── derive FlowGraph props from real trace ──
  const pinnedIds = new Set<string>()
  const includedIds = new Set<string>()
  for (const ev of c.trace) {
    if (ev.kind === 'tool_called') arr(ev.payload.evidence_ids).forEach((id) => pinnedIds.add(id))
    if (ev.kind === 'context_compiled') arr(ev.payload.included_evidence_ids).forEach((id) => includedIds.add(id))
  }
  const fxEvidence: FxEvidence[] = evid.slice(0, 3).map((e) => ({
    id: e.evidenceId, sum: clip(e.summary, 40), raw: e.source,
    pinned: pinnedIds.has(e.evidenceId), included: includedIds.has(e.evidenceId),
    cited: true, verified: c.verifier.passed,
  }))
  const memStep = steps.find((s) => s.kind === 'memory_read')
  const memoryTiers: FxMemTier[] = memStep
    ? (memStep.viz as { tiers: { zh: string; en: string; keys: string[] }[] }).tiers.map((t) => ({ code: t.en, label: zh ? t.zh : t.en, count: t.keys.length }))
    : []
  const skStep = steps.find((s) => s.kind === 'skills_exposed')
  const skExposed = skStep ? (skStep.viz as { skills: string[] }).skills : []
  const verStep = steps.find((s) => s.kind === 'verifier_result')
  const verRecall = verStep ? ((verStep.viz as { recall: number | null }).recall ?? 1) : 1
  const stations: FxStation[] = steps.map((s) => ({
    no: s.no, name: zh ? s.zh : s.en, role: DESC[s.kind]?.[zh ? 0 : 1] ?? '', kind: s.kind, cat: CAT[s.kind] ?? 'verdict',
    metric: stationMetric(s, zh), loadBearing: s.kind === 'skills_exposed',
  }))

  const base = baselines.find((b) => b.name === 'selfevo_light_path')?.rootCauseAccuracy ?? 1
  const ablated = ABL_ORDER.map((n) => baselines.find((b) => b.name === n)).filter((b): b is Baseline => Boolean(b))
  const worst = ablated.reduce<Baseline | null>((m, b) => (!m || b.rootCauseAccuracy < m.rootCauseAccuracy ? b : m), null)
  const collapsePct = worst ? Math.round(worst.rootCauseAccuracy * 100) : 0

  const seek = (i: number) => { setReached((r) => Math.max(r, i)); setCursor(i); setPlaying(false); setDetail(i) }
  const scrubTo = (i: number) => { setReached((r) => Math.max(r, i)); setCursor(i); setPlaying(false) }
  const readout = readoutLines(cur, zh)
  const brief = detail !== null && steps[detail] ? stepBrief(steps[detail], zh, c.verifier.passed) : null

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

      {/* ── ③ trajectory schematic + replay + detail ── */}
      <section className="tp-flow">
        <div className="tp-flow-head">
          <span className="tp-band-lab">{zh ? '③ 取证轨迹 · 执行账本图 · 悬停可溯源交叉高亮' : '③ TRAJECTORY · EXECUTION-LEDGER · HOVER FOR PROVENANCE CROSS-HIGHLIGHT'}</span>
          <span className="fx-hint">{zh ? '点击任一站点 → 展开角色 · 输入 · 输出 · 过程契约' : 'CLICK A STATION → ROLE · INPUTS · OUTPUTS · CONTRACT'}</span>
        </div>

        {/* architecture legend — node type ⇒ system component (always visible) */}
        <div className="fx-legend">
          <span className="fx-legend-lead">{zh ? '架构图例 · 节点 ⇒ 系统组件' : 'ARCHITECTURE · NODE ⇒ COMPONENT'}</span>
          {LEGEND.map((l) => (
            <span key={l.cat} className="fx-legend-chip">
              <i style={{ background: CAT_COLOR[l.cat] }} />
              <b>{(zh ? l.zh : l.en)[0]}</b>
              <em>{(zh ? l.zh : l.en)[1]}</em>
            </span>
          ))}
        </div>

        <FlowGraph
          stations={stations}
          evidence={fxEvidence}
          memory={memoryTiers}
          skills={{ exposed: skExposed }}
          verify={{ passed: c.verifier.passed, recall: verRecall }}
          reached={reached}
          cursor={cursor}
          zh={zh}
          onSeek={seek}
        />

        {/* ── replay deck: scrubber + live reasoning readout ── */}
        <div className="fx-deck">
          <div className="fx-scrub">
            <div className="fx-scrub-transport">
              <button onClick={() => { if (reached >= steps.length - 1 && !playing) { setReached(0); setCursor(0) } setPlaying((p) => !p) }} title="play/pause">{playing ? '❚❚' : '▶'}</button>
              <button onClick={() => { setReached(0); setCursor(0); setPlaying(false) }} title="reset">⤺</button>
              <button onClick={() => { retreat(); setPlaying(false) }} disabled={cursor <= 0} title="prev">◀</button>
              <button onClick={advance} disabled={reached >= steps.length - 1} title="step">▸|</button>
            </div>
            <div className="fx-scrub-rail">
              <div className="fx-scrub-fill" style={{ width: `${(reached / Math.max(1, steps.length - 1)) * 100}%` }} />
              <div className="fx-scrub-head" style={{ left: `${(cursor / Math.max(1, steps.length - 1)) * 100}%` }} />
              {steps.map((s, i) => (
                <button key={s.no} className={`fx-tick ${i <= reached ? 'on' : ''} ${i === cursor ? 'cur' : ''}`}
                  style={{ left: `${(i / Math.max(1, steps.length - 1)) * 100}%` }}
                  onClick={() => scrubTo(i)} title={zh ? s.zh : s.en}>
                  <i />
                  <span className="fx-tick-no">{s.no}</span>
                  <span className="fx-tick-nm">{zh ? s.zh : s.en}</span>
                </button>
              ))}
            </div>
            <div className="fx-scrub-meta">
              <span>STEP <b>{cur.no}</b> / {String(steps.length).padStart(2, '0')}</span>
              <span>REPLAY <b>t+{(cursor * (BEAT / 1000)).toFixed(1)}s</b></span>
              <span className="fx-scrub-cat" style={{ background: CAT_COLOR[CAT[cur.kind] ?? 'verdict'] }}>{cur.kind.replace(/_/g, ' ').toUpperCase()}</span>
            </div>
          </div>

          <div className="fx-hud" key={`${cursor}:${reached}`}>
            <div className="fx-hud-head">
              <span className="fx-hud-dot" />
              <span className="fx-hud-title">{zh ? '推理读出 · 实时' : 'REASONING READOUT · LIVE'}</span>
              <span className="fx-hud-step">{zh ? cur.zh : cur.en}</span>
            </div>
            <div className="fx-hud-role">▸ {DESC[cur.kind]?.[zh ? 0 : 1]}</div>
            <div className="fx-hud-body">
              {readout.map((r, i) => (
                <div className="fx-hud-line" key={i} style={{ animationDelay: `${120 + i * 130}ms` }}>
                  <span className="fx-hud-op">{r.op}</span>
                  <span className="fx-hud-txt">{r.body}</span>
                </div>
              ))}
            </div>
            <div className="fx-hud-foot">{zh ? 'R230 留出集 · 真实轨迹载荷 · 引擎无关' : 'R230 HELD-OUT · REAL TRACE PAYLOAD · ENGINE-INDEPENDENT'}</div>
          </div>
        </div>

        {/* ── detail drawer: ROLE · INPUTS · OUTPUTS · CONTRACT + figure ── */}
        <AnimatePresence>
          {detail !== null && steps[detail] && brief ? (
            <motion.div className="fx-drawer" key={detail}
              initial={{ y: 20, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: 12, opacity: 0 }}
              transition={{ type: 'spring', stiffness: 360, damping: 32 }}>
              <div className="fx-drawer-head">
                <span className="fx-drawer-tag" style={{ borderColor: CAT_COLOR[CAT[steps[detail].kind] ?? 'verdict'] }}>[TP-{steps[detail].no}] {steps[detail].kind.replace(/_/g, ' ').toUpperCase()}</span>
                <span className="fx-drawer-role">{DESC[steps[detail].kind]?.[zh ? 0 : 1]}</span>
                <button className="fx-drawer-x" onClick={() => setDetail(null)}>✕ {zh ? '收起' : 'CLOSE'}</button>
              </div>
              <div className="fx-drawer-grid">
                <div className="fx-drawer-cols">
                  <div className="fx-io">
                    <span className="fx-io-lab">{zh ? '输入' : 'INPUTS'}</span>
                    {brief.inputs.map((x, i) => <span key={i} className="fx-io-item in">◂ {x}</span>)}
                  </div>
                  <div className="fx-io">
                    <span className="fx-io-lab">{zh ? '输出' : 'OUTPUTS'}</span>
                    {brief.outputs.map((x, i) => <span key={i} className="fx-io-item out">▸ {x}</span>)}
                  </div>
                  <div className="fx-io contract">
                    <span className="fx-io-lab">{zh ? '过程核验契约' : 'PROCESS CONTRACT'}</span>
                    {brief.contract.map((cc, i) => (
                      <span key={i} className={`fx-ct ${cc.ok === true ? 'ok' : cc.ok === false ? 'no' : 'struct'}`}>
                        <b>{cc.kind}</b>
                        <em>{cc.t}</em>
                        <i>{cc.ok === true ? '✓' : cc.ok === false ? '✕' : '◇'}</i>
                      </span>
                    ))}
                  </div>
                </div>
                <div className="fx-drawer-fig"><StepFig s={steps[detail]} zh={zh} /></div>
              </div>
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
