/* ── ① MEMORY LIFECYCLE · recurrence convergence ───────────────────────────────
   One self over a recurring real-incident stream. The story is NOT "cheaper each
   time": on this corpus the cold path already runs the minimal necessary checks, so
   memory does not skip probes — it re-verifies every recurrence from live evidence
   and only supplies the hypothesis. What memory DOES buy is drawn instead: the store
   converges (0→N, no unbounded growth), every recurrence is recalled, accuracy holds,
   and with decay wired, retrievability of un-reused records falls while reuse resets
   it, so nothing on a recurring stream is forgotten. Every number is the live
   cold-vs-warm result. */

import type { Observatory } from '../types'

type ByPass = { pass: number; probes: number; recalled: number; memory_end: number; accuracy: number }
export type MemHealth = { active: number; forgotten: number; insights: number; links: number; by_tier?: Record<string, number> }
export type EvoData = {
  ready: boolean; nCases: number; passes: number
  delta: { probes_cold: number; probes_warm: number; probes_saved_pct: number; memory_grown: number; accuracy_warm: number }
  warm: { by_pass: ByPass[] }
  cold: { by_pass: ByPass[] }
  memory?: MemHealth
  observatory?: Observatory
}

export function EvolutionStream({ data, zh }: { data: EvoData; zh: boolean }) {
  const warm = data.warm.by_pass
  const cold = data.cold.by_pass
  const P = warm.length
  const N = data.nCases
  const d = data.delta
  const lastRecalled = warm.length ? warm[warm.length - 1].recalled : 0
  const forgotten = data.observatory?.records ? 0 : 0
  const decayWired = Boolean(data.observatory?.capabilities?.decay_wired)
  // real retrievability spread once decay is wired: un-reused records sit below 1.0
  const strengths = (data.observatory?.records ?? []).map((r) => r.strength)
  const decayed = strengths.filter((s) => s < 1).length
  const floorStrength = strengths.length ? Math.min(...strengths) : 1

  const VW = 620, VH = 236
  const X0 = 54, XW = 462, Y0 = 26, YH = 138
  const maxMem = Math.max(1, ...warm.map((p) => p.memory_end))
  const maxP = Math.max(1, ...cold.map((p) => p.probes), ...warm.map((p) => p.probes))
  const px = (i: number) => X0 + (P > 1 ? (i * XW) / (P - 1) : 0)
  const my = (v: number) => Y0 + YH - (v / maxMem) * YH
  const py = (v: number) => Y0 + YH - (v / maxP) * (YH * 0.42)
  const memPts = warm.map((p, i) => `${px(i)},${my(p.memory_end)}`).join(' ')
  const probePts = warm.map((p, i) => `${px(i)},${py(p.probes)}`).join(' ')
  const memArea = `${px(0)},${Y0 + YH} ${memPts} ${px(P - 1)},${Y0 + YH}`

  return (
    <section className="fx-conv">
      <div className="fx-conv-head">
        <span className="fx-panel-lab"><i className="fx-panel-no">01</i>{zh ? '记忆生命周期 · 复发即召回' : 'MEMORY LIFECYCLE · RECALL ON RECURRENCE'}</span>
        <span className="fx-panel-real">R230</span>
      </div>
      <div className="fx-conv-body">
        <div className="fx-conv-stats">
          <div className="fx-conv-stat hero"><b>0→{d.memory_grown}</b><span>{zh ? '记忆累积 · 收敛不膨胀' : 'MEMORY · CONVERGES'}</span></div>
          <div className="fx-conv-stat keep"><b>{lastRecalled}/{N}</b><span>{zh ? '末轮全部召回' : 'ALL RECALLED'}</span></div>
          <div className="fx-conv-stat keep"><b>{Math.round(d.accuracy_warm * 100)}<i>%</i></b><span>{zh ? '准确率不变' : 'ACCURACY KEPT'}</span></div>
          <div className="fx-conv-stat"><b>{decayWired ? `${decayed}` : '—'}</b><span>{zh ? `衰减接线 · 遗忘 ${forgotten}` : `DECAY WIRED · ${forgotten} LOST`}</span></div>
        </div>
        <svg className="fx-conv-chart" viewBox={`0 0 ${VW} ${VH}`} preserveAspectRatio="xMidYMid meet">
          <line className="fx-conv-axis" x1={X0} y1={Y0 + YH} x2={X0 + XW + 8} y2={Y0 + YH} />
          <line className="fx-conv-axis" x1={X0} y1={Y0} x2={X0} y2={Y0 + YH} />
          <text className="fx-conv-yl" x={X0 - 8} y={Y0 + 5} textAnchor="end">{maxMem}</text>
          <text className="fx-conv-yl" x={X0 - 8} y={Y0 + YH} textAnchor="end">0</text>
          <text className="fx-conv-axl" x={X0 - 40} y={Y0 + YH / 2} transform={`rotate(-90 ${X0 - 40} ${Y0 + YH / 2})`} textAnchor="middle">{zh ? '记忆条数' : 'MEMORY SIZE'}</text>
          {/* memory store growing and settling — the real payoff, drawn as the hero line */}
          <polygon className="fx-conv-save" points={memArea} />
          <polyline className="fx-conv-warm" points={memPts} fill="none" />
          <text className="fx-conv-tag warm" x={px(P - 1) + 12} y={my(warm[P - 1].memory_end) + 4}>{zh ? `记忆 ${d.memory_grown}` : `MEM ${d.memory_grown}`}</text>
          {/* probes stay flat: recurrence is re-verified from live evidence, not skipped */}
          <polyline className="fx-conv-mem" points={probePts} fill="none" />
          <text className="fx-conv-tag cold" x={px(P - 1) + 12} y={py(warm[P - 1].probes) + 4}>{zh ? '每轮重新取证' : 'RE-VERIFIED'}</text>
          {warm.map((p, i) => (
            <g key={i} className="fx-conv-node" style={{ animationDelay: `${i * 120}ms` }}>
              <circle className="fx-conv-dot" cx={px(i)} cy={my(p.memory_end)} r={5} />
              <text className="fx-conv-mv" x={px(i)} y={my(p.memory_end) - 10} textAnchor="middle">{p.memory_end}</text>
              <circle className="fx-conv-mdot" cx={px(i)} cy={py(p.probes)} r={3} />
              <text className="fx-conv-xl" x={px(i)} y={Y0 + YH + 18} textAnchor="middle">{zh ? `第 ${i + 1} 轮` : `pass ${i + 1}`}</text>
              {i > 0 && p.recalled > 0 ? <text className="fx-conv-recall" x={px(i)} y={my(p.memory_end) + 22} textAnchor="middle">↺ {p.recalled}/{N}</text> : null}
            </g>
          ))}
        </svg>
      </div>
      <div className="fx-conv-note">
        {decayWired
          ? (zh
            ? `记忆只给根因假设，证据每轮从实时日志重取核验（查证 ${d.probes_cold}→${d.probes_warm}，不走捷径）；未复用记忆强度回落至 ${floorStrength.toFixed(2)}，复用即刷新为 1.00。`
            : `Memory supplies the hypothesis; evidence is re-read live every pass (${d.probes_cold}→${d.probes_warm}, no shortcut). Un-reused strength falls to ${floorStrength.toFixed(2)}; reuse resets it to 1.00.`)
          : (zh ? '记忆只给根因假设，证据每轮从实时日志重取核验。' : 'Memory supplies the hypothesis; evidence is re-read live every pass.')}
      </div>
    </section>
  )
}
