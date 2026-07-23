import { useCallback, useMemo, useRef, useState } from 'react'
import type { JSX, PointerEvent as RPointerEvent, KeyboardEvent as RKeyboardEvent } from 'react'
import type { MemEvent, MemOp } from '../types'
import './memory-timeline.css'

/* the ribbon lives in ./CausalRibbon — re-exported here so the container may
 * import both panels from either path */

/* ═══════════════════════════════════════════════════════════════════════════
   MEMORY TIMELINE — scrubbable lifecycle rail over the real event ledger.

   The density problem: 235 of 257 real events are REINFORCE. A linear
   one-tick-per-event rail turns the whole store into an undifferentiated
   smear and buries the 18 ADD / 3 LINK / 1 INSIGHT that actually matter.

   Solution — a WEIGHTED, MONOTONE seq axis + two lanes. Width is allocated by
   significance, not by count:
     · every non-REINFORCE event gets a fat slot (W_RARE units) so it stays
       individually addressable — ~18px @1920, ~13px @1440;
     · the reflection (INSIGHT) gets a wider slot still (W_INSIGHT) so its
       annotation has clear paper to sit on and cannot collide with a neighbour;
     · REINFORCE events get 1 unit each and are drawn aggregated per RUN as a
       density bar (height = reinforce count in that run), never as 235 ticks;
     · the axis stays strictly monotone in seq, so drag/click/step all resolve
       to an exact seq — nothing is faked, nothing is dropped.

   There are no timestamps in the ledger. Order is `seq` only — no clock is
   rendered anywhere. Ops that never fire (UPDATE / NOOP / QUARANTINE) get no
   reserved real estate: the legend is derived from the events actually given.
   ═══════════════════════════════════════════════════════════════════════════ */

const W_RARE = 5 // units per rare op — buys it a clickable slot
const W_INSIGHT = 22 // the reflection also buys paper for its annotation
const W_DENSE = 1 // units per REINFORCE
// DECAY is a passive per-pass retrievability tick: it drives the inspector's real
// strength drop, but it is aggregated like REINFORCE so it never litters the ledger
// with fat clickable slots. FORGET (a record crossing the floor) stays rare.
const weight = (op: MemOp) => (op === 'REINFORCE' || op === 'DECAY' ? W_DENSE : op === 'INSIGHT' ? W_INSIGHT : W_RARE)
/** marks are always drawn W_RARE wide — a wide slot buys space, never a fat dot */
const markUnits = (s: { ev: MemEvent; u0: number; u1: number }) =>
  s.ev.op === 'INSIGHT' ? s.u0 + W_RARE / 2 : (s.u0 + s.u1) / 2

const T: Record<string, [string, string]> = {
  ledger: ['记忆事件账本', 'MEMORY LEDGER'],
  ev: ['事件', 'EV'],
  pass: ['轮次', 'PASS'],
  run: ['运行', 'RUN'],
  seq: ['序号', 'SEQ'],
  op: ['操作', 'OP'],
  mem: ['记忆', 'MEM'],
  tier: ['层', 'TIER'],
  case: ['案例', 'CASE'],
  sim: ['路由相似度', 'ROUTE SIM'],
  reflect: ['反思', 'REFLECT'],
  perRun: ['每运行聚合', 'PER RUN'],
  noClock: ['仅有序号 · 内核未记录时间', 'SEQ ORDER ONLY · NO CLOCK IN LEDGER'],
  reset: ['回到开头', 'RESET'],
  back: ['上一步', 'STEP BACK'],
  play: ['播放', 'PLAY'],
  pause: ['暂停', 'PAUSE'],
  fwd: ['下一步', 'STEP FWD'],
  empty: ['无事件', 'NO EVENTS'],
}
const t = (k: string, zh: boolean) => T[k][zh ? 0 : 1]

const OP_LABEL: Record<MemOp, [string, string]> = {
  ADD: ['新增', 'ADD'],
  UPDATE: ['更新', 'UPDATE'],
  NOOP: ['空操作', 'NOOP'],
  REINFORCE: ['强化', 'REINFORCE'],
  QUARANTINE: ['隔离', 'QUARANTINE'],
  INSIGHT: ['洞见', 'INSIGHT'],
  INSIGHT_REFRESH: ['洞见重算', 'REFLECT+'],
  LINK: ['连接', 'LINK'],
  DECAY: ['衰减', 'DECAY'],
  FORGET: ['遗忘', 'FORGET'],
}
const opLabel = (op: MemOp, zh: boolean) => OP_LABEL[op]?.[zh ? 0 : 1] ?? op
/** css modifier per op — dense ops are never marked individually.
 *  Underscores would not survive as a class modifier, so INSIGHT_REFRESH maps to
 *  `refresh`; it is drawn as a hollow diamond against INSIGHT's solid one, since
 *  it is the same reflection re-deriving itself, not a second kind of thing. */
