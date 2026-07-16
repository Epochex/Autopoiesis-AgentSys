/* ── ① SELF-EVOLUTION · spatial convergence panel ──────────────────────────────
   One self over a recurring real-incident stream, drawn as two overlaid runs that
   SPLIT: no-memory holds flat, with-memory plunges checks to zero as the memory
   core thickens — accuracy identical the whole way. All numbers are the live
   cold-vs-warm result, not 示意. Compact tactical panel (fx-conv-*), not a band.

   Scope note: this panel answers "did it get cheaper", nothing more. What the
   memory IS, and how it changed, belongs to MemoryObservatory, which renders the
   item-level record/event data. The tier "mesh" that used to live here was
   decorative — hub and satellite coordinates were hardcoded and the API only ever
   returned scalar counts — so it was removed rather than restyled. */

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
  // Real recall on the final pass. This used to read {N}/{N} straight from nCases,
  // so it could never report anything but 100% — recalled is the measured field.
  const lastRecalled = warm.length ? warm[warm.length - 1].recalled : 0

  const VW = 620, VH = 236
  const X0 = 54, XW = 462, Y0 = 26, YH = 138
  const maxP = Math.max(1, ...cold.map((p) => p.probes), ...warm.map((p) => p.probes))
  const maxMem = Math.max(1, ...warm.map((p) => p.memory_end))
  const px = (i: number) => X0 + (P > 1 ? (i * XW) / (P - 1) : 0)
  const py = (v: number) => Y0 + YH - (v / maxP) * YH
  // memory rides its OWN lower lane (0.5 scale) so it never crowds the flat cold line
  const my = (v: number) => Y0 + YH - (v / maxMem) * (YH * 0.5)
  const coldPts = cold.map((p, i) => `${px(i)},${py(p.probes)}`).join(' ')
  const warmPts = warm.map((p, i) => `${px(i)},${py(p.probes)}`).join(' ')
  const memPts = warm.map((p, i) => `${px(i)},${my(p.memory_end)}`).join(' ')
  const area = [...cold.map((p, i) => `${px(i)},${py(p.probes)}`), ...warm.slice().reverse().map((p, i) => `${px(P - 1 - i)},${py(p.probes)}`)].join(' ')

  return (
    <section className="fx-conv">
      <div className="fx-conv-head">
        <span className="fx-panel-lab"><i className="fx-panel-no">01</i>{zh ? '越用越省 · 自我进化' : 'CHEAPER EACH TIME · SELF-EVOLVING'}</span>
        <span className="fx-panel-real">{zh ? '真实 · R230' : 'REAL · R230'}</span>
      </div>
      <div className="fx-conv-body">
        <div className="fx-conv-stats">
          <div className="fx-conv-stat hero"><b>−{d.probes_saved_pct}<i>%</i></b><span>{zh ? '省下的查证' : 'CHECKS SAVED'}</span></div>
          <div className="fx-conv-stat keep"><b>{Math.round(d.accuracy_warm * 100)}<i>%</i></b><span>{zh ? '准确率不变' : 'ACCURACY KEPT'}</span></div>
          <div className="fx-conv-stat"><b>0→{d.memory_grown}</b><span>{zh ? '记忆累积' : 'MEMORY GROWN'}</span></div>
          <div className="fx-conv-stat"><b>{lastRecalled}/{N}</b><span>{zh ? '末轮召回' : 'RECALLED LAST PASS'}</span></div>
        </div>
        <svg className="fx-conv-chart" viewBox={`0 0 ${VW} ${VH}`} preserveAspectRatio="xMidYMid meet">
          <line className="fx-conv-axis" x1={X0} y1={Y0 + YH} x2={X0 + XW + 8} y2={Y0 + YH} />
          <line className="fx-conv-axis" x1={X0} y1={Y0} x2={X0} y2={Y0 + YH} />
          <text className="fx-conv-yl" x={X0 - 8} y={Y0 + 5} textAnchor="end">{maxP}</text>
          <text className="fx-conv-yl" x={X0 - 8} y={Y0 + YH} textAnchor="end">0</text>
          <text className="fx-conv-axl" x={X0 - 40} y={Y0 + YH / 2} transform={`rotate(-90 ${X0 - 40} ${Y0 + YH / 2})`} textAnchor="middle">{zh ? '每次查证次数' : 'CHECKS PER CASE'}</text>
          {/* the savings gap between the two runs */}
          <polygon className="fx-conv-save" points={area} />
          {/* cold: never learns — flat high */}
          <polyline className="fx-conv-cold" points={coldPts} fill="none" />
          <text className="fx-conv-tag cold" x={px(P - 1) + 12} y={py(cold[P - 1].probes) + 4}>{zh ? '不记忆' : 'NO MEMORY'}</text>
          {/* memory growth — its own lower lane */}
          <polyline className="fx-conv-mem" points={memPts} fill="none" />
          <text className="fx-conv-tag mem" x={px(P - 1) + 12} y={my(warm[P - 1].memory_end) + 4}>{zh ? `记忆 ${d.memory_grown}` : `MEMORY ${d.memory_grown}`}</text>
          {/* warm: recalls → plunges to 0 */}
          <polyline className="fx-conv-warm" points={warmPts} fill="none" />
          <text className="fx-conv-tag warm" x={px(P - 1) + 12} y={py(warm[P - 1].probes) + 4}>{zh ? '会记忆' : 'WITH MEMORY'}</text>
          {warm.map((p, i) => (
            <g key={i} className="fx-conv-node" style={{ animationDelay: `${i * 120}ms` }}>
              <circle className="fx-conv-dot" cx={px(i)} cy={py(p.probes)} r={4.5} />
              <circle className="fx-conv-mdot" cx={px(i)} cy={my(p.memory_end)} r={3} />
              <text className="fx-conv-mv" x={px(i)} y={my(p.memory_end) - 8} textAnchor="middle">{p.memory_end}</text>
              <text className="fx-conv-xl" x={px(i)} y={Y0 + YH + 18} textAnchor="middle">{i === 0 ? (zh ? '第 1 次' : '1st') : (zh ? `第 ${i + 1} 次` : `time ${i + 1}`)}</text>
              {i > 0 && p.recalled > 0 ? <text className="fx-conv-recall" x={px(i)} y={py(p.probes) - 12} textAnchor="middle">↺ {p.recalled}/{N}</text> : null}
            </g>
          ))}
        </svg>
      </div>
    </section>
  )
}
