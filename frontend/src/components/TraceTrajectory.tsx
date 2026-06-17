import type { RcaCase } from '../types'
import type { Lang } from '../i18n'

const LABEL: Record<string, [string, string]> = {
  alert_received: ['alert', '告警'],
  memory_read: ['memory', '记忆'],
  skills_exposed: ['skills', '技能'],
  tool_called: ['probe', '取证'],
  context_compiled: ['context', '压缩'],
  verifier_result: ['verify', '校验'],
  cost_observed: ['cost', '成本'],
  diagnosis_completed: ['diagnose', '诊断'],
}

// collapse repeated tool_called into one node carrying the count
function steps(c: RcaCase) {
  const out: { kind: string; n: number; detail: string }[] = []
  for (const ev of c.trace) {
    if (ev.kind === 'cost_observed') continue
    const prev = out[out.length - 1]
    if (ev.kind === 'tool_called' && prev?.kind === 'tool_called') {
      prev.n += 1
      continue
    }
    let detail = ''
    if (ev.kind === 'skills_exposed' && Array.isArray(ev.payload.skills)) detail = `${(ev.payload.skills as string[]).length}`
    if (ev.kind === 'verifier_result') detail = ev.payload.passed ? '✓' : '✕'
    if (ev.kind === 'diagnosis_completed') detail = String(ev.payload.root_cause_key ?? '').slice(0, 10)
    out.push({ kind: ev.kind, n: 1, detail })
  }
  return out
}

export function TraceTrajectory({ rcaCase, lang, reasoner }: { rcaCase: RcaCase; lang: Lang; reasoner: string }) {
  const st = steps(rcaCase)
  const W = 720
  const pad = 18
  const span = (W - pad * 2) / Math.max(1, st.length - 1)
  const y = 46
  const xs = st.map((_, i) => pad + i * span)
  const path = `M${xs[0]} ${y} ${xs.map((x) => `L ${x} ${y}`).join(' ')}`
  return (
    <div className="trace-traj">
      <div className="tt-head">
        <span className="tt-kicker">{lang === 'zh' ? '执行轨迹 · 可回放' : 'eval trace · replayable'}</span>
        <span className="tt-meta">{reasoner} · {st.length} {lang === 'zh' ? '步' : 'steps'} · {rcaCase.verifier.passed ? (lang === 'zh' ? '校验通过' : 'verified') : 'failed'}</span>
      </div>
      <svg viewBox={`0 0 ${W} 84`} className="tt-svg" preserveAspectRatio="xMidYMid meet">
        <path d={path} className="tt-rail" />
        <path d={path} className="tt-rail-lit" />
        <circle r="4" className="tt-head-dot">
          <animateMotion dur={`${st.length * 0.5}s`} repeatCount="indefinite" path={path} />
        </circle>
        {st.map((s, i) => (
          <g key={i} className="tt-node" style={{ ['--d' as string]: `${i * 0.12}s` }}>
            <circle cx={xs[i]} cy={y} r={s.kind === 'diagnosis_completed' ? 7 : 5} className={`tt-dot ${s.kind === 'verifier_result' ? (s.detail === '✓' ? 'ok' : 'bad') : ''} ${s.kind === 'diagnosis_completed' ? 'fin' : ''}`} />
            <text x={xs[i]} y={y - 14} className="tt-lab" textAnchor="middle">{LABEL[s.kind]?.[lang === 'zh' ? 1 : 0] ?? s.kind}{s.n > 1 ? `·${s.n}` : ''}</text>
            {s.detail ? <text x={xs[i]} y={y + 20} className="tt-det" textAnchor="middle">{s.detail}</text> : null}
          </g>
        ))}
      </svg>
    </div>
  )
}