const opClass = (op: MemOp) => (op === 'REINFORCE' || op === 'DECAY' ? 'dense' : op === 'INSIGHT_REFRESH' ? 'refresh' : op.toLowerCase())
const isRare = (op: MemOp) => op !== 'REINFORCE' && op !== 'DECAY'
const shortCase = (id: string) => id.replace(/^real_/, '')
const pad = (n: number, w: number) => String(n).padStart(w, '0')

type Slot = { ev: MemEvent; u0: number; u1: number }
type Run = { key: string; pass: number; caseId: string; caseNo: number; u0: number; u1: number; dense: number }
type Band = { pass: number; u0: number; u1: number; count: number }

export function MemoryTimeline(props: {
  events: MemEvent[]
  cursorSeq: number
  onCursor: (seq: number) => void
  playing: boolean
  onTogglePlay: () => void
  zh: boolean
}): JSX.Element {
  const { events, cursorSeq, onCursor, playing, onTogglePlay, zh } = props
  const plotRef = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState(false)

  const m = useMemo(() => {
    const slots: Slot[] = []
    const ordered = [...events].sort((a, b) => a.seq - b.seq)
    let u = 0
    for (const ev of ordered) {
      const w = weight(ev.op)
      slots.push({ ev, u0: u, u1: u + w })
      u += w
    }
    const total = u || 1

    // runs are contiguous in seq — aggregate the REINFORCE mass per run
    const runs: Run[] = []
    const caseOrder: string[] = []
    for (const s of slots) {
      if (!caseOrder.includes(s.ev.case_id)) caseOrder.push(s.ev.case_id)
      const last = runs[runs.length - 1]
      if (last && last.key === s.ev.run_id) {
        last.u1 = s.u1
        if (!isRare(s.ev.op)) last.dense += 1
      } else {
        runs.push({
          key: s.ev.run_id, pass: s.ev.pass, caseId: s.ev.case_id,
          caseNo: caseOrder.indexOf(s.ev.case_id) + 1,
          u0: s.u0, u1: s.u1, dense: isRare(s.ev.op) ? 0 : 1,
        })
      }
    }
    const bands: Band[] = []
    for (const s of slots) {
      const last = bands[bands.length - 1]
      if (last && last.pass === s.ev.pass) { last.u1 = s.u1; last.count += 1 }
      else bands.push({ pass: s.ev.pass, u0: s.u0, u1: s.u1, count: 1 })
    }
    const counts = {} as Record<string, number>
    for (const s of slots) counts[s.ev.op] = (counts[s.ev.op] ?? 0) + 1
    const rare = slots.filter(s => isRare(s.ev.op))
    const maxDense = runs.reduce((a, r) => Math.max(a, r.dense), 0) || 1
    const byIdx = new Map(slots.map((s, i) => [s.ev.seq, i]))
    return { slots, ordered, total, runs, bands, counts, rare, maxDense, byIdx, caseOrder }
  }, [events])

  const idx = m.byIdx.get(cursorSeq) ?? (m.slots.length ? 0 : -1)
  const cur = idx >= 0 ? m.slots[idx] : null
  const pct = useCallback((units: number) => `${(units / m.total) * 100}%`, [m.total])
  const headPct = cur ? pct(markUnits(cur)) : '0%'

  /* ── weighted axis → exact seq (monotone, so drag == click == step) ── */
  const seqAtX = useCallback((clientX: number): number | null => {
    const el = plotRef.current
    if (!el || !m.slots.length) return null
    const r = el.getBoundingClientRect()
    if (r.width <= 0) return null
    const f = Math.min(0.9999999, Math.max(0, (clientX - r.left) / r.width))
    const target = f * m.total
    let lo = 0, hi = m.slots.length - 1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (m.slots[mid].u1 <= target) lo = mid + 1
      else hi = mid
    }
    return m.slots[lo].ev.seq
  }, [m])

  const step = useCallback((d: number) => {
    if (idx < 0) return
    const n = Math.min(m.slots.length - 1, Math.max(0, idx + d))
    onCursor(m.slots[n].ev.seq)
  }, [idx, m.slots, onCursor])

  const stepRare = useCallback((d: number) => {
    if (idx < 0) return
    const hit = d > 0
      ? m.slots.find((s, i) => i > idx && isRare(s.ev.op))
      : [...m.slots].reverse().find(s => m.byIdx.get(s.ev.seq)! < idx && isRare(s.ev.op))
    if (hit) onCursor(hit.ev.seq)
  }, [idx, m, onCursor])

  const onDown = (e: RPointerEvent<HTMLDivElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    setDrag(true)
    const s = seqAtX(e.clientX)
    if (s !== null) onCursor(s)
  }
  const onMove = (e: RPointerEvent<HTMLDivElement>) => {
    if (!drag) return
    const s = seqAtX(e.clientX)
    if (s !== null && s !== cursorSeq) onCursor(s)
  }
  const onUp = (e: RPointerEvent<HTMLDivElement>) => {
    setDrag(false)
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId)
  }
  const onKey = (e: RKeyboardEvent<HTMLDivElement>) => {
    const k = e.key
    if (k === 'ArrowLeft' || k === 'ArrowRight') {
      const d = k === 'ArrowLeft' ? -1 : 1
      e.preventDefault()
      if (e.shiftKey) stepRare(d)
      else step(d)
    } else if (k === 'Home') { e.preventDefault(); if (m.slots.length) onCursor(m.slots[0].ev.seq) }
    else if (k === 'End') { e.preventDefault(); if (m.slots.length) onCursor(m.slots[m.slots.length - 1].ev.seq) }
    else if (k === ' ' || k === 'Enter') { e.preventDefault(); onTogglePlay() }
  }

  const lastSeq = m.slots.length ? m.slots[m.slots.length - 1].ev.seq : 0
  const valueText = cur
    ? `${t('seq', zh)} ${cur.ev.seq} · ${opLabel(cur.ev.op, zh)} · ${cur.ev.memory_id}`
    : t('empty', zh)

  return (
    <section className="mt" aria-label={t('ledger', zh)}>
      {/* ── transport + derived op legend (only ops that really fired) ── */}
      <div className="mt-side">
        <div className="mt-ctl">
          <button type="button" title={t('reset', zh)} aria-label={t('reset', zh)}
            disabled={!m.slots.length || idx <= 0}
            onClick={() => m.slots.length && onCursor(m.slots[0].ev.seq)}>|◀</button>
          <button type="button" title={t('back', zh)} aria-label={t('back', zh)}
            disabled={idx <= 0} onClick={() => step(-1)}>◀</button>
          <button type="button" className="mt-play" title={playing ? t('pause', zh) : t('play', zh)}
            aria-label={playing ? t('pause', zh) : t('play', zh)} aria-pressed={playing}
            disabled={!m.slots.length} onClick={onTogglePlay}>{playing ? '❚❚' : '▶'}</button>
          <button type="button" title={t('fwd', zh)} aria-label={t('fwd', zh)}
            disabled={idx < 0 || idx >= m.slots.length - 1} onClick={() => step(1)}>▶</button>
        </div>
        <div className="mt-legend">
          {(['INSIGHT', 'LINK', 'ADD', 'UPDATE', 'NOOP', 'QUARANTINE'] as MemOp[])
            .filter(op => m.counts[op])
            .map(op => (
              <span key={op} className={`mt-lg ${opClass(op)}`}>
                <i /><b>{m.counts[op]}</b>{opLabel(op, zh)}
              </span>
            ))}
          {m.counts.REINFORCE ? (
            <span className="mt-lg dense">
              <i /><b>{m.counts.REINFORCE}</b>{opLabel('REINFORCE', zh)}
              <em>{t('perRun', zh)}</em>
            </span>
          ) : null}
        </div>
      </div>

      {/* ── the rail ── */}
      <div
        ref={plotRef}
        className={`mt-plot${drag ? ' drag' : ''}`}
        role="slider"
        tabIndex={0}
        aria-label={t('ledger', zh)}
        aria-valuemin={m.slots.length ? m.slots[0].ev.seq : 0}
        aria-valuemax={lastSeq}
        aria-valuenow={cur ? cur.ev.seq : 0}
        aria-valuetext={valueText}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onKeyDown={onKey}
      >
        {/* pass bands */}
        <div className="mt-bands">
          {m.bands.map(b => (
            <span key={b.pass} className={`mt-band${cur && cur.ev.pass === b.pass ? ' on' : ''}`}
              style={{ left: pct(b.u0), width: pct(b.u1 - b.u0) }}>
              {t('pass', zh)} {b.pass}<b>{b.count}</b>{t('ev', zh)}
            </span>
          ))}
        </div>

        {/* lane A — rare ops, individually addressable */}
        <div className="mt-lane rare">
          {m.rare.map(s => {
            const on = cur?.ev.seq === s.ev.seq
            return (
              <button
                key={s.ev.seq}
                type="button"
                className={`mt-op ${opClass(s.ev.op)}${on ? ' on' : ''}`}
                style={{ left: pct(markUnits(s)) }}
                title={`${t('seq', zh)} ${s.ev.seq} · ${opLabel(s.ev.op, zh)} · ${s.ev.memory_id}`}
                aria-label={`${t('seq', zh)} ${s.ev.seq} ${opLabel(s.ev.op, zh)} ${s.ev.memory_id}`}
                onPointerDown={e => { e.stopPropagation(); onCursor(s.ev.seq) }}
                onClick={e => e.stopPropagation()}
              ><i /></button>
            )
          })}
          {m.rare.filter(s => s.ev.op === 'INSIGHT').map(s => (
            <span key={`a${s.ev.seq}`} className="mt-anno" style={{ left: pct(s.u0 + W_RARE) }}>
              {t('reflect', zh)}
            </span>
          ))}
        </div>

        {/* lane B — REINFORCE mass, aggregated per run */}
        <div className="mt-lane dense">
          {m.runs.map(r => {
            const on = !!cur && cur.ev.run_id === r.key
            return (
              <span key={r.key} className={`mt-run${on ? ' on' : ''}`}
                style={{ left: pct(r.u0), width: pct(r.u1 - r.u0) }}
                title={`${t('pass', zh)} ${r.pass} · ${shortCase(r.caseId)} · ${r.dense} ${opLabel('REINFORCE', zh)}`}>
                {r.dense > 0 ? <i style={{ height: `${18 + 82 * (r.dense / m.maxDense)}%` }} /> : null}
              </span>
            )
          })}
          {cur && !isRare(cur.ev.op) ? <span className="mt-hot" style={{ left: headPct }} /> : null}
        </div>

        {/* axis — case numerals per run + scrub track */}
        <div className="mt-axis">
          {m.runs.map(r => (
            <span key={r.key} className={`mt-no${cur && cur.ev.run_id === r.key ? ' on' : ''}`}
              style={{ left: pct((r.u0 + r.u1) / 2) }}>{r.caseNo}</span>
          ))}
          <span className="mt-track" />
          <span className="mt-fill" style={{ width: headPct }} />
          <span className="mt-head" style={{ left: headPct }} />
        </div>

        {/* pass rules + the cursor stem across every lane */}
        {m.bands.slice(1).map(b => (
          <span key={`r${b.pass}`} className="mt-rule" style={{ left: pct(b.u0) }} />
        ))}
        {cur ? <span className="mt-stem" style={{ left: headPct }} /> : null}
      </div>

      {/* ── readout: the object under the cursor, never a bare number ── */}
      <div className="mt-read">
        <div className="mt-read-top">
          <span className="mt-seq">{t('seq', zh)} <b>{pad(cur ? cur.ev.seq : 0, String(lastSeq).length)}</b>/{lastSeq}</span>
          {cur ? <span className={`mt-badge ${opClass(cur.ev.op)}`}>{opLabel(cur.ev.op, zh)}</span> : null}
        </div>
        {cur ? (
          <dl className="mt-kv">
            <dt>{t('mem', zh)}</dt><dd className="id">{cur.ev.memory_id}</dd>
            <dt>{t('tier', zh)}</dt><dd>{cur.ev.tier}</dd>
            <dt>{t('case', zh)}</dt><dd>{t('pass', zh)} {cur.ev.pass} · {shortCase(cur.ev.case_id)}</dd>
            {cur.ev.similarity !== null ? (<><dt>{t('sim', zh)}</dt><dd>{cur.ev.similarity.toFixed(2)}</dd></>) : null}
            {cur.ev.op === 'INSIGHT' && cur.ev.source_memory_ids?.length
              ? (<><dt>{t('reflect', zh)}</dt><dd>{cur.ev.source_memory_ids.length} → 1</dd></>) : null}
            {cur.ev.target_id ? (<><dt>{t('run', zh)}</dt><dd className="id">{cur.ev.target_id}</dd></>) : null}
          </dl>
        ) : <p className="mt-kv-empty">{t('empty', zh)}</p>}
        <p className="mt-foot">{t('noClock', zh)}</p>
      </div>
    </section>
  )
}
