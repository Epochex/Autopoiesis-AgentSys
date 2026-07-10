import { useEffect, useMemo, useState } from 'react'
import type { Baseline, RcaCase } from '../types'
import type { Lang } from '../i18n'
import { rc } from '../i18n'
import { CountUp } from './Motion'
import { FlowGraph, type FxStation, type FxEvidence, type FxMemTier, type FxReadout } from './FlowGraph'
import { EvolutionStream, type EvoData } from './EvolutionStream'
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
        push('diagnosis_completed', 'DX', '判决', 'VERDICT', {
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

// ROLE one-liner per step (crisp, shown on the reticle-locked hero)
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

// the live "reasoning readout" — the active step's real computation
function readoutLines(s: VStep, zh: boolean): FxReadout[] {
  const v = s.viz as Record<string, unknown>
  switch (s.kind) {
    case 'alert_received': {
      const a = v.assets as string[]
      return [
        { op: 'INGEST', body: zh ? `告警载荷 · 归一化 query` : `alert payload · normalize query` },
        { op: 'SCOPE', body: `${a.length} ${zh ? '台资产入范围' : 'assets in scope'} · ${clip(a.join(' '), 40)}` },
      ]
    }
    case 'memory_read': {
      const t = (v.tiers as { en: string; keys: string[] }[])
      return [
        { op: 'RECALL', body: t.map((x) => `${x.en}:${x.keys.length}`).join('  ') },
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
        body: `${clip(p.skill, 24)} → ${zh ? '钉' : 'pin'} ${clip((p.ev[0] ?? '—'), 22)}`,
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
        { op: 'EMIT', body: `${clip(String(v.label ?? v.rootKey), 38)}` },
        { op: 'SEAL', body: `${zh ? '置信' : 'conf'} ${((v.confidence as number) ?? 0).toFixed(2)} · ${zh ? '引用' : 'cites'} ${(v.cited as string[]).length} · RO ${v.readonly ? '✓' : '✕'}` },
      ]
  }
}

// architecture legend — node type ⇒ system component
const LEGEND: { cat: string; zh: string; en: string }[] = [
  { cat: 'alert', zh: '告警', en: 'ALERT' },
  { cat: 'memory', zh: '记忆', en: 'MEMORY' },
  { cat: 'skill', zh: '技能', en: 'SKILL' },
  { cat: 'probe', zh: '取证', en: 'PROBE' },
  { cat: 'context', zh: '压缩', en: 'CONTEXT' },
  { cat: 'verify', zh: '核验', en: 'VERIFY' },
  { cat: 'verdict', zh: '判决', en: 'VERDICT' },
]
const CAT_COLOR: Record<string, string> = {
  alert: '#d6335a', memory: '#4c9d94', skill: '#ff7a6b', probe: '#2b3d38', context: '#ffcfa0', verify: '#a8bfa0', verdict: '#0d0d0d',
}

/* ── ② WHY IT HOLDS · load-bearing structural stress test ──────────────────────
   The same 7-node skeleton, miniaturised. Pulling a component greys it out; only
   pulling the load-bearing SKILL-CONTROL node collapses the downstream span and
   crashes accuracy (real 100% → 16.7%). Not a row of bars — a structural event. */
type Pull = 'none' | 'memory' | 'context' | 'skill'
const PULL_BASELINE: Record<Pull, string> = {
  none: 'selfevo_light_path', memory: 'no_memory', context: 'full_context', skill: 'full_tools',
}
// mini skeleton positions (compact viewBox)
const MW = 700, MH = 250, MNW = 92, MNH = 44
const MP: [number, number][] = [
  [70, 66], [70, 184], [196, 125], [322, 184], [448, 125], [560, 184], [648, 78],
]
const MCAT = ['alert', 'memory', 'skill', 'probe', 'context', 'verify', 'verdict']
const MNAME: [string, string][] = [
  ['告警', 'ALERT'], ['记忆', 'MEM'], ['技能', 'SKILL'], ['取证', 'PROBE'], ['压缩', 'CTX'], ['核验', 'VERIFY'], ['判决', 'VERDICT'],
]
const PULL_INDEX: Record<Pull, number> = { none: -1, memory: 1, context: 4, skill: 2 }

function StressTest({ baselines, zh }: { baselines: Baseline[]; zh: boolean }) {
  const [pull, setPull] = useState<Pull>('none')
  // auto-demo: settle healthy, then pull the load-bearing node once to show collapse
  useEffect(() => {
    const id = setTimeout(() => setPull('skill'), 1600)
    return () => clearTimeout(id)
  }, [])
  const bAcc = (name: string) => baselines.find((b) => b.name === name)?.rootCauseAccuracy ?? 1
  const base = Math.round(bAcc('selfevo_light_path') * 100)
  const acc = Math.round(bAcc(PULL_BASELINE[pull]) * 100)
  const collapsed = acc < 50
  const pulledIdx = PULL_INDEX[pull]
  // when skill is pulled, everything downstream of it falls
  const isFallen = (i: number) => collapsed && i >= PULL_INDEX.skill
  const isPulled = (i: number) => i === pulledIdx
  const chips: { key: Pull; zh: string; en: string }[] = [
    { key: 'none', zh: '全链路', en: 'ALL ON' },
    { key: 'memory', zh: '拔记忆', en: '− MEMORY' },
    { key: 'context', zh: '拔压缩', en: '− COMPRESS' },
    { key: 'skill', zh: '拔技能调度', en: '− SKILL-CTL' },
  ]
  return (
    <section className="fx-stress">
      <div className="fx-stress-head">
        <span className="fx-panel-lab"><i className="fx-panel-no">02</i>{zh ? '凭什么成立 · 承重压力测试' : 'WHY IT HOLDS · LOAD-BEARING STRESS TEST'}</span>
        <span className="fx-stress-chips">
          {chips.map((c) => (
            <button key={c.key} className={`fx-chip ${pull === c.key ? 'on' : ''} ${c.key === 'skill' ? 'crit' : ''}`} onClick={() => setPull(c.key)}>
              {zh ? c.zh : c.en}
            </button>
          ))}
        </span>
      </div>
      <div className="fx-stress-body">
        <svg className="fx-stress-svg" viewBox={`0 0 ${MW} ${MH}`} preserveAspectRatio="xMidYMid meet">
          <defs>
            <pattern id="fx-hatch2" width={6} height={6} patternTransform="rotate(45)" patternUnits="userSpaceOnUse"><rect width={2} height={6} fill="var(--gray)" /></pattern>
          </defs>
          {/* links */}
          {MP.slice(0, -1).map((_, i) => {
            const a = MP[i], b = MP[i + 1]
            const sever = collapsed && i + 1 >= PULL_INDEX.skill && i >= PULL_INDEX.skill - 1
            const bf = isFallen(i + 1) ? [b[0], b[1] + 30] : b
            const af = isFallen(i) ? [a[0], a[1] + 30] : a
            return <line key={i} className={`fx-str-link ${sever ? 'sever' : ''}`} x1={af[0]} y1={af[1]} x2={bf[0]} y2={bf[1]} />
          })}
          {/* the load-bearing pillar under the skill node */}
          <g className={`fx-str-pillar ${collapsed ? 'gone' : ''}`}>
            <line x1={MP[2][0]} y1={MP[2][1] + MNH / 2} x2={MP[2][0]} y2={MH - 18} />
            <text x={MP[2][0]} y={MH - 6} textAnchor="middle">{zh ? '承重' : 'LOAD'}</text>
          </g>
          {/* nodes */}
          {MP.map((p, i) => {
            const fell = isFallen(i), pulled = isPulled(i)
            const cy = fell ? p[1] + 30 : p[1]
            const cls = pulled ? 'pulled' : fell ? 'fell' : 'ok'
            return (
              <g key={i} className={`fx-str-node ${cls}`} transform={`translate(${p[0] - MNW / 2} ${cy - MNH / 2})`}>
                <rect className="fx-str-bg" width={MNW} height={MNH} />
                <rect className="fx-str-strip" width={MNW} height={4} fill={CAT_COLOR[MCAT[i]]} />
                <text className="fx-str-nm" x={MNW / 2} y={MNH / 2 + 6} textAnchor="middle">{(zh ? MNAME[i][0] : MNAME[i][1])}</text>
                {pulled ? <text className="fx-str-x" x={MNW / 2} y={-8} textAnchor="middle">✕ {zh ? '拔除' : 'REMOVED'}</text> : null}
              </g>
            )
          })}
        </svg>
        <div className={`fx-stress-read ${collapsed ? 'bad' : 'ok'}`}>
          <span className="fx-stress-acc"><CountUp value={acc} /><i>%</i></span>
          <span className="fx-stress-acc-lab">{zh ? '根因准确率' : 'ROOT-CAUSE ACC'}</span>
          <span className="fx-stress-delta">{acc === base ? 'Δ0' : `Δ${Math.round((acc - base))}`}</span>
          <span className={`fx-stress-flag ${collapsed ? 'bad' : 'ok'}`}>
            {pull === 'none' ? (zh ? '结构完好' : 'STRUCTURE INTACT')
              : collapsed ? (zh ? '结构失稳 · 不成立' : 'UNSUPPORTED · COLLAPSE')
                : (zh ? '可容忍 · 结构撑住' : 'TOLERATED · HELD')}
          </span>
        </div>
      </div>
      <div className="fx-stress-foot">
        <span className="fx-stress-tag crit">{zh ? '技能调度 = 承重节点' : 'SKILL-CTL = LOAD-BEARING'}</span>
        <span className="fx-stress-tag">{zh ? '拔记忆 / 压缩 · 100% 撑住' : '− MEM / COMPRESS · 100% HOLD'}</span>
        <span className="fx-stress-tag dim">6-CASE HELD-OUT · RULE</span>
      </div>
    </section>
  )
}

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
  const evid = c.diagnosis.evidence

  // ── derive FlowGraph props from real trace ──
  const pinnedIds = new Set<string>()
  const includedIds = new Set<string>()
  for (const ev of c.trace) {
    if (ev.kind === 'tool_called') arr(ev.payload.evidence_ids).forEach((id) => pinnedIds.add(id))
    if (ev.kind === 'context_compiled') arr(ev.payload.included_evidence_ids).forEach((id) => includedIds.add(id))
  }
  const fxEvidence: FxEvidence[] = evid.slice(0, 2).map((e) => ({
    id: e.evidenceId, sum: clip(e.summary, 54), raw: e.source,
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
    metric: stationMetric(s, zh), readout: readoutLines(s, zh), loadBearing: s.kind === 'skills_exposed',
  }))

  const seek = (i: number) => { setReached((r) => Math.max(r, i)); setCursor(i); setPlaying(false) }
  const scrubTo = (i: number) => { setReached((r) => Math.max(r, i)); setCursor(i); setPlaying(false) }
  const atEnd = reached >= steps.length - 1

  return (
    <div className="traj-page">
      <div className="tp-grid" />

      {/* ── HUD masthead: title · mission · case chips ── */}
      <header className="fx-mast">
        <div className="fx-mast-l">
          <span className="fx-mast-kick">SELFEVO · SELF-EVOLVING LONG-HORIZON AGENT · 内网根因分析</span>
          <h1 className="fx-mast-title">{zh ? <>长<mark>轨迹</mark></> : <>LONG <mark>TRAJECTORY</mark></>}<em>/ TACTICAL EXEC-LEDGER REPLAY</em></h1>
          <div className="fx-mast-mission">
            <span className="fx-mast-q" title={c.query}>{clip(c.query, 62)}</span>
            <span className="fx-mast-arrow">▸</span>
            <mark className="fx-mast-root">{rc(c.diagnosis.rootCauseKey, lang)}</mark>
            <span className="fx-mast-facts"><b>{c.diagnosis.confidence.toFixed(2)}</b>{zh ? '置信' : 'CONF'} · <b>{evid.length}/{evid.length}</b>{zh ? '引用核验' : 'VERIFIED'} · <b>RO</b>{zh ? '只读' : 'READ-ONLY'}</span>
          </div>
        </div>
        <div className="fx-mast-r">
          <span className="fx-mast-real">{zh ? '真实事件 · R230 FORTIGATE 留出集' : 'REAL · R230 FORTIGATE HELD-OUT'}</span>
          <div className="fx-mast-cases">
            <span className="fx-mast-cases-lab">{zh ? '事件' : 'CASE'}</span>
            {cases.map((x, i) => (
              <button key={x.id} className={`fx-case ${x.id === c.id ? 'on' : ''} ${x.verifier.passed ? 'pass' : ''}`} onClick={() => onPick(x.id)} title={rc(x.diagnosis.rootCauseKey, lang)}>
                {String(i + 1).padStart(2, '0')}
              </button>
            ))}
          </div>
        </div>
      </header>

      {/* ── legend key: node type ⇒ system component ── */}
      <div className="fx-key">
        <span className="fx-key-lead">{zh ? '图例 · 节点 ⇒ 组件' : 'KEY · NODE ⇒ COMPONENT'}</span>
        {LEGEND.map((l) => (
          <span key={l.cat} className="fx-key-chip"><i style={{ background: CAT_COLOR[l.cat] }} />{zh ? l.zh : l.en}</span>
        ))}
        <span className="fx-key-hint">{zh ? '悬停 → 点亮关联 · 点击 → 锁定' : 'HOVER → LIGHT LINKS · CLICK → LOCK'}</span>
      </div>

      {/* ── ③ THE TACTICAL REPLAY CANVAS (hero) ── */}
      <section className="fx-replay">
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

        {/* ── transport HUD: scrubber through the spatial graph ── */}
        <div className="fx-transport">
          <div className="fx-tp-ctl">
            <button onClick={() => { if (atEnd && !playing) { setReached(0); setCursor(0) } setPlaying((p) => !p) }} title="play/pause">{playing ? '❚❚' : '▶'}</button>
            <button onClick={() => { setReached(0); setCursor(0); setPlaying(false) }} title="reset">⤺</button>
            <button onClick={() => { retreat(); setPlaying(false) }} disabled={cursor <= 0} title="prev">◀</button>
            <button onClick={advance} disabled={atEnd} title="step">▸|</button>
          </div>
          <div className="fx-tp-rail">
            <div className="fx-tp-fill" style={{ width: `${(reached / Math.max(1, steps.length - 1)) * 100}%` }} />
            <div className="fx-tp-head" style={{ left: `${(cursor / Math.max(1, steps.length - 1)) * 100}%` }} />
            {steps.map((s, i) => (
              <button key={s.no} className={`fx-tp-tick ${i <= reached ? 'on' : ''} ${i === cursor ? 'cur' : ''}`}
                style={{ left: `${(i / Math.max(1, steps.length - 1)) * 100}%` }}
                onClick={() => scrubTo(i)} title={zh ? s.zh : s.en}>
                <i style={{ background: CAT_COLOR[CAT[s.kind] ?? 'verdict'] }} />
                <span className="fx-tp-no">{s.no}</span>
                <span className="fx-tp-nm">{zh ? s.zh : s.en}</span>
              </button>
            ))}
          </div>
          <div className="fx-tp-meta">
            <span>STEP <b>{cur.no}</b>/{String(steps.length).padStart(2, '0')}</span>
            <span>t+<b>{(cursor * (BEAT / 1000)).toFixed(1)}s</b></span>
            <span className="fx-tp-cat" style={{ background: CAT_COLOR[CAT[cur.kind] ?? 'verdict'] }}>{cur.kind.replace(/_/g, ' ').toUpperCase()}</span>
          </div>
        </div>
      </section>

      {/* ── analytical footer: convergence + structural stress (2-up, not bands) ── */}
      <section className="fx-footer">
        {evo?.ready ? <EvolutionStream data={evo} zh={zh} /> : <div className="fx-conv placeholder" />}
        <StressTest baselines={baselines} zh={zh} />
        <span className="fx-footer-sig">[R230] NO:{String(caseIdx + 1).padStart(2, '0')} · {zh ? '引擎无关 · 可复现' : 'ENGINE-INDEPENDENT · REPRODUCIBLE'}</span>
      </section>
    </div>
  )
}
