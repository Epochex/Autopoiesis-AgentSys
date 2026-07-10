import { useEffect, useState } from 'react'

/* ── ③ EXECUTION-LEDGER SCHEMATIC ─────────────────────────────────────────────
   A bespoke architectural schematic (not a linear box-chain). The 7-station
   read-only reasoning spine, with the real system architecture wired IN:
     · 3-TIER MEMORY store  → side-module feeding the 记忆 station
     · SKILL-ATTENTION ctrl → side-module feeding the 技能 station
     · PROVENANCE LEDGER    → a real ledger row below, with actual evidence IDs,
                              raw log lines and per-citation verify status
     · CITATION VERIFIER    → an explicit gate on the 核验→判决 edge
   Orthogonal schematic routing, data-pulses on live edges, and full provenance
   cross-highlighting (station ⇄ evidence). All values are real trace payload. */

export type FxUnit = 'int' | 'pct' | 'x' | 'conf'
export type FxStation = {
  no: string
  name: string
  role: string
  kind: string
  cat: string
  metric: { value: number; unit: FxUnit; caption: string } | null
  loadBearing?: boolean
}
export type FxEvidence = { id: string; sum: string; raw: string; pinned: boolean; included: boolean; cited: boolean; verified: boolean }
export type FxMemTier = { code: string; label: string; count: number }
export type FxSkills = { exposed: string[] }
export type FxVerify = { passed: boolean; recall: number }

/* ── geometry (fixed viewBox, scales to container) ── */
const VW = 1240, VH = 662
const START = 40, W = 150, H = 112, STEP = 168, GATE_GAP = 40
const SP_Y = 262
const CY = SP_Y + H / 2                 // spine connector line
const RAIL_Y = 40, RAIL_H = 150         // top module band (memory / skills)
const LED_Y = 470, LED_H = 158          // provenance ledger band
const xOf = (i: number) => START + i * STEP + (i === 6 ? GATE_GAP : 0)
const cxOf = (i: number) => xOf(i) + W / 2

const CAT_COLOR: Record<string, string> = {
  alert: '#d6335a', memory: '#4c9d94', skill: '#ff7a6b', probe: '#2b3d38',
  context: '#ffcfa0', verify: '#a8bfa0', verdict: '#0d0d0d',
}

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + '…' : s)
const fmt = (n: number, unit: FxUnit) => {
  switch (unit) {
    case 'x': return n.toFixed(2)
    case 'conf': return n.toFixed(2)
    default: return String(Math.round(n))
  }
}
const unitSuffix = (u: FxUnit) => (u === 'pct' ? '%' : u === 'x' ? '×' : '')

/* count-up that only runs while a station is the live cursor. State is only
   written inside the rAF callback; when idle we render the target directly. */
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

function Metric({ m, active, pending }: { m: FxStation['metric']; active: boolean; pending: boolean }) {
  const shown = useCountUp(m?.value ?? 0, active)
  if (!m) return null
  if (pending) return <text className="fx-stn-num pend" x={0} y={0}>·</text>
  return (
    <>
      <text className="fx-stn-num" x={0} y={0}>{fmt(shown, m.unit)}<tspan className="fx-stn-u">{unitSuffix(m.unit)}</tspan></text>
      <text className="fx-stn-cap" x={0} y={18}>{m.caption}</text>
    </>
  )
}

