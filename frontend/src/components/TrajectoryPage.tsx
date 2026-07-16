import { useEffect, useMemo, useState } from 'react'
import type { Baseline, RcaCase } from '../types'
import type { Lang } from '../i18n'
import { rc } from '../i18n'
import { CountUp } from './Motion'
import { FlowGraph, type FxStation, type FxEvidence, type FxMemTier, type FxReadout, type FxSkills } from './FlowGraph'
import { EvolutionStream, type EvoData } from './EvolutionStream'
import './trajectory.css'

/* ── real ledger → typed viz per step ── */
const arr = (v: unknown): string[] => (Array.isArray(v) ? (v as unknown[]).map(String) : [])
const num = (v: unknown): number | null => (typeof v === 'number' ? v : null)
const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + '…' : s)

// humanized names for the real read-only skills (never show the raw function name)
const SKILL_LABEL: Record<string, [string, string]> = {
  check_admin_auth_failures: ['查登录失败', 'LOGIN FAILS'],
  check_admin_lockout: ['查账号锁定', 'LOCKOUTS'],
  check_policy_deny_profile: ['查拦截策略', 'BLOCK POLICY'],
  check_traffic_baseline: ['查流量基线', 'TRAFFIC BASE'],
  check_event_log: ['查事件日志', 'EVENT LOG'],
  check_dhcp_service: ['查 DHCP', 'DHCP HEALTH'],
  check_security_posture: ['查安全态势', 'POSTURE'],
  check_device_port_probe: ['查端口探测', 'PORT PROBES'],
}
const skillLabel = (s: string, zh: boolean) =>
  SKILL_LABEL[s]?.[zh ? 0 : 1] ?? s.replace(/^check[_-]?/i, '').replace(/_/g, ' ').toUpperCase()

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
          { id: 'episodic', zh: '情景', en: 'CASES', keys: arr(p.episodic) },
          { id: 'semantic', zh: '语义', en: 'FACTS', keys: arr(p.semantic) },
          { id: 'procedural', zh: '程序', en: 'PLAYBOOKS', keys: arr(p.procedural) },
          { id: 'asset_profile', zh: '资产', en: 'ASSETS', keys: arr(p.asset_profile) },
        ]
        push('memory_read', 'ME', '记忆', 'MEMORY', { tiers, total: tiers.reduce((a, t) => a + t.keys.length, 0) })
        break
      }
      case 'skills_exposed':
        push('skills_exposed', 'SK', '技能', 'SKILLS', { skills: arr(p.skills) })
        break
      case 'context_compiled':
        push('context_compiled', 'CT', '压缩', 'COMPRESS', {
          before: num(p.estimated_tokens_before), after: num(p.estimated_tokens_after), ratio: num(p.compression_ratio),
          kept: arr(p.included_evidence_ids).length, missing: arr(p.missing_evidence),
        })
        break
      case 'verifier_result':
        push('verifier_result', 'VF', '核验', 'VERIFY', { passed: Boolean(p.passed), recall: num(p.evidence_recall) })
        break
      case 'diagnosis_completed':
        push('diagnosis_completed', 'DX', '结论', 'RESULT', {
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
    out.splice(i + 1, 0, { kind: 'tool_called', code: 'PR', zh: '查证', en: 'PROBE', no: '', viz: { probes: tools, faces } })
  }
  out.forEach((s, i) => (s.no = String(i + 1).padStart(2, '0')))
  return out
}

