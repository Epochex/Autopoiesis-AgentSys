import { useEffect, useState } from 'react'

/* ── ③ TACTICAL REPLAY CANVAS ─────────────────────────────────────────────────
   A single connected spatial diagram (ctOS system-map / Division ISAC in spirit).
   The 7-stage read-only reasoning chain is a zig-zag constellation wired by drawn
   data-flow links, and the DECISIONS are drawn as forks, all from real trace:
     · TOOL FILTER   → pool of known tools → funnel → chosen chips (solid) vs
                       skipped chips (dashed grey) — "considered N, chose M"
     · COMPRESS fork → kept evidence rides the solid edge; a dashed stub carries
                       the dropped count (real included_evidence_ids / missing)
     · CHECK gate    → the ✓ path actually taken vs the dashed ✕ reject stub
   The active stage is reticle-locked and mirrored into the acid HERO PROFILER.
   All values are real trace payload — nothing invented. */

export type FxUnit = 'int' | 'pct' | 'x' | 'conf'
export type FxReadout = { op: string; body: string; glyph?: 'probe' }
export type FxStation = {
  no: string
  name: string
  role: string
  kind: string
  cat: string
  metric: { value: number; unit: FxUnit; caption: string } | null
  readout?: FxReadout[]
  loadBearing?: boolean
}
export type FxEvidence = { id: string; sum: string; raw: string; pinned: boolean; included: boolean; cited: boolean; verified: boolean }
export type FxMemTier = { label: string; count: number }
export type FxSkillChip = { id: string; label: string; called: boolean }
export type FxSkills = { poolCount: number; chosen: FxSkillChip[]; skippedCount: number; skippedNames: string }
export type FxFork = { kept: number; dropped: number }
export type FxProbes = { available: number; run: number }
export type FxVerify = { passed: boolean; recall: number }

/* ── geometry (fixed viewBox, scales uniformly to container) ── */
const VW = 1440, VH = 900
const NW = 154, NH = 94
type XY = [number, number]
const POS: XY[] = [
  [152, 250], // 01 alert
  [152, 476], // 02 memory
  [388, 366], // 03 skills
  [620, 476], // 04 probe
  [852, 366], // 05 compress
  [1064, 476], // 06 verify
  [1300, 288], // 07 verdict
]
const posOf = (i: number): XY => POS[i] ?? [150 + i * 176, i % 2 ? 476 : 288]

const CAT_COLOR: Record<string, string> = {
  alert: '#d6335a', memory: '#4c9d94', skill: '#ff7a6b', probe: '#2b3d38',
  context: '#ffcfa0', verify: '#a8bfa0', verdict: '#0d0d0d',
}

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + '…' : s)
const fmt = (n: number, unit: FxUnit) => (unit === 'x' || unit === 'conf' ? n.toFixed(2) : String(Math.round(n)))
const unitSuffix = (u: FxUnit) => (u === 'pct' ? '%' : u === 'x' ? '×' : '')

// point on a node's rectangle boundary along the direction toward (tx,ty)
function edge(cx: number, cy: number, tx: number, ty: number, w = NW, h = NH): XY {
  const dx = tx - cx, dy = ty - cy
  const sx = dx ? (w / 2) / Math.abs(dx) : Infinity
  const sy = dy ? (h / 2) / Math.abs(dy) : Infinity
  const s = Math.min(sx, sy)
  return [cx + dx * s, cy + dy * s]
}

/* ── tiny flat-print icons (no words) ── */
// observe-only: an eye
const EyeIcon = ({ x, y }: { x: number; y: number }) => (
  <g className="fx-ico" transform={`translate(${x} ${y})`}>
    <path d="M0 6 Q9 -2 18 6 Q9 14 0 6 Z" />
    <circle cx={9} cy={6} r={2.6} />
  </g>
)
// no-write / read-only clamp: a padlock
const LockIcon = ({ x, y, s = 1 }: { x: number; y: number; s?: number }) => (
  <g className="fx-ico" transform={`translate(${x} ${y}) scale(${s})`}>
    <rect x={0} y={6} width={12} height={9} />
    <path d="M2.5 6 V4.5 a3.5 3.5 0 0 1 7 0 V6" />
  </g>
)
// evidence id as a barcode fingerprint — raw id only on hover
function Barcode({ id, x, y, h = 14 }: { id: string; x: number; y: number; h?: number }) {
  const bars: { x: number; w: number }[] = []
  let bx = 0
  for (let i = 0; i < Math.min(id.length, 16); i++) {
    const w = 1 + (id.charCodeAt(i) % 3)
    bars.push({ x: bx, w })
    bx += w + 2
  }
  return (
    <g className="fx-bars" transform={`translate(${x} ${y})`}>
      <title>{id}</title>
      {bars.map((b, i) => <rect key={i} x={b.x} y={0} width={b.w} height={h} />)}
    </g>
  )
}
// evidence lifecycle pips: pin / box (packed) / quote (cited) — lit or dim
function Pip({ x, y, kind, on, label }: { x: number; y: number; kind: 'pin' | 'box' | 'quote'; on: boolean; label: string }) {
  return (
    <g className={`fx-pip ${on ? 'on' : ''}`} transform={`translate(${x} ${y})`}>
      <title>{label}</title>
      <rect className="fx-pip-cell" x={0} y={0} width={18} height={18} />
      {kind === 'pin' ? (<><circle cx={9} cy={6.5} r={3} /><line x1={9} y1={9.5} x2={9} y2={14} /></>)
        : kind === 'box' ? (<><rect x={4.5} y={5} width={9} height={8.5} /><line x1={4.5} y1={8} x2={13.5} y2={8} /></>)
          : (<path d="M6 5.5 q-2.4 3.6 0 7.5 M12 5.5 q-2.4 3.6 0 7.5" />)}
    </g>
  )
}

