/* ── ② WRITE ROUTER · similarity ruler ────────────────────────────────────────
   route() decides ADD / UPDATE / NOOP by one number: cosine similarity against
   the nearest existing memory. The rule is two thresholds. This draws the rule
   AND every real call evaluated against it, so the reader learns the decision
   procedure and watches it run on the R230 held-out set at the same time.

   The zones above the UPDATE gate are drawn at full structural weight and
   carry a real count (0 / 6) — they were evaluated and nothing landed there.
   That is a measured result, not missing data, and it is never faked into.
   No --acid here: this component has no cursor, and --acid means "changed at
   this cursor step" only. Ink, gray and hatch carry the whole drawing. */
import './memory-inspector.css'
import type { MemEvent } from '../types'

/** Kernel constants — core/evolve route(). Shown on the axis, not hidden in code. */
const GATE_UPDATE = 0.62
const GATE_NOOP = 0.97

const L: Record<string, [string, string]> = {
  kick: ['写入路由 · route()', 'WRITE ROUTER · route()'],
  real: ['真实调用', 'REAL CALLS'],
  never: ['从未进入', 'NEVER ENTERED'],
  of: ['/', '/'],
  short: ['离闸门还差', 'SHORT OF GATE'],
  sum: ['本数据集路由结果', 'ROUTE RESULT ON THIS DATASET'],
  none: ['本数据集没有任何 route() 调用', 'No route() call was recorded on this dataset.'],
  obs: ['实测区间', 'OBSERVED'],
}
const t = (k: string, zh: boolean) => L[k][zh ? 0 : 1]

const VW = 600, VH = 140
const X0 = 44, X1 = 556, AX = 116          // axis
const BT = 100                             // zone strip top (16px tall, meets axis)
const R0 = 26, RP = 11                     // first call row + row pitch
const px = (s: number) => X0 + s * (X1 - X0)
const f4 = (n: number) => n.toFixed(4)
/** case_id "real_admin_bruteforce_lockout" → "admin_bruteforce_lockout" */
const shortCase = (c: string) => c.replace(/^real_/, '')