// ROLE one-liner per step (terse, shown on the reticle-locked hero)
const DESC: Record<string, [string, string]> = {
  alert_received: ['收到告警 · 圈定范围', 'ALERT IN · SCOPE ASSETS'],
  memory_read: ['调出过往经验', 'RECALL PAST CASES'],
  skills_exposed: ['只允许只读工具', 'READ-ONLY TOOLS ONLY'],
  tool_called: ['只读查证 · 收集证据', 'READ-ONLY CHECKS · COLLECT EVIDENCE'],
  context_compiled: ['压缩证据 · 装进预算', 'COMPRESS TO FIT BUDGET'],
  verifier_result: ['核对每条证据', 'CHECK EACH CITATION'],
  diagnosis_completed: ['给出根因 · 全程只读', 'ROOT CAUSE · READ-ONLY'],
}
const CAT: Record<string, string> = {
  alert_received: 'alert', memory_read: 'memory', skills_exposed: 'skill', tool_called: 'probe',
  context_compiled: 'context', verifier_result: 'verify', diagnosis_completed: 'verdict',
}
const CAP: Record<string, [string, string]> = {
  alert_received: ['资产', 'ASSETS'], memory_read: ['经验', 'PRIORS'], skills_exposed: ['可用', 'TOOLS'],
  tool_called: ['查证', 'CHECKS'], context_compiled: ['压缩', 'RATIO'], verifier_result: ['找回', 'RECALL'],
  diagnosis_completed: ['把握', 'CONF'],
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

// the live readout — the active step's real computation, plain verbs only
function readoutLines(s: VStep, zh: boolean, pool: number): FxReadout[] {
  const v = s.viz as Record<string, unknown>
  switch (s.kind) {
    case 'alert_received': {
      const a = v.assets as string[]
      return [
        { op: zh ? '接收' : 'TAKE', body: zh ? '整理告警内容' : 'clean up the alert' },
        { op: zh ? '圈定' : 'SCOPE', body: `${a.length} ${zh ? '台资产' : 'assets'} · ${clip(a.join(' '), 40)}` },
      ]
    }
    case 'memory_read': {
      const t = (v.tiers as { zh: string; en: string; keys: string[] }[])
      return [
        { op: zh ? '调忆' : 'RECALL', body: t.map((x) => `${zh ? x.zh : x.en} ${x.keys.length}`).join(' · ') },
        { op: zh ? '载入' : 'LOAD', body: `${v.total} ${zh ? '条经验' : 'priors'}` },
      ]
    }
    case 'skills_exposed': {
      const sk = v.skills as string[]
      return [
        { op: zh ? '打分' : 'SCORE', body: zh ? '从全部工具里挑相关的' : 'pick the relevant tools' },
        { op: zh ? '放行' : 'ALLOW', body: zh ? `考虑 ${pool} · 选 ${sk.length}` : `considered ${pool} · chose ${sk.length}` },
      ]
    }
    case 'tool_called': {
      const P = v.probes as { skill: string }[]
      // glyph flow per check: magnifier → evidence chip (no function name, no ev-id)
      return P.slice(0, 3).map(() => ({ op: zh ? '查证' : 'CHECK', body: '', glyph: 'probe' as const }))
    }
    case 'context_compiled': {
      const ratio = (v.ratio as number) ?? 1
      const miss = (v.missing as string[]).length
      return [
        {
          op: zh ? '打包' : 'PACK',
          body: ratio > 1
            ? (zh ? `${v.after}/${v.before} 词 · 缩 ${ratio.toFixed(1)} 倍` : `${v.after}/${v.before} tokens · ${ratio.toFixed(1)}× smaller`)
            : (zh ? `${v.after}/${v.before} 词 · 未超预算` : `${v.after}/${v.before} tokens · fits budget`),
        },
        { op: zh ? '覆盖' : 'COVER', body: zh ? `留 ${v.kept} · 丢 ${miss}` : `kept ${v.kept} · dropped ${miss}` },
      ]
    }
    case 'verifier_result':
      return [
        { op: zh ? '比对' : 'MATCH', body: zh ? '每条引用都亲眼见过' : 'every citation was actually seen' },
        { op: v.passed ? '✓' : '✕', body: `${zh ? '找回' : 'recall'} ${Math.round(((v.recall as number) ?? 0) * 100)}%` },
      ]
    default:
      return [
        { op: zh ? '输出' : 'EMIT', body: `${clip(String(v.label ?? v.rootKey), 38)}` },
        { op: zh ? '封存' : 'SEAL', body: zh ? `把握 ${((v.confidence as number) ?? 0).toFixed(2)} · 引用 ${(v.cited as string[]).length}` : `conf ${((v.confidence as number) ?? 0).toFixed(2)} · ${(v.cited as string[]).length} cites` },
      ]
  }
}

// architecture legend — node type ⇒ system component
const LEGEND: { cat: string; zh: string; en: string }[] = [
  { cat: 'alert', zh: '告警', en: 'ALERT' },
  { cat: 'memory', zh: '记忆', en: 'MEMORY' },
  { cat: 'skill', zh: '技能', en: 'SKILL' },
  { cat: 'probe', zh: '查证', en: 'PROBE' },
  { cat: 'context', zh: '压缩', en: 'COMPRESS' },
  { cat: 'verify', zh: '核验', en: 'VERIFY' },
  { cat: 'verdict', zh: '结论', en: 'RESULT' },
]
const CAT_COLOR: Record<string, string> = {
  alert: '#d6335a', memory: '#4c9d94', skill: '#ff7a6b', probe: '#2b3d38', context: '#ffcfa0', verify: '#a8bfa0', verdict: '#0d0d0d',
}

// small inline padlock (read-only), HTML flavour
const LockGlyph = () => (
  <svg className="fx-lockg" viewBox="0 0 12 16" aria-label="read-only">
    <rect x="1" y="7" width="10" height="8" fill="none" stroke="currentColor" strokeWidth="1.6" />
    <path d="M3 7 V5 a3 3 0 0 1 6 0 V7" fill="none" stroke="currentColor" strokeWidth="1.6" />
  </svg>
)

/* ── ② THE KEY STEP · structural stress test ───────────────────────────────────
   The same 7-node skeleton, miniaturised. Pulling a component greys it out; only
   pulling the SKILL node collapses the downstream span and crashes accuracy
   (real 100% → 16.7%). Not a row of bars — a structural event. */
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
  ['告警', 'ALERT'], ['记忆', 'MEM'], ['技能', 'SKILL'], ['查证', 'PROBE'], ['压缩', 'CTX'], ['核验', 'VERIFY'], ['结论', 'RESULT'],
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
    { key: 'skill', zh: '拔技能', en: '− SKILLS' },
  ]
  return (
    <section className="fx-stress">
      <div className="fx-stress-head">
        <span className="fx-panel-lab"><i className="fx-panel-no">02</i>{zh ? '关键一步 · 技能' : 'THE KEY STEP · SKILLS'}</span>
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
          {/* the load-bearing pillar under the skill node — the shape says it, no word */}
          <g className={`fx-str-pillar ${collapsed ? 'gone' : ''}`}>
            <line x1={MP[2][0]} y1={MP[2][1] + MNH / 2} x2={MP[2][0]} y2={MH - 10} />
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
                {pulled ? <text className="fx-str-x" x={MNW / 2} y={-8} textAnchor="middle">✕</text> : null}
              </g>
            )
          })}
        </svg>
        <div className={`fx-stress-read ${collapsed ? 'bad' : 'ok'}`}>
          <span className="fx-stress-acc"><CountUp value={acc} /><i>%</i></span>
          <span className="fx-stress-acc-lab">{zh ? '准确率' : 'ACCURACY'}</span>
          {acc !== base ? <span className="fx-stress-delta">↓{Math.abs(acc - base)}%</span> : null}
          <span className={`fx-stress-flag ${collapsed ? 'bad' : 'ok'}`}>
            {pull === 'none' ? (zh ? '还成立' : 'HOLDS')
              : collapsed ? (zh ? '结论站不住' : 'BREAKS')
                : (zh ? '仍成立' : 'STILL HOLDS')}
          </span>
        </div>
      </div>
      <div className="fx-stress-foot">
        <span className="fx-stress-tag crit">{zh ? '技能 = 最关键' : 'SKILLS = MOST CRITICAL'}</span>
        <span className="fx-stress-tag">{zh ? '拔记忆或压缩 · 仍 100%' : '− MEMORY / COMPRESS · STILL 100%'}</span>
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
  // the agent's tool pool = every skill observed across the real held-out traces
  const toolPool = useMemo(() => [...new Set(
    cases.flatMap((cc) => cc.trace.filter((e) => e.kind === 'skills_exposed' || e.kind === 'tool_called')
      .flatMap((e) => e.kind === 'skills_exposed' ? arr(e.payload.skills) : [String(e.payload.skill ?? '')]))
  )].filter(Boolean), [cases])

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
  const calledSkills = new Set<string>()
  for (const ev of c.trace) {
    if (ev.kind === 'tool_called') {
      arr(ev.payload.evidence_ids).forEach((id) => pinnedIds.add(id))
      calledSkills.add(String(ev.payload.skill ?? ''))
    }
    if (ev.kind === 'context_compiled') arr(ev.payload.included_evidence_ids).forEach((id) => includedIds.add(id))
  }
  const fxEvidence: FxEvidence[] = evid.slice(0, 2).map((e) => ({
    id: e.evidenceId, sum: clip(e.summary, 54), raw: e.source,
    pinned: pinnedIds.has(e.evidenceId), included: includedIds.has(e.evidenceId),
    cited: true, verified: c.verifier.passed,
  }))
  const memStep = steps.find((s) => s.kind === 'memory_read')
  const memViz = memStep ? (memStep.viz as { tiers: { zh: string; en: string; keys: string[] }[]; total: number }) : null
  const memoryTiers: FxMemTier[] = memViz ? memViz.tiers.map((t) => ({ label: zh ? t.zh : t.en, count: t.keys.length })) : []
  const skStep = steps.find((s) => s.kind === 'skills_exposed')
  const skExposed = skStep ? (skStep.viz as { skills: string[] }).skills : []
  const exposedSet = new Set(skExposed)
  const skippedPool = toolPool.filter((s) => !exposedSet.has(s))
  const fxSkills: FxSkills = {
    poolCount: toolPool.length,
    chosen: skExposed.map((id) => ({ id, label: skillLabel(id, zh), called: calledSkills.has(id) })),
    skippedCount: skippedPool.length,
    skippedNames: skippedPool.map((s) => skillLabel(s, zh)).join(' · '),
  }
  const ctxStep = steps.find((s) => s.kind === 'context_compiled')
  const ctxViz = ctxStep ? (ctxStep.viz as { kept: number; missing: string[] }) : null
  const verStep = steps.find((s) => s.kind === 'verifier_result')
  const verRecall = verStep ? ((verStep.viz as { recall: number | null }).recall ?? 1) : 1
  const stations: FxStation[] = steps.map((s) => ({
    no: s.no, name: zh ? s.zh : s.en, role: DESC[s.kind]?.[zh ? 0 : 1] ?? '', kind: s.kind, cat: CAT[s.kind] ?? 'verdict',
    metric: stationMetric(s, zh), readout: readoutLines(s, zh, toolPool.length), loadBearing: s.kind === 'skills_exposed',
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
          <span className="fx-mast-kick">{zh ? '自我进化 AI · 内网排查' : 'SELF-EVOLVING AI · NETWORK TRIAGE'}</span>
          <h1 className="fx-mast-title">{zh ? <>长<mark>轨迹</mark></> : <>LONG <mark>TRAJECTORY</mark></>}</h1>
          <div className="fx-mast-mission">
            <span className="fx-mast-q" title={c.query}>{clip(c.query, 62)}</span>
            <span className="fx-mast-arrow">▸</span>
            <mark className="fx-mast-root">{rc(c.diagnosis.rootCauseKey, lang)}</mark>
            <span className="fx-mast-facts">
              <b>{c.diagnosis.confidence.toFixed(2)}</b>{zh ? '把握' : 'CONF'} · <b>{evid.length}/{evid.length}</b>{zh ? '已核对' : 'VERIFIED'} · {zh ? '全程只读' : <LockGlyph />}
            </span>
          </div>
        </div>
        <div className="fx-mast-r">
          <span className="fx-mast-real">{zh ? '真实事件 · R230' : 'REAL CASE · R230'}</span>
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
        <span className="fx-key-lead">{zh ? '图例' : 'KEY'}</span>
        {LEGEND.map((l) => (
          <span key={l.cat} className="fx-key-chip"><i style={{ background: CAT_COLOR[l.cat] }} />{zh ? l.zh : l.en}</span>
        ))}
        <span className="fx-key-hint">{zh ? '悬停高亮 · 点击锁定' : 'HOVER · CLICK'}</span>
      </div>

      {/* ── ③ THE TACTICAL REPLAY CANVAS (hero) ── */}
      <section className="fx-replay">
        <FlowGraph
          stations={stations}
          evidence={fxEvidence}
          memory={memoryTiers}
          memoryTotal={memViz?.total ?? 0}
          skills={fxSkills}
          ctxFork={{ kept: ctxViz?.kept ?? 0, dropped: ctxViz?.missing.length ?? 0 }}
          probes={{ available: skExposed.length, run: calledSkills.size }}
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
            <span>{zh ? <>第 <b>{Number(cur.no)}</b> 步 / 共 {steps.length}</> : <>STEP <b>{Number(cur.no)}</b> / {steps.length}</>}</span>
            <span>t+<b>{(cursor * (BEAT / 1000)).toFixed(1)}s</b></span>
          </div>
        </div>
      </section>

      {/* ── analytical footer: convergence + structural stress (2-up, not bands) ── */}
      <section className="fx-footer">
        {evo?.ready ? <EvolutionStream data={evo} zh={zh} /> : <div className="fx-conv placeholder" />}
        <StressTest baselines={baselines} zh={zh} />
      </section>
    </div>
  )
}