/* count-up that only runs while a station is live cursor */
function useCountUp(target: number, run: boolean, dur = 820) {
  const [n, setN] = useState(0)
  useEffect(() => {
    if (!run) return
    let raf = 0
    const start = performance.now()
    const tick = (t: number) => {
      const p = Math.min(1, (t - start) / dur)
      const e = 1 - Math.pow(1 - p, 3)
      setN(target * e)
      if (p < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [target, run, dur])
  return run ? n : target
}

function NodeMetric({ m, active, pending }: { m: FxStation['metric']; active: boolean; pending: boolean }) {
  const shown = useCountUp(m?.value ?? 0, active)
  if (!m) return null
  if (pending) return <text className="fx-node-metric pend" x={0} y={0}>·</text>
  return (
    <>
      <text className="fx-node-metric" x={0} y={0}>{fmt(shown, m.unit)}<tspan className="fx-node-u">{unitSuffix(m.unit)}</tspan></text>
      <text className="fx-node-cap" x={2} y={13}>{m.caption}</text>
    </>
  )
}

// the reticle-locked hero read of whichever stage is currently active
function HeroBig({ m }: { m: FxStation['metric'] }) {
  const shown = useCountUp(m?.value ?? 0, true, 900)
  if (!m) return null
  return <text className="fx-hero-big" x={0} y={0}>{fmt(shown, m.unit)}<tspan className="fx-hero-u">{unitSuffix(m.unit)}</tspan></text>
}

export function FlowGraph({
  stations, evidence, memory, memoryTotal, skills, ctxFork, probes, verify, reached, cursor, zh, onSeek,
}: {
  stations: FxStation[]
  evidence: FxEvidence[]
  memory: FxMemTier[]
  memoryTotal: number
  skills: FxSkills
  ctxFork: FxFork
  probes: FxProbes
  verify: FxVerify
  reached: number
  cursor: number
  zh: boolean
  onSeek: (i: number) => void
}) {
  const [hoverStn, setHoverStn] = useState<number | null>(null)
  const [hoverEvi, setHoverEvi] = useState<number | null>(null)

  const idx = (k: string) => stations.findIndex((s) => s.kind === k)
  const memIdx = idx('memory_read'), skIdx = idx('skills_exposed'), probeIdx = idx('tool_called')
  const ctxIdx = idx('context_compiled'), verIdx = idx('verifier_result'), diagIdx = idx('diagnosis_completed')
  const provStations = [probeIdx, verIdx, diagIdx].filter((i) => i >= 0)

  // ── connection-driven cross-highlight ──
  const hovering = hoverStn !== null || hoverEvi !== null
  const hotStn = new Set<number>()
  const hotEvi = new Set<number>()
  const hotBus = new Set<string>()  // 'pin' | 'verify' | 'cite' | 'mem' | 'skill'
  if (hoverEvi !== null) {
    hotEvi.add(hoverEvi)
    const e = evidence[hoverEvi]
    if (e?.pinned && probeIdx >= 0) { hotStn.add(probeIdx); hotBus.add('pin') }
    if (e?.cited && verIdx >= 0) { hotStn.add(verIdx); hotBus.add('verify') }
    if (e?.cited && diagIdx >= 0) { hotStn.add(diagIdx); hotBus.add('cite') }
  } else if (hoverStn !== null) {
    hotStn.add(hoverStn)
    if (hoverStn === memIdx) hotBus.add('mem')
    if (hoverStn === skIdx) hotBus.add('skill')
    if (provStations.includes(hoverStn)) {
      if (hoverStn === probeIdx) hotBus.add('pin')
      if (hoverStn === verIdx) hotBus.add('verify')
      if (hoverStn === diagIdx) hotBus.add('cite')
      evidence.forEach((e, j) => { if (e.pinned || e.cited) hotEvi.add(j) })
    }
  }
  const stnCls = (i: number) => hovering ? (hotStn.has(i) ? 'hot' : 'dim') : ''
  const eviCls = (j: number) => hovering ? (hotEvi.has(j) ? 'hot' : 'dim') : ''
  const busCls = (b: string) => hovering ? (hotBus.has(b) ? 'hot' : 'dim') : ''

  // ── module + card frames ──
  const memBox = { x: 40, y: 604, w: 300, h: 170 }        // memory store (lower-left)
  const skBox = { x: 246, y: 88, w: 390, h: 152 }          // tool filter (top)
  const cards = evidence.slice(0, 2)
  const CARD_X = 986, CARD_W = 420, CARD_H = 140, CARD_GAP = 12
  const cardBox = (j: number) => ({ x: CARD_X, y: 604 + j * (CARD_H + CARD_GAP), w: CARD_W, h: CARD_H })
  const cardCenter = (j: number): XY => { const b = cardBox(j); return [b.x + b.w / 2, b.y + b.h / 2] }

  // hero profiler (focal)
  const hero = { x: 360, y: 600, w: 596, h: 288 }
  const cur = stations[cursor] ?? stations[0]
  const curPos = posOf(cursor)
  const heroAnchorX = Math.max(hero.x + 60, Math.min(hero.x + hero.w - 60, curPos[0]))

  const memLive = memIdx >= 0 && reached >= memIdx
  const skLive = skIdx >= 0 && reached >= skIdx
  const ctxLive = ctxIdx >= 0 && reached >= ctxIdx
  const gateLive = verIdx >= 0 && reached >= verIdx

  // anchor geometry
  const memC = posOf(memIdx >= 0 ? memIdx : 1)
  const skC = posOf(skIdx >= 0 ? skIdx : 2)
  const probeC = posOf(probeIdx >= 0 ? probeIdx : 3)
  const ctxC = posOf(ctxIdx >= 0 ? ctxIdx : 4)
  const verC = posOf(verIdx >= 0 ? verIdx : 5)
  const diagC = posOf(diagIdx >= 0 ? diagIdx : 6)

  // verifier gate sits on the verify→verdict edge
  const gs = edge(verC[0], verC[1], diagC[0], diagC[1])
  const ge = edge(diagC[0], diagC[1], verC[0], verC[1])
  const gate: XY = [gs[0] + (ge[0] - gs[0]) * 0.46, gs[1] + (ge[1] - gs[1]) * 0.46]

  // compress fork: kept rides the solid edge to verify; dropped exits on a dashed stub
  const keptT = 0.62
  const keptTag: XY = [ctxC[0] + (verC[0] - ctxC[0]) * keptT, ctxC[1] + (verC[1] - ctxC[1]) * keptT]

  const probesSkipped = Math.max(0, probes.available - probes.run)

  return (
    <div className="fx-stage">
      <svg className={`fx-canvas ${hovering ? 'hov' : ''}`} viewBox={`0 0 ${VW} ${VH}`} preserveAspectRatio="xMidYMid meet" role="img"
        onMouseLeave={() => { setHoverStn(null); setHoverEvi(null) }}>
        <defs>
          <pattern id="fx-dots" width={22} height={22} patternUnits="userSpaceOnUse"><circle cx={1} cy={1} r={1} fill="var(--rule)" /></pattern>
          <pattern id="fx-hatch" width={7} height={7} patternTransform="rotate(45)" patternUnits="userSpaceOnUse"><rect width={2.4} height={7} fill="var(--ink)" /></pattern>
          <marker id="fx-ar" markerWidth={9} markerHeight={9} refX={6} refY={3} orient="auto"><path d="M0 0 L6.5 3 L0 6 Z" fill="var(--ink)" /></marker>
          <marker id="fx-ar-d" markerWidth={8} markerHeight={8} refX={5.5} refY={3} orient="auto"><path d="M0 0 L6 3 L0 6 Z" fill="var(--gray)" /></marker>
        </defs>

        {/* ── field + HUD frame chrome ── */}
        <rect x={0} y={0} width={VW} height={VH} fill="url(#fx-dots)" opacity={0.7} />
        <g className="fx-frame">
          <path d="M30 30 h30 M30 30 v30" /><path d={`M${VW - 30} 30 h-30 M${VW - 30} 30 v30`} />
          <path d={`M30 ${VH - 30} h30 M30 ${VH - 30} v-30`} /><path d={`M${VW - 30} ${VH - 30} h-30 M${VW - 30} ${VH - 30} v-30`} />
        </g>
        {/* coordinate ticks along the top edge */}
        <g className="fx-coord">{Array.from({ length: 24 }).map((_, i) => <line key={i} x1={72 + i * 56} y1={30} x2={72 + i * 56} y2={i % 4 === 0 ? 42 : 36} />)}</g>
        {/* observe-only + no-write, as icons */}
        <EyeIcon x={44} y={44} />
        <LockIcon x={74} y={42} />

        {/* wireframe globe motif (ambient, top-right void) */}
        <g className="fx-globe" transform="translate(1240 168)">
          <circle r={46} /><ellipse rx={46} ry={17} /><ellipse rx={30} ry={46} /><ellipse rx={15} ry={46} />
          <line x1={-46} y1={0} x2={46} y2={0} /><line x1={0} y1={-46} x2={0} y2={46} />
        </g>

        {/* ambient scan sweep (atmosphere) */}
        <g className="fx-sweep"><line x1={0} y1={44} x2={0} y2={VH - 44} /></g>

        {/* ══ CONNECTIVE TISSUE (drawn first, sits under nodes) ══ */}
        {/* spine links between consecutive stages */}
        {stations.slice(0, -1).map((_, i) => {
          const a = posOf(i), b = posOf(i + 1)
          const s = edge(a[0], a[1], b[0], b[1]), e = edge(b[0], b[1], a[0], a[1])
          const on = reached >= i + 1
          const isGateEdge = i === verIdx && i + 1 === diagIdx
          const d = `M${s[0]} ${s[1]} L${e[0]} ${e[1]}`
          return (
            <g key={`lnk${i}`} className={`fx-link ${on ? 'on' : ''} ${hovering ? (hotStn.has(i) && hotStn.has(i + 1) ? 'hot' : 'dim') : ''}`}>
              <path className="fx-link-l" d={d} markerEnd={isGateEdge ? undefined : 'url(#fx-ar)'} />
              {on && !isGateEdge ? <circle className="fx-pulse" r={4}><animateMotion dur="1.6s" repeatCount="indefinite" path={d} /></circle> : null}
            </g>
          )
        })}

        {/* memory-store feed → 记忆 */}
        <g className={`fx-feed ${memLive ? 'on' : ''} ${busCls('mem')}`}>
          <path className="fx-feed-l" d={`M${memBox.x + memBox.w / 2} ${memBox.y} L${memBox.x + memBox.w / 2} ${memC[1] + NH / 2 + 4}`} markerEnd="url(#fx-ar)" />
          <text className="fx-feed-t" x={memBox.x + memBox.w / 2 + 8} y={(memBox.y + memC[1] + NH / 2) / 2}>{zh ? '调记忆' : 'RECALL'}</text>
          {memLive ? <circle className="fx-pulse sm" r={3}><animateMotion dur="1.8s" repeatCount="indefinite" path={`M${memBox.x + memBox.w / 2} ${memBox.y} L${memBox.x + memBox.w / 2} ${memC[1] + NH / 2 + 4}`} /></circle> : null}
        </g>
        {/* tool-filter feed → 技能 · read-only clamp = padlock, no words */}
        <g className={`fx-feed ${skLive ? 'on' : ''} ${busCls('skill')}`}>
          <path className="fx-feed-l" d={`M${skBox.x + skBox.w / 2} ${skBox.y + skBox.h} L${skBox.x + skBox.w / 2} ${skC[1] - NH / 2 - 4}`} markerEnd="url(#fx-ar)" />
          <LockIcon x={skBox.x + skBox.w / 2 + 8} y={(skBox.y + skBox.h + skC[1] - NH / 2) / 2 - 10} s={0.85} />
          {skLive ? <circle className="fx-pulse sm" r={3}><animateMotion dur="1.8s" repeatCount="indefinite" path={`M${skBox.x + skBox.w / 2} ${skBox.y + skBox.h} L${skBox.x + skBox.w / 2} ${skC[1] - NH / 2 - 4}`} /></circle> : null}
        </g>

        {/* evidence tethers: PROBE pin (down), VERIFY verify, VERDICT cite */}
        {cards.map((e, j) => {
          const c = cardCenter(j)
          const pinS = edge(probeC[0], probeC[1], c[0], c[1])
          const citeS = edge(diagC[0], diagC[1], c[0], c[1])
          const verS = edge(verC[0], verC[1], c[0], c[1])
          const cb = cardBox(j)
          const toCard = (from: XY): string => `M${from[0]} ${from[1]} L${cb.x} ${c[1]}`
          return (
            <g key={`teth${j}`}>
              {e.pinned ? <g className={`fx-teth pin ${reached >= (probeIdx < 0 ? 3 : probeIdx) ? 'on' : ''} ${hovering ? (hotBus.has('pin') && hotEvi.has(j) ? 'hot' : 'dim') : ''}`}>
                <path className="fx-teth-l" d={toCard(pinS)} markerEnd="url(#fx-ar-d)" />
              </g> : null}
              {e.cited ? <g className={`fx-teth cite ${diagIdx >= 0 && reached >= diagIdx ? 'on' : ''} ${hovering ? (hotBus.has('cite') && hotEvi.has(j) ? 'hot' : 'dim') : ''}`}>
                <path className="fx-teth-l" d={toCard(citeS)} markerEnd="url(#fx-ar-d)" />
              </g> : null}
              {e.cited ? <g className={`fx-teth verify ${verIdx >= 0 && reached >= verIdx ? 'on' : ''} ${hovering ? (hotBus.has('verify') && hotEvi.has(j) ? 'hot' : 'dim') : ''}`}>
                <path className="fx-teth-l" d={toCard(verS)} markerEnd="url(#fx-ar-d)" />
              </g> : null}
            </g>
          )
        })}

        {/* ══ COMPRESS FORK — kept evidence rides the main edge, dropped exits on a stub ══ */}
        <g className={`fx-fork ${ctxLive ? 'on' : ''}`}>
          <g className="fx-keep-tag">
            <rect x={keptTag[0] - 32} y={keptTag[1] - 8} width={64} height={16} />
            <text x={keptTag[0]} y={keptTag[1] + 3.5} textAnchor="middle">{zh ? `留 ${ctxFork.kept}` : `keep ${ctxFork.kept}`}</text>
          </g>
          <path className="fx-branch" d={`M${ctxC[0] + 32} ${ctxC[1] + NH / 2 + 2} L${ctxC[0] + 86} ${ctxC[1] + NH / 2 + 58}`} markerEnd="url(#fx-ar-d)" />
          <rect className="fx-branch-end" x={ctxC[0] + 80} y={ctxC[1] + NH / 2 + 58} width={12} height={12} />
          <text className="fx-branch-t" x={ctxC[0] + 98} y={ctxC[1] + NH / 2 + 68}>{zh ? `丢 ${ctxFork.dropped}` : `drop ${ctxFork.dropped}`}</text>
        </g>

        {/* ══ PROBE FORK — read-only checks run vs exposed-but-unused (only if real) ══ */}
        {probesSkipped > 0 ? (
          <g className={`fx-fork ${reached >= probeIdx ? 'on' : ''}`}>
            <path className="fx-branch" d={`M${probeC[0] + 40} ${probeC[1] + NH / 2 + 2} L${probeC[0] + 80} ${probeC[1] + NH / 2 + 44}`} markerEnd="url(#fx-ar-d)" />
            <text className="fx-branch-t" x={probeC[0] + 86} y={probeC[1] + NH / 2 + 52}>{zh ? `没用 ${probesSkipped}` : `unused ${probesSkipped}`}</text>
          </g>
        ) : null}

        {/* ══ CHECK GATE on the 核验 → 结论 edge — ✓ path taken, ✕ stub not taken ══ */}
        <g className={`fx-gate ${gateLive ? 'on' : ''} ${verify.passed ? 'pass' : 'reject'}`} transform={`translate(${gate[0]} ${gate[1]})`}>
          <path className="fx-gate-body" d="M0 -20 L20 0 L0 20 L-20 0 Z" />
          <path className="fx-gate-x" d="M-7 -7 L7 7 M-7 7 L7 -7" />
          <text className="fx-gate-t" x={0} y={-28} textAnchor="middle">{zh ? '核对' : 'CHECK'}</text>
          {/* verdict mark only — no words */}
          <g className={`fx-gate-mark ${verify.passed ? 'ok' : 'no'}`}>
            <rect x={-32} y={22} width={18} height={18} />
            <text x={-23} y={35.5} textAnchor="middle">{verify.passed ? '✓' : '✕'}</text>
          </g>
          {/* the branch NOT taken */}
          <g className={`fx-gate-stub ${verify.passed ? 'idle' : 'taken'}`}>
            <path className="fx-branch" d="M14 8 L48 46" markerEnd="url(#fx-ar-d)" />
            <text className="fx-gate-stub-x" x={54} y={58} textAnchor="middle">{verify.passed ? '✕' : '✓'}</text>
            <text className="fx-branch-t" x={54} y={72} textAnchor="middle">{verify.passed ? (zh ? '退回' : 'reject') : (zh ? '通过' : 'pass')}</text>
          </g>
        </g>

        {/* ══ MEMORY STORE (docked subsystem) ══ */}
        <g className={`fx-mod ${memLive ? 'on' : ''} ${stnCls(memIdx)}`} onMouseEnter={() => setHoverStn(memIdx)} onMouseLeave={() => setHoverStn(null)}>
          <rect className="fx-mod-box" x={memBox.x} y={memBox.y} width={memBox.w} height={memBox.h} />
          <rect className="fx-mod-strip" x={memBox.x} y={memBox.y} width={memBox.w} height={4} fill={CAT_COLOR.memory} />
          <text className="fx-mod-tag" x={memBox.x + 12} y={memBox.y + 24}>{zh ? '记忆库' : 'MEMORY'}</text>
          <text className="fx-mod-sub" x={memBox.x + memBox.w - 12} y={memBox.y + 24} textAnchor="end">{zh ? `共 ${memoryTotal}` : `${memoryTotal} total`}</text>
          {/* tier store as a mesh: satellites sized by recall count, link weight ∝ count */}
          {(() => {
            const hubX = memBox.x + memBox.w / 2, hubY = memBox.y + 100
            const max = Math.max(1, ...memory.map((m) => m.count))
            const off: XY[] = [[-95, -38], [95, -38], [-95, 40], [95, 40]]
            return (
              <>
                {memory.slice(0, 4).map((m, k) => {
                  const sx = hubX + off[k][0], sy = hubY + off[k][1]
                  return <line key={'l' + m.label} className="fx-mesh-link" x1={hubX} y1={hubY} x2={sx} y2={sy} style={{ strokeWidth: 1 + (m.count / max) * 3, opacity: memLive ? (m.count ? 0.9 : 0.35) : 0.25 }} />
                })}
                <circle className="fx-mesh-hub" cx={hubX} cy={hubY} r={13} />
                <text className="fx-mesh-hubn" x={hubX} y={hubY + 4} textAnchor="middle">{memoryTotal}</text>
                {memory.slice(0, 4).map((m, k) => {
                  const sx = hubX + off[k][0], sy = hubY + off[k][1]
                  const r = 6 + Math.sqrt(m.count / max) * 10
                  const top = off[k][1] < 0
                  return (
                    <g key={m.label} className={`fx-mesh-sat ${m.count ? '' : 'empty'}`} style={{ opacity: memLive ? 1 : 0.4 }}>
                      <circle className="fx-mesh-dot" cx={sx} cy={sy} r={r} />
                      <text className="fx-mesh-n" x={sx} y={sy + 4} textAnchor="middle">{m.count}</text>
                      <text className="fx-mesh-k" x={sx} y={top ? sy - r - 6 : sy + r + 12} textAnchor="middle">{m.label}</text>
                    </g>
                  )
                })}
              </>
            )
          })()}
        </g>

        {/* ══ TOOL FILTER (docked subsystem) — the visible choice: pool → chosen / skipped ══ */}
        <g className={`fx-mod ${skLive ? 'on' : ''} ${stnCls(skIdx)}`} onMouseEnter={() => setHoverStn(skIdx)} onMouseLeave={() => setHoverStn(null)}>
          <rect className="fx-mod-box" x={skBox.x} y={skBox.y} width={skBox.w} height={skBox.h} />
          <rect className="fx-mod-strip" x={skBox.x} y={skBox.y} width={skBox.w} height={4} fill={CAT_COLOR.skill} />
          <text className="fx-mod-tag" x={skBox.x + 12} y={skBox.y + 24}>{zh ? '工具筛选' : 'TOOL FILTER'}</text>
          <LockIcon x={skBox.x + skBox.w - 26} y={skBox.y + 10} s={0.85} />
          {/* the full pool of known tools */}
          <path className="fx-hatchbox" d={`M${skBox.x + 16} ${skBox.y + 44} h46 v60 h-46 Z`} />
          <text className="fx-pool-n" x={skBox.x + 39} y={skBox.y + 121} textAnchor="middle">{skills.poolCount}</text>
          <text className="fx-mod-mini" x={skBox.x + 39} y={skBox.y + 135} textAnchor="middle">{zh ? '全部工具' : 'ALL TOOLS'}</text>
          {/* the narrowing funnel IS the filter — no score text */}
          <path className="fx-funnel" d={`M${skBox.x + 70} ${skBox.y + 46} L${skBox.x + 126} ${skBox.y + 70} L${skBox.x + 126} ${skBox.y + 96} L${skBox.x + 70} ${skBox.y + 120} Z`} />
          {/* chosen branch — solid */}
          {skills.chosen.slice(0, 3).map((s, i) => {
            const yy = skBox.y + 42 + i * 24
            return (
              <g key={s.id} className="fx-sk-row" style={{ opacity: skLive ? 1 : 0.35 }}>
                <line className="fx-sk-link" x1={skBox.x + 126} y1={skBox.y + 83} x2={skBox.x + 158} y2={yy + 9} />
                <rect className="fx-sk-chip" x={skBox.x + 158} y={yy} width={150} height={18} />
                <text className="fx-sk-t" x={skBox.x + 165} y={yy + 13}>{clip(s.label, 16)}<title>{s.id}</title></text>
                {s.called ? <text className="fx-sk-ok" x={skBox.x + 158 + 150 - 8} y={yy + 13} textAnchor="end">✓</text> : null}
              </g>
            )
          })}
          {/* skipped branch — dashed grey, the tools it did NOT pick */}
          {skills.skippedCount > 0 ? (
            <g className="fx-sk-row skip" style={{ opacity: skLive ? 1 : 0.35 }}>
              <line className="fx-sk-link skip" x1={skBox.x + 126} y1={skBox.y + 83} x2={skBox.x + 158} y2={skBox.y + 127} />
              <rect className="fx-sk-chip skip" x={skBox.x + 158} y={skBox.y + 118} width={150} height={18} />
              <text className="fx-sk-t skip" x={skBox.x + 165} y={skBox.y + 131}>{zh ? `没选 ${skills.skippedCount}` : `skipped ${skills.skippedCount}`}<title>{skills.skippedNames}</title></text>
            </g>
          ) : null}
          {/* considered N → chose M */}
          <text className="fx-sk-sum" x={skBox.x + skBox.w - 14} y={skBox.y + skBox.h - 12} textAnchor="end">{skills.poolCount} → {skills.chosen.length}</text>
        </g>

        {/* ══ EVIDENCE CARDS (ctOS profiler cards, floating) ══ */}
        <text className="fx-cards-lead" x={CARD_X} y={588}>{zh ? '证据来源' : 'EVIDENCE'}</text>
        {cards.map((e, j) => {
          const b = cardBox(j)
          return (
            <g key={e.id} className={`fx-card ${e.verified ? 'ver' : ''} ${eviCls(j)}`}
              onMouseEnter={() => setHoverEvi(j)} onMouseLeave={() => setHoverEvi(null)}>
              <rect className="fx-card-box" x={b.x} y={b.y} width={b.w} height={b.h} />
              <rect className="fx-card-strip" x={b.x} y={b.y} width={4} height={b.h} fill={CAT_COLOR.probe} />
              <rect className="fx-card-tag" x={b.x + 14} y={b.y + 12} width={34} height={20} />
              <text className="fx-card-ref" x={b.x + 31} y={b.y + 26} textAnchor="middle">E{String(j + 1).padStart(2, '0')}</text>
              <Barcode id={e.id} x={b.x + 58} y={b.y + 15} />
              <g className={`fx-card-st ${e.verified ? 'ok' : 'obs'}`}>
                <rect x={b.x + b.w - 112} y={b.y + 12} width={98} height={20} />
                <text x={b.x + b.w - 63} y={b.y + 26} textAnchor="middle">{e.verified ? (zh ? '✓ 已核对' : '✓ VERIFIED') : (zh ? '◇ 已观测' : '◇ OBSERVED')}</text>
              </g>
              <line className="fx-card-rule" x1={b.x + 14} y1={b.y + 42} x2={b.x + b.w - 14} y2={b.y + 42} />
              <text className="fx-card-sum" x={b.x + 14} y={b.y + 62}>{clip(e.sum, 54)}<title>{e.sum}</title></text>
              <text className="fx-card-h" x={b.x + 14} y={b.y + 84}>{zh ? '来源' : 'SOURCE'}</text>
              <text className="fx-card-raw" x={b.x + (zh ? 44 : 62)} y={b.y + 84}>{clip(e.raw, zh ? 46 : 44)}<title>{e.raw}</title></text>
              {/* lifecycle pips: pinned / packed / cited */}
              <Pip x={b.x + 14} y={b.y + b.h - 30} kind="pin" on={e.pinned} label={zh ? '钉实证据' : 'pinned'} />
              <Pip x={b.x + 38} y={b.y + b.h - 30} kind="box" on={e.included} label={zh ? '装入上下文' : 'packed'} />
              <Pip x={b.x + 62} y={b.y + b.h - 30} kind="quote" on={e.cited} label={zh ? '被结论引用' : 'cited'} />
            </g>
          )
        })}

        {/* ══ THE 7 STAGE NODES ══ */}
        {stations.map((s, i) => {
          const [cx, cy] = posOf(i)
          const x = cx - NW / 2, y = cy - NH / 2
          const pending = i > reached, active = i === cursor && !pending
          return (
            <g key={s.no} className={`fx-node ${pending ? 'pend' : active ? 'active' : 'done'} ${s.loadBearing ? 'load' : ''} ${stnCls(i)}`}
              onMouseEnter={() => setHoverStn(i)} onMouseLeave={() => setHoverStn(null)}
              onClick={() => onSeek(i)} style={{ cursor: 'pointer' }}>
              <rect className="fx-node-bg" x={x} y={y} width={NW} height={NH} />
              <rect className="fx-node-strip" x={x} y={y} width={NW} height={5} fill={CAT_COLOR[s.cat] ?? '#0d0d0d'} />
              <text className="fx-node-no" x={x + 12} y={y + 24}>{s.no}</text>
              <text className="fx-node-name" x={x + 12} y={y + 52}>{s.name}</text>
              <line className="fx-node-rule" x1={x + 12} y1={y + 61} x2={x + NW - 12} y2={y + 61} />
              <g transform={`translate(${x + 13} ${y + NH - 20})`}><NodeMetric m={s.metric} active={active} pending={pending} /></g>
              {s.loadBearing ? <text className="fx-node-load" x={x + NW - 11} y={y + NH - 10} textAnchor="end">{zh ? '◼ 最关键' : '◼ MOST CRITICAL'}</text> : null}
              {/* reticle lock on the active stage */}
              {active ? (
                <g className="fx-reticle">
                  <path d={`M${x - 8} ${y - 8} h16 M${x - 8} ${y - 8} v16`} />
                  <path d={`M${x + NW + 8} ${y - 8} h-16 M${x + NW + 8} ${y - 8} v16`} />
                  <path d={`M${x - 8} ${y + NH + 8} h16 M${x - 8} ${y + NH + 8} v-16`} />
                  <path d={`M${x + NW + 8} ${y + NH + 8} h-16 M${x + NW + 8} ${y + NH + 8} v-16`} />
                </g>
              ) : null}
            </g>
          )
        })}

        {/* ══ HERO PROFILER — the reticle pulls the active stage into the focal read ══ */}
        <g className="fx-hero-lead">
          <path className="fx-hero-lead-l" d={`M${curPos[0]} ${curPos[1] + NH / 2 + 8} L${curPos[0]} ${hero.y - 22} L${heroAnchorX} ${hero.y - 22} L${heroAnchorX} ${hero.y}`} />
          <circle className="fx-hero-lead-o" cx={curPos[0]} cy={curPos[1] + NH / 2 + 8} r={3} />
        </g>
        <g className="fx-hero" key={`hero-${cursor}-${reached}`}>
          <rect className="fx-hero-box" x={hero.x} y={hero.y} width={hero.w} height={hero.h} />
          <rect className="fx-hero-strip" x={hero.x} y={hero.y} width={hero.w} height={5} fill={CAT_COLOR[cur.cat] ?? '#0d0d0d'} />
          {/* header */}
          <text className="fx-hero-kick" x={hero.x + 20} y={hero.y + 28}>{zh ? '当前步骤' : 'CURRENT STEP'} · {cur.no}/{String(stations.length).padStart(2, '0')}</text>
          <text className="fx-hero-name" x={hero.x + 18} y={hero.y + 78}>{cur.name}</text>
          <text className="fx-hero-role" x={hero.x + 20} y={hero.y + 100}>▸ {cur.role}</text>
          {/* big metric */}
          <g transform={`translate(${hero.x + hero.w - 24} ${hero.y + 86})`} textAnchor="end"><HeroBig m={cur.metric} /></g>
          <text className="fx-hero-cap" x={hero.x + hero.w - 24} y={hero.y + 104} textAnchor="end">{cur.metric?.caption}</text>
          <line className="fx-hero-rule" x1={hero.x + 18} y1={hero.y + 118} x2={hero.x + hero.w - 18} y2={hero.y + 118} />
          {/* live readout */}
          <text className="fx-hero-live" x={hero.x + 20} y={hero.y + 140}>{zh ? '实时' : 'LIVE'}</text>
          {(cur.readout ?? []).slice(0, 3).map((r, i) => {
            const yy = hero.y + 164 + i * 32
            return (
              <g key={i} className="fx-hero-line" style={{ animationDelay: `${140 + i * 130}ms` }}>
                <rect className="fx-hero-op-bg" x={hero.x + 20} y={yy - 13} width={64} height={19} />
                <text className="fx-hero-op" x={hero.x + 52} y={yy + 1} textAnchor="middle">{r.op}</text>
                {r.glyph === 'probe' ? (
                  /* glyph flow: magnifier → evidence chip · no function name, no ev-id */
                  <g className="fx-gf" transform={`translate(${hero.x + 100} ${yy - 9})`}>
                    <circle className="fx-gf-lens" cx={8} cy={7} r={5.4} />
                    <line className="fx-gf-lens" x1={12.2} y1={11.2} x2={17.5} y2={16.5} />
                    <line className="fx-gf-arrow" x1={28} y1={9} x2={62} y2={9} markerEnd="url(#fx-ar-d)" />
                    <rect className="fx-gf-chip" x={70} y={0} width={46} height={18} />
                    {[4, 9, 13, 18, 24, 29, 35].map((bx, k) => <rect key={k} className="fx-gf-bar" x={70 + bx} y={4} width={k % 2 ? 1 : 2} height={10} />)}
                    <text className="fx-gf-ok" x={126} y={13.5}>✓</text>
                  </g>
                ) : (
                  <text className="fx-hero-txt" x={hero.x + 96} y={yy + 1}>{clip(r.body, 54)}</text>
                )}
              </g>
            )
          })}
        </g>
      </svg>
    </div>
  )
}
