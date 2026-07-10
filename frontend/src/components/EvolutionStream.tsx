/* ── ① SELF-EVOLUTION · spatial convergence panel ──────────────────────────────
   One self over a recurring real-incident stream, drawn as two overlaid runs that
   SPLIT: cold (never learns) holds flat, warm (learns) plunges probing to zero as
   the memory core thickens — accuracy identical the whole way. All numbers live
   cold-vs-warm result, not 示意. Compact tactical panel (fx-conv-*), not a band. */

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
  const bt = data.memory?.by_tier
  const tiers = bt ? [
    { id: 'episodic', label: zh ? '情景 EPI' : 'EPI', count: bt.episodic ?? 0, color: '#d6335a' },
    { id: 'semantic', label: zh ? '语义 SEM' : 'SEM', count: bt.semantic ?? 0, color: '#4c9d94' },
    { id: 'procedural', label: zh ? '程序 PRO' : 'PRO', count: bt.procedural ?? 0, color: '#ffcfa0' },
  ] : []

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
        <span className="fx-panel-lab"><i className="fx-panel-no">01</i>{zh ? '长周期自演化 · 越用越省' : 'SELF-EVOLUTION · CHEAPER OVER TIME'}</span>
        <span className="fx-panel-real">{zh ? '真实 · R230 · cold-vs-warm' : 'REAL · R230 · cold-vs-warm'}</span>
      </div>
      <div className="fx-conv-body">
        <div className="fx-conv-stats">
          <div className="fx-conv-stat hero"><b>−{d.probes_saved_pct}<i>%</i></b><span>{zh ? '探针成本 · 复现' : 'PROBE COST · RECUR'}</span></div>
          <div className="fx-conv-stat keep"><b>{Math.round(d.accuracy_warm * 100)}<i>%</i></b><span>{zh ? '准确率不变' : 'ACCURACY · KEPT'}</span></div>
          <div className="fx-conv-stat"><b>0→{d.memory_grown}</b><span>{zh ? '记忆累积' : 'MEMORY GROWN'}</span></div>
          <div className="fx-conv-stat"><b>{P - 1}×{N}/{N}</b><span>{zh ? '复现即召回' : 'RECALLED'}</span></div>
        </div>
        <svg className="fx-conv-chart" viewBox={`0 0 ${VW} ${VH}`} preserveAspectRatio="xMidYMid meet">
          <line className="fx-conv-axis" x1={X0} y1={Y0 + YH} x2={X0 + XW + 8} y2={Y0 + YH} />
          <line className="fx-conv-axis" x1={X0} y1={Y0} x2={X0} y2={Y0 + YH} />
          <text className="fx-conv-yl" x={X0 - 8} y={Y0 + 5} textAnchor="end">{maxP}</text>
          <text className="fx-conv-yl" x={X0 - 8} y={Y0 + YH} textAnchor="end">0</text>
          <text className="fx-conv-axl" x={X0 - 40} y={Y0 + YH / 2} transform={`rotate(-90 ${X0 - 40} ${Y0 + YH / 2})`} textAnchor="middle">{zh ? '探针 / 事件' : 'PROBES / EVENT'}</text>
          {/* the savings gap between the two runs */}
          <polygon className="fx-conv-save" points={area} />
          {/* cold: never learns — flat high */}
          <polyline className="fx-conv-cold" points={coldPts} fill="none" />
          <text className="fx-conv-tag cold" x={px(P - 1) + 12} y={py(cold[P - 1].probes) + 4}>{zh ? '冷 · 不记忆' : 'COLD · NO-MEM'}</text>
          {/* memory growth — its own lower lane, clearly labelled */}
          <polyline className="fx-conv-mem" points={memPts} fill="none" />
          <text className="fx-conv-tag mem" x={px(P - 1) + 12} y={my(warm[P - 1].memory_end) + 4}>{zh ? `记忆 →${d.memory_grown}` : `MEM →${d.memory_grown}`}</text>
          {/* warm: recalls → plunges to 0 */}
          <polyline className="fx-conv-warm" points={warmPts} fill="none" />
          <text className="fx-conv-tag warm" x={px(P - 1) + 12} y={py(warm[P - 1].probes) + 4}>{zh ? '暖 · 会记忆' : 'WARM · +MEM'}</text>
          {warm.map((p, i) => (
            <g key={i} className="fx-conv-node" style={{ animationDelay: `${i * 120}ms` }}>
              <circle className="fx-conv-dot" cx={px(i)} cy={py(p.probes)} r={4.5} />
              <circle className="fx-conv-mdot" cx={px(i)} cy={my(p.memory_end)} r={3} />
              <text className="fx-conv-mv" x={px(i)} y={my(p.memory_end) - 8} textAnchor="middle">{p.memory_end}</text>
              <text className="fx-conv-xl" x={px(i)} y={Y0 + YH + 18} textAnchor="middle">{i === 0 ? (zh ? '首·取证' : '1st·probe') : (zh ? `复现${i}` : `recur ${i}`)}</text>
              {i > 0 && p.recalled > 0 ? <text className="fx-conv-recall" x={px(i)} y={py(p.probes) - 12} textAnchor="middle">↺ {p.recalled}/{N}</text> : null}
            </g>
          ))}
        </svg>
      </div>
      {tiers.length ? (
        <div className="fx-conv-tiers">
          <span className="fx-conv-tiers-lead">{zh ? '记忆核 · 三层' : 'MEMORY CORE'}</span>
          <svg className="fx-conv-mesh" viewBox="0 0 168 96" preserveAspectRatio="xMidYMid meet">
            {(() => {
              const max = Math.max(1, ...tiers.map((t) => t.count))
              const total = tiers.reduce((a, t) => a + t.count, 0)
              const hub: [number, number] = [84, 50]
              const sat: [number, number][] = [[84, 16], [26, 82], [142, 82]]
              return (
                <>
                  {tiers.map((t, i) => (
                    <line key={'l' + t.id} className="fx-conv-mlink" x1={hub[0]} y1={hub[1]} x2={sat[i][0]} y2={sat[i][1]} style={{ strokeWidth: 1 + (t.count / max) * 3, stroke: t.color }} />
                  ))}
                  <circle className="fx-conv-mhub" cx={hub[0]} cy={hub[1]} r={13} />
                  <text className="fx-conv-mhubn" x={hub[0]} y={hub[1] + 4} textAnchor="middle">Σ{total}</text>
                  {tiers.map((t, i) => {
                    const r = 8 + Math.sqrt(t.count / max) * 12
                    return (
                      <g key={t.id}>
                        <circle className="fx-conv-mdot2" cx={sat[i][0]} cy={sat[i][1]} r={r} style={{ fill: t.color }} />
                        <text className="fx-conv-mn" x={sat[i][0]} y={sat[i][1] + 4} textAnchor="middle">{t.count}</text>
                        <text className="fx-conv-mk" x={sat[i][0]} y={i === 0 ? sat[i][1] - r - 5 : sat[i][1] + r + 11} textAnchor="middle">{t.label}</text>
                      </g>
                    )
                  })}
                </>
              )
            })()}
          </svg>
          {data.memory ? (
            <span className="fx-conv-ops">
              <span className="fx-conv-op"><b>Mem0</b>{data.memory.active}</span>
              <span className="fx-conv-op"><b>A-MEM</b>{data.memory.links}</span>
              <span className="fx-conv-op"><b>{zh ? '反思' : 'REFLECT'}</b>{data.memory.insights}</span>
              <span className="fx-conv-op"><b>{zh ? '遗忘' : 'DECAY'}</b>{data.memory.forgotten}</span>
            </span>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