export function FlowGraph({
  stations, evidence, memory, skills, verify, reached, cursor, zh, onSeek,
}: {
  stations: FxStation[]
  evidence: FxEvidence[]
  memory: FxMemTier[]
  skills: FxSkills
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
  const verIdx = idx('verifier_result'), diagIdx = idx('diagnosis_completed')

  // provenance stations = every station that touches the evidence ledger
  const provStations = [probeIdx, verIdx, diagIdx].filter((i) => i >= 0)

  // ── cross-highlight resolution ──
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
      provStations.forEach((s) => hotStn.add(s))
      hotBus.add('pin'); hotBus.add('verify'); hotBus.add('cite')
      evidence.forEach((e, j) => { if (e.pinned || e.cited) hotEvi.add(j) })
    }
  }
  const stnCls = (i: number) => hovering ? (hotStn.has(i) ? 'hot' : 'dim') : ''
  const eviCls = (j: number) => hovering ? (hotEvi.has(j) ? 'hot' : 'dim') : ''
  const busCls = (b: string) => hovering ? (hotBus.has(b) ? 'hot' : 'dim') : ''

  const eviRows = evidence.slice(0, 3)
  const rowY = (j: number) => LED_Y + 52 + j * 34

  // vertical bus anchors
  const pinX = cxOf(probeIdx >= 0 ? probeIdx : 3)
  const verX = cxOf(verIdx >= 0 ? verIdx : 5)
  const citeX = cxOf(diagIdx >= 0 ? diagIdx : 6)

  const memPanel = { x: 30, y: RAIL_Y, w: 330, h: RAIL_H }
  const skPanel = { x: 392, y: RAIL_Y, w: 340, h: RAIL_H }
  const memDropX = cxOf(memIdx >= 0 ? memIdx : 1)
  const skDropX = cxOf(skIdx >= 0 ? skIdx : 2)

  const memLive = memIdx >= 0 && reached >= memIdx
  const skLive = skIdx >= 0 && reached >= skIdx
  const gateX = (xOf(5) + W + xOf(6)) / 2
  const gateLive = verIdx >= 0 && reached >= verIdx

  return (
    <div className="fx-stage">
      <svg className={`fx-svg ${hovering ? 'hov' : ''}`} viewBox={`0 0 ${VW} ${VH}`} preserveAspectRatio="xMidYMid meet" role="img"
        onMouseLeave={() => { setHoverStn(null); setHoverEvi(null) }}>
        <defs>
          <pattern id="fx-dots" width={16} height={16} patternUnits="userSpaceOnUse">
            <circle cx={1} cy={1} r={1} fill="var(--rule)" />
          </pattern>
          <pattern id="fx-hatch" width={7} height={7} patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <rect width={2.4} height={7} fill="var(--ink)" />
          </pattern>
          <marker id="fx-ar" markerWidth={9} markerHeight={9} refX={6.5} refY={3} orient="auto"><path d="M0 0 L6.5 3 L0 6 Z" fill="var(--ink)" /></marker>
          <marker id="fx-ar-a" markerWidth={9} markerHeight={9} refX={6.5} refY={3} orient="auto"><path d="M0 0 L6.5 3 L0 6 Z" fill="var(--ink)" /></marker>
        </defs>

        {/* dot-grid field + registration marks + frame */}
        <rect x={0} y={0} width={VW} height={VH} fill="url(#fx-dots)" opacity={0.6} />
        <g className="fx-reg">
          <path d="M14 14 h16 M14 14 v16" /><path d={`M${VW - 14} 14 h-16 M${VW - 14} 14 v16`} />
          <path d={`M14 ${VH - 14} h16 M14 ${VH - 14} v-16`} /><path d={`M${VW - 14} ${VH - 14} h-16 M${VW - 14} ${VH - 14} v-16`} />
        </g>
        <text className="fx-anno" x={16} y={VH - 22}>SCHEMA · EXEC-LEDGER · R230 · READ-ONLY DIGRAPH</text>
        <text className="fx-anno r" x={VW - 16} y={VH - 22} textAnchor="end">0x{(reached + 1).toString(16).toUpperCase().padStart(2, '0')} / 0x{stations.length.toString(16).toUpperCase().padStart(2, '0')}</text>

        {/* ── wireframe globe motif + read-only annotation (top-right void) ── */}
        <g className="fx-globe" transform="translate(968 116)">
          <circle r={58} />
          <ellipse rx={58} ry={22} /><ellipse rx={38} ry={58} /><ellipse rx={20} ry={58} />
          <line x1={-58} y1={0} x2={58} y2={0} /><line x1={0} y1={-58} x2={0} y2={58} />
          <circle className="fx-globe-o" r={58} />
        </g>
        <text className="fx-anno" x={900} y={44}>⊘ NO-WRITE PATH</text>
        <text className="fx-anno" x={900} y={58}>OBSERVE-ONLY · Σ READ</text>
        <text className="fx-anno dim2" x={1046} y={190} textAnchor="middle">∮ probe ⊂ readonly</text>

        {/* ══ SPINE CONNECTORS (orthogonal) + data-pulses ══ */}
        {stations.slice(0, -1).map((_, i) => {
          const x1 = xOf(i) + W, x2 = xOf(i + 1)
          const on = reached >= i + 1
          const d = `M${x1} ${CY} L${x2} ${CY}`
          return (
            <g key={`sp${i}`} className={`fx-edge ${on ? 'on' : ''}`}>
              <path className="fx-edge-l" d={d} markerEnd="url(#fx-ar)" />
              {on ? <circle className="fx-pulse" r={4}><animateMotion dur="1.5s" repeatCount="indefinite" path={d} /></circle> : null}
            </g>
          )
        })}

        {/* ══ VERIFIER GATE on the 核验 → 判决 edge ══ */}
        <g className={`fx-gate ${gateLive ? 'on' : ''} ${verify.passed ? 'pass' : 'reject'}`} transform={`translate(${gateX} ${CY})`}>
          <path className="fx-gate-body" d="M-15 -18 L15 -18 L15 18 L-15 18 Z" />
          <path className="fx-gate-slot" d="M-8 -8 L8 8 M-8 8 L8 -8" />
          <text className="fx-gate-t" x={0} y={-26} textAnchor="middle">{zh ? '核验闸' : 'GATE'}</text>
          <text className="fx-gate-v" x={0} y={34} textAnchor="middle">{verify.passed ? '✓ PASS' : '✕ REJECT'}</text>
        </g>

        {/* ══ MEMORY 3-TIER STORE · side-module feeding 记忆 ══ */}
        <g className={`fx-mod ${memLive ? 'on' : ''} ${stnCls(memIdx)}`}
          onMouseEnter={() => setHoverStn(memIdx)} onMouseLeave={() => setHoverStn(null)}>
          <path className="fx-mod-box" d={`M${memPanel.x} ${memPanel.y} h${memPanel.w} v${memPanel.h} h-${memPanel.w} Z`} />
          <rect className="fx-mod-strip" x={memPanel.x} y={memPanel.y} width={memPanel.w} height={4} fill={CAT_COLOR.memory} />
          <text className="fx-mod-tag" x={memPanel.x + 12} y={memPanel.y + 24}>{zh ? '三层记忆存储' : '3-TIER MEMORY STORE'}</text>
          <text className="fx-mod-sub" x={memPanel.x + memPanel.w - 12} y={memPanel.y + 24} textAnchor="end">MEM-CORE</text>
          {memory.length ? memory.slice(0, 4).map((m, k) => {
            const yy = memPanel.y + 44 + k * 24
            const max = Math.max(1, ...memory.map((x) => x.count))
            const bw = (m.count / max) * 150
            return (
              <g key={m.code}>
                <text className="fx-mod-k" x={memPanel.x + 14} y={yy + 9}>{m.code}</text>
                <rect className="fx-mod-track" x={memPanel.x + 74} y={yy} width={154} height={11} />
                <rect className="fx-mod-bar" x={memPanel.x + 74} y={yy} width={Math.max(3, bw)} height={11} style={{ opacity: memLive ? 1 : 0.3 }} />
                <text className="fx-mod-n" x={memPanel.x + memPanel.w - 12} y={yy + 9} textAnchor="end">{m.count}</text>
              </g>
            )
          }) : <text className="fx-mod-k" x={memPanel.x + 14} y={memPanel.y + 60}>{zh ? '无先验命中' : 'NO PRIORS'}</text>}
        </g>
        {/* memory drop-in bus */}
        <g className={`fx-bus ${memLive ? 'on' : ''} ${busCls('mem')}`}>
          <path className="fx-bus-l" d={`M${memDropX} ${memPanel.y + memPanel.h} L${memDropX} ${SP_Y}`} markerEnd="url(#fx-ar)" />
          <text className="fx-bus-t" x={memDropX + 8} y={(memPanel.y + memPanel.h + SP_Y) / 2} >{zh ? '先验' : 'PRIOR'}</text>
          {memLive ? <circle className="fx-pulse sm" r={3}><animateMotion dur="1.7s" repeatCount="indefinite" path={`M${memDropX} ${memPanel.y + memPanel.h} L${memDropX} ${SP_Y}`} /></circle> : null}
        </g>

        {/* ══ SKILL-ATTENTION CONTROLLER · side-module feeding 技能 ══ */}
        <g className={`fx-mod ${skLive ? 'on' : ''} ${stnCls(skIdx)}`}
          onMouseEnter={() => setHoverStn(skIdx)} onMouseLeave={() => setHoverStn(null)}>
          <path className="fx-mod-box" d={`M${skPanel.x} ${skPanel.y} h${skPanel.w} v${skPanel.h} h-${skPanel.w} Z`} />
          <rect className="fx-mod-strip" x={skPanel.x} y={skPanel.y} width={skPanel.w} height={4} fill={CAT_COLOR.skill} />
          <text className="fx-mod-tag" x={skPanel.x + 12} y={skPanel.y + 24}>{zh ? '技能注意力控制器' : 'SKILL-ATTENTION CTRL'}</text>
          <text className="fx-mod-sub" x={skPanel.x + skPanel.w - 12} y={skPanel.y + 24} textAnchor="end">TOP-K</text>
          {/* funnel: full toolset → scored top-k → exposed read-only */}
          <path className="fx-hatchbox" d={`M${skPanel.x + 14} ${skPanel.y + 42} h56 v78 h-56 Z`} />
          <text className="fx-mod-mini" x={skPanel.x + 42} y={skPanel.y + 134} textAnchor="middle">{zh ? '全集' : 'TOOLSET'}</text>
          <path className="fx-funnel" d={`M${skPanel.x + 78} ${skPanel.y + 44} L${skPanel.x + 150} ${skPanel.y + 64} L${skPanel.x + 150} ${skPanel.y + 98} L${skPanel.x + 78} ${skPanel.y + 118} Z`} />
          <text className="fx-mod-mini" x={skPanel.x + 114} y={skPanel.y + 78} textAnchor="middle">SCORE</text>
          <text className="fx-mod-mini" x={skPanel.x + 114} y={skPanel.y + 92} textAnchor="middle">&gt;0.5</text>
          {skills.exposed.slice(0, 3).map((s, i) => {
            const yy = skPanel.y + 46 + i * 24
            const code = (s.replace(/^check[_-]?/i, '').replace(/_/g, ' ').slice(0, 15) || 'skill').toUpperCase()
            return (
              <g key={s}>
                <rect className="fx-sk-chip" x={skPanel.x + 168} y={yy} width={150} height={18} style={{ opacity: skLive ? 1 : 0.3 }} />
                <text className="fx-sk-t" x={skPanel.x + 176} y={yy + 13}>{code}<title>{s}</title></text>
              </g>
            )
          })}
          <text className="fx-mod-lock" x={skPanel.x + skPanel.w - 12} y={skPanel.y + skPanel.h - 10} textAnchor="end">⊘ WRITE-BLOCKED · RO</text>
        </g>
        {/* skill drop-in bus */}
        <g className={`fx-bus ${skLive ? 'on' : ''} ${busCls('skill')}`}>
          <path className="fx-bus-l" d={`M${skDropX} ${skPanel.y + skPanel.h} L${skDropX} ${SP_Y}`} markerEnd="url(#fx-ar)" />
          <text className="fx-bus-t" x={skDropX + 8} y={(skPanel.y + skPanel.h + SP_Y) / 2}>{zh ? '钳制' : 'CLAMP'}</text>
          {skLive ? <circle className="fx-pulse sm" r={3}><animateMotion dur="1.7s" repeatCount="indefinite" path={`M${skDropX} ${skPanel.y + skPanel.h} L${skDropX} ${SP_Y}`} /></circle> : null}
        </g>

        {/* ══ PROVENANCE LEDGER · real evidence IDs / raw logs / verify status ══ */}
        <g className={`fx-led ${reached >= (probeIdx < 0 ? 3 : probeIdx) ? 'on' : ''}`}>
          <path className="fx-led-box" d={`M30 ${LED_Y} h1180 v${LED_H} h-1180 Z`} />
          <rect className="fx-led-strip" x={30} y={LED_Y} width={1180} height={4} fill={CAT_COLOR.probe} />
          <text className="fx-led-tag" x={46} y={LED_Y + 26}>{zh ? '证据溯源账本' : 'PROVENANCE LEDGER'}</text>
          <text className="fx-led-sub" x={214} y={LED_Y + 26}>{zh ? `钉实 ${evidence.filter((e) => e.pinned).length} · 引用核验 ${evidence.filter((e) => e.cited).length}` : `${evidence.filter((e) => e.pinned).length} PINNED · ${evidence.filter((e) => e.cited).length} CITED+VERIFIED`}</text>
          <text className="fx-led-h" x={64} y={LED_Y + 44}>REF</text>
          <text className="fx-led-h" x={116} y={LED_Y + 44}>EVIDENCE-ID</text>
          <text className="fx-led-h" x={342} y={LED_Y + 44}>STATUS</text>
          <text className="fx-led-h" x={470} y={LED_Y + 44}>RAW OBSERVATION · SOURCE</text>
          {eviRows.map((e, j) => {
            const y = rowY(j)
            return (
              <g key={e.id} className={`fx-row ${eviCls(j)} ${e.cited ? 'cited' : ''}`}
                onMouseEnter={() => setHoverEvi(j)} onMouseLeave={() => setHoverEvi(null)}>
                <rect className="fx-row-hit" x={40} y={y - 15} width={1160} height={30} />
                <rect className="fx-row-tag" x={52} y={y - 9} width={30} height={18} />
                <text className="fx-row-ref" x={67} y={y + 4} textAnchor="middle">E{String(j + 1).padStart(2, '0')}</text>
                <rect className="fx-row-dot" x={110} y={y - 4} width={8} height={8} />
                <text className="fx-row-id" x={126} y={y + 4}>{clip(e.id, 26)}<title>{e.id}</title></text>
                <g className={`fx-row-st ${e.verified ? 'ok' : 'obs'}`}>
                  <path d={`M338 ${y - 9} h94 v18 h-94 Z`} />
                  <text x={385} y={y + 4} textAnchor="middle">{e.verified ? (zh ? '✓ 已核验' : '✓ VERIFIED') : (zh ? '◇ 已观测' : '◇ OBSERVED')}</text>
                </g>
                <text className="fx-row-raw" x={470} y={y + 4}>{clip(e.raw || e.sum, 96)}<title>{e.raw}</title></text>
              </g>
            )
          })}
        </g>

        {/* ledger ⇄ spine buses: PIN (down) / VERIFY (up) / CITE (up) */}
        <g className={`fx-bus ${reached >= (probeIdx < 0 ? 3 : probeIdx) ? 'on' : ''} ${busCls('pin')}`}>
          <path className="fx-bus-l" d={`M${pinX} ${SP_Y + H} L${pinX} ${LED_Y}`} markerEnd="url(#fx-ar)" />
          <text className="fx-bus-t" x={pinX + 8} y={(SP_Y + H + LED_Y) / 2}>{zh ? '钉证据' : 'PIN'}</text>
        </g>
        <g className={`fx-bus ${verIdx >= 0 && reached >= verIdx ? 'on' : ''} ${busCls('verify')}`}>
          <path className="fx-bus-l" d={`M${verX} ${LED_Y} L${verX} ${SP_Y + H}`} markerEnd="url(#fx-ar)" />
          <text className="fx-bus-t" x={verX + 8} y={(SP_Y + H + LED_Y) / 2}>{zh ? '核验' : 'VERIFY'}</text>
        </g>
        <g className={`fx-bus ${diagIdx >= 0 && reached >= diagIdx ? 'on' : ''} ${busCls('cite')}`}>
          <path className="fx-bus-l" d={`M${citeX} ${LED_Y} L${citeX} ${SP_Y + H}`} markerEnd="url(#fx-ar)" />
          <text className="fx-bus-t" x={citeX + 8} y={(SP_Y + H + LED_Y) / 2}>{zh ? '引用' : 'CITE'}</text>
        </g>

        {/* ══ THE 7 STATIONS ══ */}
        {stations.map((s, i) => {
          const x = xOf(i), pending = i > reached, active = i === cursor && !pending
          return (
            <g key={s.no} className={`fx-stn ${pending ? 'pend' : active ? 'active' : 'done'} ${s.loadBearing ? 'load' : ''} ${stnCls(i)}`}
              transform={`translate(${x} ${SP_Y})`}
              onMouseEnter={() => setHoverStn(i)} onMouseLeave={() => setHoverStn(null)}
              onClick={() => onSeek(i)} style={{ cursor: 'pointer' }}>
              <rect className="fx-stn-bg" x={0} y={0} width={W} height={H} />
              <rect className="fx-stn-strip" x={0} y={0} width={W} height={5} fill={CAT_COLOR[s.cat] ?? '#0d0d0d'} />
              <text className="fx-stn-no" x={12} y={26}>{s.no}</text>
              <text className="fx-stn-idx" x={W - 12} y={26} textAnchor="end">◇{i + 1}/{stations.length}</text>
              <text className="fx-stn-name" x={12} y={50}>{s.name}</text>
              <line className="fx-stn-rule" x1={12} y1={60} x2={W - 12} y2={60} />
              <g transform={`translate(14 ${H - 26})`}><Metric m={s.metric} active={active} pending={pending} /></g>
              {s.loadBearing ? <text className="fx-stn-load" x={W - 12} y={H - 10} textAnchor="end">◼ LOAD-BEARING</text> : null}
              {active ? <text className="fx-stn-live" x={W - 12} y={H - 10} textAnchor="end">▸ LIVE</text> : null}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
