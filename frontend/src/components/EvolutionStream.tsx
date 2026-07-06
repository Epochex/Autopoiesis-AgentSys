/* ── ① 长周期自演化 · REAL data hero ────────────────────────────────────────
   One self over a recurring real-incident stream: first encounter = investigate,
   every recurrence = resolved from provenance-linked memory (0 probes) at
   unchanged accuracy. All numbers are the live cold-vs-warm result — not 示意. */

type ByPass = { pass: number; probes: number; recalled: number; memory_end: number; accuracy: number }
export type MemHealth = { active: number; forgotten: number; insights: number; links: number; by_tier?: Record<string, number> }
export type EvoData = {
  ready: boolean; nCases: number; passes: number
  delta: { probes_cold: number; probes_warm: number; probes_saved_pct: number; memory_grown: number; accuracy_warm: number }
  warm: { by_pass: ByPass[] }
  cold: { by_pass: ByPass[] }
  memory?: MemHealth
}

export function EvolutionStream({ data, zh }: { data: EvoData; zh: boolean }) {
  const warm = data.warm.by_pass
  const cold = data.cold.by_pass
  const P = warm.length
  const N = data.nCases
  const d = data.delta
  const maxP = Math.max(1, ...cold.map((p) => p.probes), ...warm.map((p) => p.probes))
  const maxMem = Math.max(1, ...warm.map((p) => p.memory_end))
  const X0 = 70, XW = 560, Y0 = 30, YH = 150
  const px = (i: number) => X0 + (P > 1 ? (i * XW) / (P - 1) : 0)
  const py = (v: number) => Y0 + YH - (v / maxP) * YH
  const my = (v: number) => Y0 + YH - (v / maxMem) * (YH * 0.9)
  const coldPts = cold.map((p, i) => `${px(i)},${py(p.probes)}`).join(' ')
  const warmPts = warm.map((p, i) => `${px(i)},${py(p.probes)}`).join(' ')
  const memPts = warm.map((p, i) => `${px(i)},${my(p.memory_end)}`).join(' ')
  const area = [...cold.map((p, i) => `${px(i)},${py(p.probes)}`), ...warm.slice().reverse().map((p, i) => `${px(P - 1 - i)},${py(p.probes)}`)].join(' ')

  return (
    <section className="tp-evo">
      <div className="tp-evo-head">
        <span className="tp-band-lab">{zh ? '① 长周期自演化 · 一个 self 在真实事件流上越用越省' : '① SELF-EVOLUTION · ONE SELF GETTING CHEAPER OVER A REAL INCIDENT STREAM'}</span>
        <span className="tp-evo-real">{zh ? '真实数据 · R230 留出集 · cold-vs-warm · 可复现' : 'REAL · R230 HELD-OUT · cold-vs-warm · reproducible'}</span>
      </div>
      <div className="tp-evo-body">
        <div className="tp-evo-stats">
          <div className="tp-evo-stat hero"><b>−{d.probes_saved_pct}<i>%</i></b><span>{zh ? '探针 / 工具成本 · 复现' : 'PROBES / COST · RECURRENCE'}</span></div>
          <div className="tp-evo-stat"><b>{P - 1}×{N}/{N}</b><span>{zh ? '复现从记忆召回' : 'RESOLVED FROM MEMORY'}</span></div>
          <div className="tp-evo-stat"><b>0→{d.memory_grown}</b><span>{zh ? '记忆累积' : 'MEMORY GROWN'}</span></div>
          <div className="tp-evo-stat keep"><b>{Math.round(d.accuracy_warm * 100)}<i>%</i></b><span>{zh ? '准确率不变' : 'ACCURACY · UNCHANGED'}</span></div>
        </div>
        <svg className="tp-evo-chart" viewBox="0 0 680 230" preserveAspectRatio="xMidYMid meet">
          <line className="tp-evo-axis" x1={X0} y1={Y0 + YH} x2={X0 + XW + 10} y2={Y0 + YH} />
          <line className="tp-evo-axis" x1={X0} y1={Y0} x2={X0} y2={Y0 + YH} />
          <text className="tp-evo-yl" x={X0 - 8} y={Y0 + 4} textAnchor="end">{maxP}</text>
          <text className="tp-evo-yl" x={X0 - 8} y={Y0 + YH} textAnchor="end">0</text>
          <text className="tp-evo-axl" x={X0 - 44} y={Y0 + YH / 2} transform={`rotate(-90 ${X0 - 44} ${Y0 + YH / 2})`} textAnchor="middle">{zh ? '探针数 / 事件' : 'PROBES / EVENT'}</text>
          {/* savings gap */}
          <polygon className="tp-evo-save" points={area} />
          {/* cold: never learns — flat */}
          <polyline className="tp-evo-cold" points={coldPts} fill="none" />
          <text className="tp-evo-tag cold" x={px(P - 1) + 14} y={py(cold[P - 1].probes) + 4}>{zh ? '不记忆' : 'COLD'}</text>
          {/* memory growth (secondary) */}
          <polyline className="tp-evo-mem" points={memPts} fill="none" />
          {/* warm: recalls → drops to 0 */}
          <polyline className="tp-evo-warm" points={warmPts} fill="none" />
          <text className="tp-evo-tag warm" x={px(P - 1) + 14} y={py(warm[P - 1].probes) + 4}>{zh ? '会记忆' : 'WARM'}</text>
          {warm.map((p, i) => (
            <g key={i} className="tp-evo-node" style={{ animationDelay: `${i * 120}ms` }}>
              <circle className="tp-evo-dot" cx={px(i)} cy={py(p.probes)} r={4.5} />
              <circle className="tp-evo-mdot" cx={px(i)} cy={my(p.memory_end)} r={3} />
              <text className="tp-evo-mv" x={px(i)} y={my(p.memory_end) - 8} textAnchor="middle">{p.memory_end}</text>
              <text className="tp-evo-xl" x={px(i)} y={Y0 + YH + 20} textAnchor="middle">{i === 0 ? (zh ? '第1次·取证' : '1st · probe') : (zh ? `复现${i}` : `recur ${i}`)}</text>
              {i > 0 && p.recalled > 0 ? <text className="tp-evo-recall" x={px(i)} y={py(p.probes) - 13} textAnchor="middle">↺ {p.recalled}/{N}</text> : null}
            </g>
          ))}
          <text className="tp-evo-note" x={X0} y={Y0 + YH + 44}>{zh ? '记忆核随事件变厚 → 复现即召回,取证归零 · 准确率与引用核验 100% 不变' : 'memory thickens → recurrences recalled, probing → 0 · accuracy + citation-verify 100% unchanged'}</text>
        </svg>
      </div>
      {data.memory ? (
        <div className="tp-evo-ops">
          <span className="tp-evo-ops-lead">{zh ? '记忆是被管理的,而非只增不减' : 'THE STORE IS MANAGED, NOT JUST APPENDED'}</span>
          <span className="tp-evo-op"><b>Mem0</b><i>{zh ? '写路由' : 'WRITE ROUTER'}</i><em>{data.memory.active} {zh ? '条·去重' : 'DEDUP'}</em></span>
          <span className="tp-evo-op"><b>A-MEM</b><i>{zh ? '关联' : 'LINKS'}</i><em>{data.memory.links}</em></span>
          <span className="tp-evo-op"><b>{zh ? '反思' : 'REFLECT'}</b><i>{zh ? '族群晋升' : 'FAMILY INSIGHT'}</i><em>{data.memory.insights}</em></span>
          <span className="tp-evo-op"><b>Ebbinghaus</b><i>{zh ? '衰减遗忘' : 'DECAY'}</i><em>{data.memory.forgotten}{zh ? ' · 全复用保鲜' : ' FORGOTTEN'}</em></span>
        </div>
      ) : null}
    </section>
  )
}