export function RouteRuler({ decisions, zh }: { decisions: MemEvent[]; zh: boolean }) {
  // only calls where route() genuinely ran and recorded a similarity.
  // Sorted DESCENDING so the longest labels ride the top rows, leaving the
  // right-hand corridor clear for the gate annotations.
  const calls = decisions
    .filter((d) => d.similarity !== null)
    .sort((a, b) => (b.similarity as number) - (a.similarity as number))
  const N = calls.length

  const zones = [
    { id: 'add', from: 0, to: GATE_UPDATE, name: 'ADD', op: '<', gate: GATE_UPDATE },
    { id: 'upd', from: GATE_UPDATE, to: GATE_NOOP, name: 'UPDATE', op: '≥', gate: GATE_UPDATE },
    { id: 'noop', from: GATE_NOOP, to: 1, name: 'NOOP', op: '≥', gate: GATE_NOOP },
  ].map((z) => {
    const hit = calls.filter((c) => (c.similarity as number) >= z.from && (c.similarity as number) < (z.to === 1 ? 1.01 : z.to))
    return { ...z, n: hit.length }
  })

  // the contiguous never-entered run at the top of the scale, measured — not assumed.
  // One bracket says "the data stops here", which reads as a result, not a gap.
  let voidFrom: number | null = null
  const voidNames: string[] = []
  for (let i = zones.length - 1; i >= 0; i--) {
    if (zones[i].n > 0) break
    voidFrom = zones[i].from
    voidNames.unshift(zones[i].name)
  }

  // calls is sorted DESCENDING, so calls[0] is the max and calls[N-1] the min.
  const maxSim = N ? (calls[0].similarity as number) : 0
  const minSim = N ? (calls[N - 1].similarity as number) : 0
  // the real headroom between the closest call and the first gate it never reached
  const gap = GATE_UPDATE - maxSim
  const showGap = N > 0 && gap > 0.02 && px(GATE_UPDATE) - px(maxSim) > 60

  return (
    <section className="mi-ruler">
      <svg viewBox={`0 0 ${VW} ${VH}`} preserveAspectRatio="xMidYMid meet" role="img"
        aria-label={zh
          ? `写入路由相似度标尺：${N} 次真实调用，${zones.map((z) => `${z.name} ${z.n}`).join('，')}`
          : `Write-router similarity ruler: ${N} real calls, ${zones.map((z) => `${z.name} ${z.n}`).join(', ')}`}>
        <defs>
          <pattern id="mi-hatch" width="7" height="7" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <line className="mi-hl" x1="0" y1="0" x2="0" y2="7" />
          </pattern>
        </defs>

        <text className="mi-r-kick" x={0} y={10}>{t('kick', zh)}</text>
        <text className="mi-r-kick" x={VW} y={10} textAnchor="end">{N} {t('real', zh)} · R230</text>
        <line className="mi-r-rule" x1={0} y1={17} x2={VW} y2={17} />

        {/* zone strip · every zone is drawn at full weight whether or not the data
            reached it. A hatched zone carrying a real 0/6 is a measured result. */}
        {zones.map((z) => {
          const x = px(z.from), w = px(z.to) - px(z.from)
          return (
            <g key={z.id} className={`mi-r-zone ${z.n ? 'hit' : 'void'}`}>
              <rect x={x} y={BT} width={w} height={AX - BT} />
              {w > 70 && (
                <text className="mi-r-zt" x={x + 5} y={BT + 11}>
                  {z.name} {z.op} {z.gate} · {z.n}{t('of', zh)}{N}
                </text>
              )}
            </g>
          )
        })}

        {/* the never-entered span · the single strongest honest statement here */}
        {voidFrom !== null && (
          <g className="mi-r-void">
            <path d={`M${px(voidFrom)} ${BT - 4} v-5 h${px(1) - px(voidFrom)} v5`} />
            <text className="mi-r-vt" x={(px(voidFrom) + px(1)) / 2} y={BT - 12} textAnchor="middle">
              {voidNames.join(' + ')} · 0 {t('of', zh)} {N} · {t('never', zh)}
            </text>
          </g>
        )}

        {/* the two gates · full-height walls, labelled */}
        {[GATE_UPDATE, GATE_NOOP].map((g) => (
          <line key={g} className="mi-r-gate" x1={px(g)} y1={22} x2={px(g)} y2={AX + 4} />
        ))}

        {/* one row per real call: stem from the axis up to its own line, then the
            value → decision it produced. All ticks sit left of the UPDATE gate
            because that is where the real similarities actually are. */}
        {calls.map((c, i) => {
          const s = c.similarity as number
          const x = px(s), y = R0 + i * RP
          return (
            <g className="mi-r-call" key={`${c.seq}-${c.memory_id}`} style={{ animationDelay: `${i * 40}ms` }}>
              <line className="mi-r-stem" x1={x} y1={AX} x2={x} y2={y} />
              <circle className="mi-r-dot" cx={x} cy={y} r={2.4} />
              <text className="mi-r-lab" x={x + 6} y={y + 3.4}>
                <tspan className="mi-r-val">{f4(s)}</tspan>
                <tspan className="mi-r-ar"> → </tspan>
                <tspan className="mi-r-op">{c.op}</tspan>
                <tspan className="mi-r-case">  {shortCase(c.case_id)}</tspan>
              </text>
            </g>
          )
        })}

        {/* the punchline: how far the closest real candidate stayed from the gate */}
        {showGap && (
          <g className="mi-r-gap">
            <path d={`M${px(maxSim)} ${BT - 4} h${px(GATE_UPDATE) - px(maxSim)}`} />
            <path d={`M${px(maxSim)} ${BT - 8} v8 M${px(GATE_UPDATE)} ${BT - 8} v8`} />
            <text className="mi-r-gt" x={(px(maxSim) + px(GATE_UPDATE)) / 2} y={BT - 7} textAnchor="middle">
              {gap.toFixed(2)} {t('short', zh)}
            </text>
          </g>
        )}

        {/* axis */}
        <line className="mi-r-ax" x1={X0} y1={AX} x2={X1} y2={AX} />
        {[0, 0.25, 0.5, 0.75, 1].map((v) => (
          <g key={v}>
            <line className="mi-r-ax-t" x1={px(v)} y1={AX} x2={px(v)} y2={AX + 3} />
            <text className="mi-r-ax-n" x={px(v)} y={AX + 11} textAnchor="middle">{v}</text>
          </g>
        ))}
        {/* calls are sorted descending, so the real span is last → first */}
        <text className="mi-r-ax-l" x={X0} y={AX + 22}>
          {t('obs', zh)} {N ? `${f4(minSim)}–${f4(maxSim)}` : '—'}
        </text>
        <text className="mi-r-sum" x={X1} y={AX + 22} textAnchor="end">
          {t('sum', zh)} — {zones.map((z) => `${z.name} ×${z.n}`).join(' · ')}
        </text>
        {!N && <text className="mi-r-none" x={VW / 2} y={64} textAnchor="middle">{t('none', zh)}</text>}
      </svg>
    </section>
  )
}
