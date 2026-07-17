/* ═══════════════════════════════════════════════════════════════════════════
   MEMORY GRAPH — three-tier memory space, real records only.

   Every mark on this canvas is decoded from a field the kernel actually
   serializes. Nothing is synthesized, nothing is decorative.

   ENCODING CONTRACT (also printed in the on-canvas legend)
     column x      root pattern family, read from the real `root:<key>` tag.
                   Column order = first-write order of the records array.
     band y        tier. semantic(top) → procedural(mid) → episodic(bottom).
     node height   importance, sqrt scale, ceiling 52 (real max 50.9). The
                   exact value is ALSO printed in every card, so height is only
                   the at-a-glance ordering — never the sole carrier.
     3 ticks       confidence, hard cap 3.0 → one tick per unit, partial fill
                   for the fraction. Numeral printed beside it.

   TIME BASIS — this is a labelled mixed-time view, and the legend says so.
     from `records` (the store AS IT STANDS): height, importance numeral,
       confidence ticks + numeral. Static under scrub, agrees with the inspector.
     from `events` (TRUE AT THIS CURSOR STEP): whether a record exists at all,
       which links exist, the ×N reinforce tally, the acid touch + op badge, and
       the whole recall rail.
     Why not read importance/confidence off events[].after instead? Because on
     this dataset the store and the event stream disagree on exactly one record:
     insight-fortigate's last snapshot (seq 77) says importance 21.02 while the
     store says 50.9 — the kernel bumps it afterwards without emitting an event.
     Every other record reconciles exactly. Rather than silently pick one and
     contradict the inspector, both time bases are shown and both are labelled.
     --acid        EXACTLY ONE meaning: mutated at the current cursor step.
     ▲ edge        reflection provenance: episodic member abstracts UP into the
                   semantic insight (direction from insight.links / INSIGHT
                   source_memory_ids).
     ─ edge        associative link from record.links. Undirected — the kernel
                   stores these symmetrically, so no arrowhead is drawn.
     → rail        recall: retrieved AND included in the compiled context.
     ⊣ break       recall: retrieved but NOT included. The kernel records no
                   reason for the drop, so none is implied.

   INTERACTION MARKS — about the viewer, never about the data
     ⌐ inner ticks every node is a button; the ticks say so at rest, before the
                   pointer arrives. They encode nothing about the record.
     ⌐ outer marks the inspector is PINNED to this record. NOT the selection
                   halo: the cursor selects a record every step by itself, so the
                   halo means "current", which is not a thing the viewer did.

   DELIBERATELY NOT ENCODED
     `strength` — it is 1.0 on every record (decay is not wired into the loop).
     Encoding a constant as if it varied is the exact failure mode this view
     replaces. The field is ignored, not faked.
   ═══════════════════════════════════════════════════════════════════════════ */
import { useMemo, useCallback } from 'react'
import type { JSX, KeyboardEvent } from 'react'
import type { MemRecord, MemEvent, MemRecall, MemTier, MemOp } from '../types'
import './memory-graph.css'

/* ── canvas geometry (all static; layout depends on `records` only) ────────── */
const VB_W = 1240
const FRAME_X = 20 /* band frame left  */
const FRAME_R = 1010 /* band frame right */
const PLOT_X = 28 /* column 0 origin  */
const PLOT_W = 984
const CORR_X = 1018 /* first routing lane in the corridor */
const CORR_STEP = 9
const RAIL_X = 1108
const RAIL_W = 124
const HEAD_H = 44 /* column-header row */

/* Band label, op badge and link approach lanes all live in this strip. It has to
   clear the label's baseline plus the badge that hangs at node.y - 12, or the
   leftmost node's badge prints straight through the band title.
   Measured, not guessed: at 24 the col-0 badge overlaps the label ink by 2px in
   both languages (episodic + procedural bands); 27 is the exact touch point, so
   30 buys a 3px gap. Every +1 here costs 1px on each band — keep it tight. */
const BAND_HEAD = 30
const ROW_HEAD = 14 /* approach lanes for sub-rows       */
const LANE_STEP = 4
const ROW_LANES = 4 /* 0..2 recall · 3 associative       */
const BAND_PAD = 6
const BAND_GAP = 10
const SPINE_ZONE = 14
const FOOT_H = 56

/* importance → height. Ceiling is a FIXED constant, not data-derived, so the
 * scale is stable if the store grows. Real domain today: 2.5 → 50.9. */
const IMP_CEIL = 52
const H_MIN = 44
const H_SPAN = 44
const hOf = (imp: number) =>
  H_MIN + H_SPAN * Math.sqrt(Math.max(0, Math.min(imp, IMP_CEIL)) / IMP_CEIL)

/* IBM Plex Mono advance is a flat 0.6em, BUT engines snap the advance to whole
 * pixels at small sizes (measured: 7.5px renders at 5.0px/char, not 4.5). Take
 * the upper bound of both cases so a char budget can never overflow its box.
 * LS_* mirror the letter-spacing of the matching CSS rule in memory-graph.css. */
const CH = 0.6
const LS_TXT = 0.005
const LS_ID = 0.02
const LS_COL = 0.06
const LS_OP = 0.08
const advOf = (size: number, lsEm = 0) =>
  Math.max(size * CH, Math.round(size * CH)) + size * lsEm
const fitChars = (px: number, size: number, lsEm = 0) =>
  Math.max(1, Math.floor(px / advOf(size, lsEm)))

const TIER_ORDER: MemTier[] = ['semantic', 'procedural', 'episodic', 'asset_profile']

const BAND_LABEL: Record<MemTier, [string, string]> = {
  semantic: ['语义 SEMANTIC · 抽象认知', 'SEMANTIC · ABSTRACTION'],
  procedural: ['程序 PROCEDURAL · 可复用模式', 'PROCEDURAL · REUSABLE PROBE'],
  episodic: ['情景 EPISODIC · 具体经历', 'EPISODIC · CONCRETE EPISODE'],
  asset_profile: ['资产 ASSET PROFILE · 实体画像', 'ASSET PROFILE · ENTITY'],
}
const ROW_LABEL: Record<string, [string, string]> = {
  family: ['族 FAMILY', 'FAMILY'],
  pattern: ['模式 PATTERN', 'PATTERN'],
}

/* ── text helpers ─────────────────────────────────────────────────────────── */
function midEllipsis(s: string, n: number): string {
  if (s.length <= n) return s
  const head = Math.ceil((n - 1) * 0.6)
  return s.slice(0, head) + '…' + s.slice(s.length - (n - 1 - head))
}
function wrapLines(text: string, maxChars: number, maxLines: number): string[] {
  const parts = text.split(/(?<=[ _\-—:,])/)
  const lines: string[] = []
  let cur = ''
  for (const w of parts) {
    if (cur.length + w.length <= maxChars) {
      cur += w
      continue
    }
    if (cur) lines.push(cur)
    let rest = w
    while (rest.length > maxChars) {
      lines.push(rest.slice(0, maxChars))
      rest = rest.slice(maxChars)
    }
    cur = rest
  }
  if (cur) lines.push(cur)
  if (lines.length <= maxLines) return lines
  const cut = lines.slice(0, maxLines)
  cut[maxLines - 1] = cut[maxLines - 1].slice(0, Math.max(1, maxChars - 1)).trimEnd() + '…'
  return cut
}
const rootOf = (r: MemRecord): string | null => {
  const t = r.tags.find((x) => x.startsWith('root:'))
  return t ? t.slice(5) : null
}
/* A "family" record is a semantic record with no root: tag — i.e. the reflection
 * product, which abstracts across root families rather than sitting inside one. */
const isFamily = (r: MemRecord) => r.tier === 'semantic' && rootOf(r) === null

/* Badge text = the raw op, except the one that cannot fit: INSIGHT_REFRESH is 15
 * chars against a badge sized for REINFORCE, and nodes go as narrow as 60px. */
const opBadge = (op: MemOp) => (op === 'INSIGHT_REFRESH' ? 'REFLECT+' : op)

/* ── layout types ─────────────────────────────────────────────────────────── */
interface RowGeom {
  tier: MemTier
  kind: 'family' | 'pattern'
  nodesTop: number
  maxH: number
  laneBase: number
  approach: number
}
interface NodeGeom {
  rec: MemRecord
  col: number /* -1 = family row, spans the plot */
  row: RowGeom
  x: number
  y: number
  w: number
  h: number
  cx: number
  bottom: number
  lines: string[]
  idText: string
}
interface BandGeom {
  tier: MemTier
  top: number
  bottom: number
  rows: RowGeom[]
}
interface EdgeGeom {
  id: string
  a: string
  b: string
  kind: 'prov' | 'assoc'
  d: string
}

/* ── click affordance · a hit-target frame, drawn at rest ─────────────────────
   The nodes were always buttons; nothing said so until the pointer was already
   on one. These are registration ticks INSIDE the box corners — structural, no
   shadow, no scale, no glow, no second accent. Quiet enough to survive 19 of
   them on one canvas, and they are the same mark the pin brackets complete.  */
const hitTicks = (x: number, y: number, w: number, h: number, inset = 3.5, arm = 5) => {
  const l = x + inset, r = x + w - inset, t = y + inset, b = y + h - inset
  return [
    `M${l} ${t + arm} V${t} H${l + arm}`,
    `M${r - arm} ${t} H${r} V${t + arm}`,
    `M${r} ${b - arm} V${b} H${r - arm}`,
    `M${l + arm} ${b} H${l} V${b - arm}`,
  ].join(' ')
}
/* ── pin mark · the SAME corner language, completed OUTSIDE the box ───────────
   Crop marks around the record the inspector is held on. Distinct from the
   selection halo, which the cursor sets on its own every step and which
   therefore cannot mean "you pinned this". */
const pinBrackets = (x: number, y: number, w: number, h: number, off = 3.5, arm = 9) => {
  const l = x - off, r = x + w + off, t = y - off, b = y + h + off
  return [
    `M${l} ${t + arm} V${t} H${l + arm}`,
    `M${r - arm} ${t} H${r} V${t + arm}`,
    `M${r} ${b - arm} V${b} H${r - arm}`,
    `M${l + arm} ${b} H${l} V${b - arm}`,
  ].join(' ')
}

export function MemoryGraph(props: {
  records: MemRecord[]
  events: MemEvent[]
  touchedIds: Set<string>
  recall: MemRecall | null
  selectedId: string | null
  pinnedId: string | null
  onSelect: (id: string | null) => void
  zh: boolean
}): JSX.Element {
  const { records, events, touchedIds, recall, selectedId, pinnedId, onSelect, zh } = props

  /* ── 1 · geometry. Depends on `records` only, so scrubbing the cursor never
   *      moves a single box. Same input ⇒ same positions, always. ─────────── */
  const geom = useMemo(() => {
    const cols: string[] = []
    for (const r of records) {
      const k = rootOf(r) ?? (isFamily(r) ? null : '__other')
      if (k && !cols.includes(k)) cols.push(k)
    }
    if (!cols.length) cols.push('__other')
    const colW = PLOT_W / cols.length
    const boxW = Math.max(60, Math.min(144, colW - 20))
    const boxDx = (colW - boxW) / 2
    const colX = (i: number) => PLOT_X + i * colW
    const colCx = (i: number) => colX(i) + colW / 2
    /* vertical routing lanes live in the real gaps between boxes, never over one */
    const riserX = (i: number) => colX(i) + boxDx / 2
    const assocX = (i: number, k: number) => colX(i) - boxDx * 0.7 + (k % 3) * 4

    const tiers = TIER_ORDER.filter((t) => records.some((r) => r.tier === t) || t !== 'asset_profile')
    const bands: BandGeom[] = []
    const nodes: NodeGeom[] = []
    const byId = new Map<string, NodeGeom>()

    let y = HEAD_H
    let spineY = 0
    for (const tier of tiers) {
      const inTier = records.filter((r) => r.tier === tier)
      const kinds: ('family' | 'pattern')[] = inTier.some(isFamily)
        ? ['family', 'pattern']
        : ['pattern']
      const band: BandGeom = { tier, top: y, bottom: y, rows: [] }
      let first = true
      for (const kind of kinds) {
        const rs = inTier.filter((r) => (kind === 'family' ? isFamily(r) : !isFamily(r)))
        const nodesTop = first ? band.top + BAND_HEAD : y + ROW_HEAD
        const maxH = rs.length ? Math.max(...rs.map((r) => hOf(r.importance))) : H_MIN
        const row: RowGeom = {
          tier,
          kind,
          nodesTop,
          maxH,
          laneBase: nodesTop + maxH + LANE_STEP,
          approach: nodesTop - LANE_STEP,
        }
        band.rows.push(row)

        for (const rec of rs) {
          const h = hOf(rec.importance)
          let x: number
          let col: number
          if (kind === 'family') {
            /* centred over the real span of the columns it links to — the only
             * honest x for a record that belongs to no single root family. */
            const memberCols = rec.links
              .map((id) => records.find((o) => o.memory_id === id))
              .map((o) => (o ? cols.indexOf(rootOf(o) ?? '') : -1))
              .filter((i) => i >= 0)
            const cx = memberCols.length
              ? memberCols.reduce((s, i) => s + colCx(i), 0) / memberCols.length
              : PLOT_X + PLOT_W / 2
            x = cx - boxW / 2
            col = -1
          } else {
            col = cols.indexOf(rootOf(rec) ?? '__other')
            if (col < 0) col = 0
            x = colX(col) + boxDx
          }
          const inner = boxW - 14
          const maxLines = Math.max(1, Math.floor((h - 31) / 10.5))
          const n: NodeGeom = {
            rec,
            col,
            row,
            x,
            y: nodesTop,
            w: boxW,
            h,
            cx: x + boxW / 2,
            bottom: nodesTop + h,
            /* − 34 keeps clear of the right-anchored reinforce tally */
            lines: wrapLines(rec.text, fitChars(inner, 8.5, LS_TXT), maxLines),
            idText: midEllipsis(rec.memory_id, fitChars(inner - 34, 8, LS_ID)),
          }
          nodes.push(n)
          byId.set(rec.memory_id, n)
        }

        y = row.laneBase + ROW_LANES * LANE_STEP
        if (tier === 'semantic' && kind === 'family') {
          spineY = y + SPINE_ZONE / 2
          y += SPINE_ZONE
        }
        first = false
      }
      band.bottom = y + BAND_PAD
      bands.push(band)
      y = band.bottom + BAND_GAP
    }
    const footTop = y
    const vbH = Math.round(footTop + FOOT_H)
    const railBottom = bands[bands.length - 1].bottom

    return { cols, colW, colX, riserX, assocX, bands, nodes, byId, spineY, footTop, vbH, railBottom }
  }, [records])

  /* ── 2 · state at the cursor, derived from the supplied event window ─────── */
  const state = useMemo(() => {
    const live = new Set<string>()
    const links = new Map<string, Set<string>>()
    const reinforce = new Map<string, number>()
    const lastOp = new Map<string, MemOp>()
    const push = (a: string, b: string) => {
      if (!links.has(a)) links.set(a, new Set())
      links.get(a)!.add(b)
    }
    if (!events.length) {
      /* No event window supplied ⇒ no timeline filter. Show the store as-is
       * rather than a blank canvas; nothing here is invented either way. */
      for (const r of records) {
        live.add(r.memory_id)
        r.links.forEach((l) => push(r.memory_id, l))
      }
      return { live, links, reinforce, lastOp, filtered: false }
    }
    for (const e of events) {
      if (e.op === 'ADD' || e.op === 'INSIGHT') live.add(e.memory_id)
      if (e.op === 'REINFORCE') reinforce.set(e.memory_id, (reinforce.get(e.memory_id) ?? 0) + 1)
      /* LINK carries no snapshot — the edge lives in memory_id ⇄ target_id. */
      if (e.op === 'LINK' && e.target_id) {
        push(e.memory_id, e.target_id)
        push(e.target_id, e.memory_id)
      }
      if (e.after) {
        links.set(e.memory_id, new Set(e.after.links))
        for (const l of e.after.links) push(e.memory_id, l)
      }
      lastOp.set(e.memory_id, e.op)
    }
    return { live, links, reinforce, lastOp, filtered: true }
  }, [events, records])

  /* ── 3 · edges. Real links only, reciprocals deduped, both ends must exist
   *      at the cursor. Orthogonal routing through the real column gaps. ──── */
  const edges = useMemo(() => {
    const { byId, riserX, assocX, spineY } = geom
    const out: EdgeGeom[] = []
    const seen = new Set<string>()
    const pairs: [NodeGeom, NodeGeom][] = []
    for (const [id, set] of state.links) {
      const a = byId.get(id)
      if (!a || !state.live.has(id)) continue
      for (const oid of set) {
        const b = byId.get(oid)
        if (!b || !state.live.has(oid) || oid === id) continue
        const key = id < oid ? `${id}|${oid}` : `${oid}|${id}`
        if (seen.has(key)) continue
        seen.add(key)
        pairs.push(id < oid ? [a, b] : [b, a])
      }
    }
    pairs.sort((p, q) => (p[0].rec.memory_id + p[1].rec.memory_id).localeCompare(q[0].rec.memory_id + q[1].rec.memory_id))

    /* attachment slots on each box edge, distributed deterministically */
    const topAt = new Map<string, string[]>()
    const botAt = new Map<string, string[]>()
    const key = (p: [NodeGeom, NodeGeom]) => p[0].rec.memory_id + '|' + p[1].rec.memory_id
    const rowRank = (n: NodeGeom) => {
      let i = 0
      for (const b of geom.bands) {
        for (const r of b.rows) {
          if (r === n.row) return i
          i++
        }
      }
      return i
    }
    const add = (m: Map<string, string[]>, id: string, k: string) => {
      const list = m.get(id)
      if (list) list.push(k)
      else m.set(id, [k])
    }
    for (const p of pairs) {
      const k = key(p)
      if (isFamily(p[0].rec) || isFamily(p[1].rec)) {
        const [fam, mem] = isFamily(p[0].rec) ? [p[0], p[1]] : [p[1], p[0]]
        add(topAt, mem.rec.memory_id, k)
        add(botAt, fam.rec.memory_id, k)
      } else {
        const up = rowRank(p[0]) <= rowRank(p[1]) ? p[0] : p[1]
        const dn = up === p[0] ? p[1] : p[0]
        add(botAt, up.rec.memory_id, k)
        add(rowRank(up) === rowRank(dn) ? botAt : topAt, dn.rec.memory_id, k)
      }
    }
    const slot = (n: NodeGeom, m: Map<string, string[]>, k: string) => {
      const list = m.get(n.rec.memory_id) ?? []
      const i = Math.max(0, list.indexOf(k))
      /* keep clear of the right edge — recall connectors own x + w − 12 */
      return n.x + 8 + ((n.w - 32) * (i + 1)) / (list.length + 1)
    }

    let assocSeq = 0
    for (const p of pairs) {
      const k = key(p)
      const prov = isFamily(p[0].rec) || isFamily(p[1].rec)
      if (prov) {
        const [fam, mem] = isFamily(p[0].rec) ? [p[0], p[1]] : [p[1], p[0]]
        const ax = slot(mem, topAt, k)
        const ex = slot(fam, botAt, k)
        const vx = mem.col >= 0 ? riserX(mem.col) : fam.cx
        const ay = mem.y - 6
        out.push({
          id: k,
          a: mem.rec.memory_id,
          b: fam.rec.memory_id,
          kind: 'prov',
          d: `M${ax} ${mem.y} L${ax} ${ay} L${vx} ${ay} L${vx} ${spineY} L${ex} ${spineY} L${ex} ${fam.bottom}`,
        })
      } else {
        const up = rowRank(p[0]) <= rowRank(p[1]) ? p[0] : p[1]
        const dn = up === p[0] ? p[1] : p[0]
        const ax = slot(up, botAt, k)
        const ly = up.row.laneBase + (ROW_LANES - 1) * LANE_STEP
        if (rowRank(up) === rowRank(dn)) {
          const bx = slot(dn, botAt, k)
          out.push({
            id: k,
            a: up.rec.memory_id,
            b: dn.rec.memory_id,
            kind: 'assoc',
            d: `M${ax} ${up.bottom} L${ax} ${ly} L${bx} ${ly} L${bx} ${dn.bottom}`,
          })
        } else {
          const bx = slot(dn, topAt, k)
          const vx = assocX(Math.max(0, dn.col), assocSeq++)
          const by = dn.row.approach
          out.push({
            id: k,
            a: up.rec.memory_id,
            b: dn.rec.memory_id,
            kind: 'assoc',
            d: `M${ax} ${up.bottom} L${ax} ${ly} L${vx} ${ly} L${vx} ${by} L${bx} ${by} L${bx} ${dn.y}`,
          })
        }
      }
    }
    return out
  }, [geom, state])

  /* ── 4 · recall. included → rail slot · dropped → break mark. ───────────── */
  const rail = useMemo(() => {
    if (!recall) return null
    const retrieved: string[] = []
    for (const ids of Object.values(recall.retrieved)) for (const id of ids ?? []) if (!retrieved.includes(id)) retrieved.push(id)
    const included = recall.included_memory_ids.filter((id) => geom.byId.has(id))
    const dropped = recall.dropped_memory_ids.filter((id) => geom.byId.has(id))
    const top = HEAD_H + 30
    const avail = geom.railBottom - 8 - top
    const slotH = included.length ? Math.min(44, avail / included.length) : 44
    const slotY = (i: number) => top + slotH * (i + 0.5)

    type Conn = { id: string; d: string; slot: number; kind: 'in' | 'out'; bx: number; by: number }
    const conns: Conn[] = []
    /* one horizontal lane per row, assigned right-column-first so a lane run
     * never crosses a box and never crosses another run in the same row */
    const perRow = new Map<RowGeom, string[]>()
    for (const id of [...included, ...dropped]) {
      const n = geom.byId.get(id)!
      const list = perRow.get(n.row) ?? []
      list.push(id)
      perRow.set(n.row, list)
    }
    /* rightmost column takes the topmost lane ⇒ a lane run never crosses a box */
    for (const ids of perRow.values()) ids.sort((a, b) => geom.byId.get(b)!.cx - geom.byId.get(a)!.cx)
    included.forEach((id, i) => {
      const n = geom.byId.get(id)!
      const j = Math.min(ROW_LANES - 2, perRow.get(n.row)!.indexOf(id))
      const ly = n.row.laneBase + j * LANE_STEP
      const ex = n.x + n.w - 12
      const vx = CORR_X + (i + 1) * CORR_STEP
      const sy = slotY(i)
      conns.push({ id, slot: i, kind: 'in', bx: 0, by: 0, d: `M${ex} ${n.bottom} L${ex} ${ly} L${vx} ${ly} L${vx} ${sy} L${RAIL_X} ${sy}` })
    })
    dropped.forEach((id) => {
      const n = geom.byId.get(id)!
      const j = Math.min(ROW_LANES - 2, perRow.get(n.row)!.indexOf(id))
      const ly = n.row.laneBase + j * LANE_STEP
      const ex = n.x + n.w - 12
      conns.push({ id, slot: -1, kind: 'out', bx: CORR_X, by: ly, d: `M${ex} ${n.bottom} L${ex} ${ly} L${CORR_X} ${ly}` })
    })
    return { retrieved, included, dropped, conns, slotY, slotH, top }
  }, [recall, geom])

  /* ── 5 · pin halo: the PINNED node and whatever it really links to ─────────
   *
   * Keyed to the pin, not to `selectedId`. `selectedId` falls back to the record
   * at the cursor, which the replay moves every 60ms — so keying the halo off it
   * meant the canvas was permanently in "something is selected" mode, dimming 17
   * of 19 records at 6Hz for a choice the viewer never made. That (a) flickers,
   * (b) hides the resting click affordance under opacity 0.16, and (c) burns the
   * one canvas-wide signal that could have meant "you pinned this" on a state
   * that arrives by itself. The cursor already has its own marker: the acid fill.
   */
  const hot = useMemo(() => {
    if (!pinnedId) return null
    const s = new Set<string>([pinnedId])
    for (const id of state.links.get(pinnedId) ?? []) s.add(id)
    return s
  }, [pinnedId, state])

  /* Report the click; do not interpret it. Toggling here against `selectedId`
   * double-toggled with the owner's own pin toggle: `selectedId` is
   * `pinned ?? recordAtCursor`, so clicking a node the cursor already sat on
   * sent null and read as "unpin" — making the click a silent no-op in exactly
   * the case that matters (jump to the reflection, then pin it). */
  const pick = useCallback((id: string) => onSelect(id), [onSelect])
  const onKey = useCallback(
    (e: KeyboardEvent, id: string) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        pick(id)
      }
    },
    [pick],
  )

  const { cols, colX, colW, bands, nodes, spineY, vbH, railBottom, footTop } = geom
  const colChars = fitChars(colW - 14, 8, LS_COL)

  return (
    <div className="mg-root">
      <svg
        className={`mg-canvas${pinnedId ? ' sel' : ''}`}
        viewBox={`0 0 ${VB_W} ${vbH}`}
        role="group"
        aria-label={zh ? '三层记忆空间' : 'Three-tier memory space'}
      >
        <defs>
          <pattern id="mg-dots" width="12" height="12" patternUnits="userSpaceOnUse">
            <circle cx="1.2" cy="1.2" r="1.2" className="mg-dot" />
          </pattern>
          <pattern id="mg-hatch" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="3" height="9" className="mg-hatchbar" />
          </pattern>
          <marker id="mg-up" viewBox="0 0 8 8" refX="4" refY="7" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0.5 7.5 L4 1 L7.5 7.5" className="mg-arrow" />
          </marker>
        </defs>

        {/* click-away target */}
        <rect x="0" y="0" width={VB_W} height={vbH} className="mg-bg" onClick={() => onSelect(null)} />

        {/* ── column headers: real root-cause families, in first-write order.
             No column grid lines are drawn: the vertical routing lanes carry
             real edges, and a decorative rule beside them would read as one. ── */}
        <g className="mg-heads">
          <text x={FRAME_X} y={10} className="mg-kick">
            {zh ? '列 = 根因族 · 按首次写入顺序' : 'COLUMN = ROOT FAMILY · FIRST-WRITE ORDER'}
          </text>
          {cols.map((k, i) => (
            <text key={k} x={colX(i) + 10} y={24} className="mg-colkey">
              {wrapLines(k, colChars, 2).map((l, j) => (
                <tspan key={j} x={colX(i) + 10} dy={j ? 9 : 0}>
                  {l}
                </tspan>
              ))}
              <title>{k}</title>
            </text>
          ))}
          <line x1={FRAME_X} y1={38} x2={FRAME_R} y2={38} className="mg-headrule" />
        </g>

        {/* ── bands: tiers separated by STRUCTURE, never by a second accent ── */}
        {bands.map((b) => (
          <g key={b.tier} className={`mg-band ${b.tier}`}>
            {b.tier === 'episodic' && (
              <rect x={FRAME_X} y={b.top} width={FRAME_R - FRAME_X} height={b.bottom - b.top} className="mg-band-ground" />
            )}
            <rect x={FRAME_X} y={b.top} width={FRAME_R - FRAME_X} height={b.bottom - b.top} className="mg-band-box" />
            {b.tier === 'semantic' && <line x1={FRAME_X} y1={b.top} x2={FRAME_R} y2={b.top} className="mg-band-heavy" />}
            {b.tier === 'procedural' && (
              <rect x={FRAME_X + 6} y={b.top + 3} width={14} height={9} className="mg-band-hatch" />
            )}
            <text
              x={FRAME_X + (b.tier === 'procedural' ? 26 : 6)}
              y={b.top + 11}
              className="mg-band-lab"
            >
              {BAND_LABEL[b.tier][zh ? 0 : 1]}
            </text>
            {b.rows.length > 1 &&
              b.rows.map((r) => (
                <text key={r.kind} x={FRAME_R - 6} y={r.nodesTop - 5} className="mg-row-lab">
                  {ROW_LABEL[r.kind][zh ? 0 : 1]}
                </text>
              ))}
          </g>
        ))}

        {/* ── provenance spine: 6 episodic members abstract UP into the insight ── */}
        {edges.some((e) => e.kind === 'prov') && (
          <text x={FRAME_R - 6} y={spineY - 5} className="mg-spine-lab">
            {zh ? '反思溯源 ▲ 情景 → 洞察' : 'REFLECTION PROVENANCE ▲ EPISODIC → INSIGHT'}
          </text>
        )}

        {/* ── real edges ─────────────────────────────────────────────────── */}
        <g className="mg-edges">
          {edges.map((e) => (
            <path
              key={e.id}
              d={e.d}
              className={`mg-edge ${e.kind}${hot ? (hot.has(e.a) && hot.has(e.b) ? ' hot' : ' dim') : ''}`}
              markerEnd={e.kind === 'prov' ? 'url(#mg-up)' : undefined}
            />
          ))}
        </g>

        {/* ── recall connectors ──────────────────────────────────────────── */}
        {rail && recall && (
          <g className="mg-recall" key={`rc-${recall.seq}-${recall.pass}`}>
            {rail.conns.map((c) => (
              <g key={c.id} className={`mg-conn ${c.kind}${hot ? (hot.has(c.id) ? ' hot' : ' dim') : ''}`}>
                <path d={c.d} className="mg-conn-l" />
                {c.kind === 'out' && (
                  <>
                    <line x1={c.bx} y1={c.by - 5} x2={c.bx} y2={c.by + 5} className="mg-break" />
                    <line x1={c.bx + 4} y1={c.by - 5} x2={c.bx + 4} y2={c.by + 5} className="mg-break" />
                  </>
                )}
              </g>
            ))}
          </g>
        )}

        {/* ── nodes ──────────────────────────────────────────────────────── */}
        <g className="mg-nodes">
          {nodes.map((n) => {
            const id = n.rec.memory_id
            const alive = state.live.has(id)
            const touched = alive && touchedIds.has(id)
            const op = touched ? state.lastOp.get(id) : undefined
            const rf = state.reinforce.get(id) ?? 0
            const isPin = pinnedId === id
            const cls = [
              'mg-node',
              alive ? 'live' : 'pend',
              touched ? 'touch' : '',
              selectedId === id ? 'on' : '',
              isPin ? 'pin' : '',
              n.rec.quarantined ? 'quar' : '',
              hot ? (hot.has(id) ? 'hot' : 'dim') : '',
            ]
              .filter(Boolean)
              .join(' ')
            return (
              <g
                key={id}
                className={cls}
                tabIndex={0}
                role="button"
                aria-label={`${n.rec.tier} ${id}`}
                /* the click toggles the PIN, so that — not the cursor's own
                   selection — is what pressed state means here */
                aria-pressed={isPin}
                onClick={(ev) => {
                  ev.stopPropagation()
                  pick(id)
                }}
                onKeyDown={(ev) => onKey(ev, id)}
              >
                <title>{`${id}\n${n.rec.text}\nimportance ${n.rec.importance.toFixed(2)} · confidence ${n.rec.confidence.toFixed(2)}`}</title>
                {/* A record that does not exist yet at this cursor claims NO
                    magnitude: its slot is drawn at h(0), never at the height it
                    will eventually reach. The reserved geometry stays put so
                    nothing moves when it is finally written. */}
                <rect x={n.x} y={n.y} width={n.w} height={alive ? n.h : H_MIN} className="mg-node-bg" />
                {/* every node is a real button — including one not yet written,
                    which pins to an honest "NOT YET IN MEMORY". So every node
                    carries the hit-target frame; none of them lies about it. */}
                <path d={hitTicks(n.x, n.y, n.w, alive ? n.h : H_MIN)} className="mg-hit" />
                {isPin && (
                  <path d={pinBrackets(n.x, n.y, n.w, alive ? n.h : H_MIN)} className="mg-pinmark" />
                )}
                {!alive ? (
                  <text x={n.x + 7} y={n.y + 20} className="mg-pend-t">
                    {zh ? '未写入' : 'NOT YET'}
                  </text>
                ) : (
                  <>
                    {op && (
                      <g className="mg-op">
                        <rect x={n.x} y={n.y - 12} width={opBadge(op).length * advOf(7.5, LS_OP) + 10} height={11} className="mg-op-bg" />
                        <text x={n.x + 5} y={n.y - 3.5} className="mg-op-t">
                          {opBadge(op)}
                        </text>
                      </g>
                    )}
                    <text x={n.x + 7} y={n.y + 11} className="mg-id">
                      {n.idText}
                    </text>
                    {rf > 0 && (
                      <text x={n.x + n.w - 7} y={n.y + 11} className="mg-tally">
                        ×{rf}
                      </text>
                    )}
                    <line x1={n.x + 6} y1={n.y + 15} x2={n.x + n.w - 6} y2={n.y + 15} className="mg-node-rule" />
                    <text x={n.x + 7} y={n.y + 25} className="mg-txt">
                      {n.lines.map((l, i) => (
                        <tspan key={i} x={n.x + 7} dy={i ? 10.5 : 0}>
                          {l}
                        </tspan>
                      ))}
                    </text>
                    {/* confidence · 3 discrete ticks, hard cap 3.0, value printed */}
                    <g className="mg-conf">
                      {[0, 1, 2].map((k) => {
                        const f = Math.max(0, Math.min(1, n.rec.confidence - k))
                        return (
                          <g key={k}>
                            <rect x={n.x + 7 + k * 10} y={n.bottom - 13} width={8} height={7} className="mg-cell" />
                            {f > 0 && (
                              <rect x={n.x + 7 + k * 10} y={n.bottom - 13} width={8 * f} height={7} className="mg-cell-f" />
                            )}
                          </g>
                        )
                      })}
                      <text x={n.x + 41} y={n.bottom - 7} className="mg-val">
                        {n.rec.confidence.toFixed(1)}
                      </text>
                      <text x={n.x + n.w - 7} y={n.bottom - 7} className="mg-val imp">
                        {n.rec.importance.toFixed(1)}
                      </text>
                    </g>
                  </>
                )}
              </g>
            )
          })}
        </g>

        {/* ── context rail: the compiled context, in the kernel's own order ── */}
        <g className="mg-rail">
          <rect x={RAIL_X} y={HEAD_H} width={RAIL_W} height={railBottom - HEAD_H} className="mg-rail-box" />
          <rect x={RAIL_X} y={HEAD_H} width={RAIL_W} height={22} className="mg-rail-cap" />
          <text x={RAIL_X + 7} y={HEAD_H + 15} className="mg-rail-t">
            {zh ? '上下文 CONTEXT' : 'CONTEXT'}
          </text>
          {rail && recall ? (
            <>
              <text x={RAIL_X + 7} y={HEAD_H + 34} className="mg-rail-s">
                {`P${recall.pass} · ${rail.included.length}/${rail.retrieved.length} ${zh ? '条' : 'IN'}`}
              </text>
              {rail.included.map((id, i) => {
                const y = rail.slotY(i)
                const n = geom.byId.get(id)!
                return (
                  <g key={id} className={`mg-slot${hot ? (hot.has(id) ? ' hot' : ' dim') : ''}`}>
                    <line x1={RAIL_X} y1={y} x2={RAIL_X + 8} y2={y} className="mg-slot-l" />
                    <text x={RAIL_X + 12} y={y - 2} className="mg-slot-n">
                      {String(i + 1).padStart(2, '0')}
                    </text>
                    <text x={RAIL_X + 12} y={y + 8} className="mg-slot-id">
                      {midEllipsis(n.rec.memory_id, fitChars(RAIL_W - 24, 7.5))}
                      <title>{n.rec.memory_id}</title>
                    </text>
                    {i < rail.included.length - 1 && (
                      <line x1={RAIL_X + 6} y1={y + rail.slotH / 2} x2={RAIL_X + RAIL_W - 6} y2={y + rail.slotH / 2} className="mg-slot-rule" />
                    )}
                  </g>
                )
              })}
              {rail.dropped.length > 0 && (
                <text x={RAIL_X + 7} y={railBottom - 8} className="mg-rail-drop">
                  {zh ? `⊣ ${rail.dropped.length} 条未注入` : `⊣ ${rail.dropped.length} DROPPED`}
                </text>
              )}
            </>
          ) : (
            <text x={RAIL_X + 7} y={HEAD_H + 40} className="mg-rail-s">
              {zh ? '本步无检索' : 'NO RECALL'}
            </text>
          )}
        </g>

        {/* ── legend: the decoder for every mark above ───────────────────── */}
        <g className="mg-foot" transform={`translate(0 ${footTop})`}>
          <line x1={FRAME_X} y1={0} x2={VB_W - 8} y2={0} className="mg-headrule" />

          <rect x={FRAME_X} y={12} width={13} height={9} className="mg-lg-acid" />
          <text x={FRAME_X + 19} y={20} className="mg-lg">
            {zh ? '本步变更 CHANGED THIS STEP' : 'CHANGED THIS STEP'}
          </text>

          <rect x={252} y={9} width={9} height={12} className="mg-lg-box" />
          <rect x={264} y={13} width={9} height={8} className="mg-lg-box" />
          <text x={280} y={20} className="mg-lg">
            {zh ? '高度=重要度·库当前值（√刻度）' : 'HEIGHT = IMPORTANCE · STORE (√)'}
          </text>

          <rect x={640} y={13} width={8} height={7} className="mg-cell" />
          <rect x={640} y={13} width={8} height={7} className="mg-cell-f" />
          <rect x={650} y={13} width={8} height={7} className="mg-cell" />
          <rect x={650} y={13} width={4} height={7} className="mg-cell-f" />
          <rect x={660} y={13} width={8} height={7} className="mg-cell" />
          <text x={675} y={20} className="mg-lg">
            {zh ? '三格=置信度·库当前值（上限 3.0）' : 'TICKS = CONFIDENCE · STORE (CAP 3)'}
          </text>

          {/* the interaction the whole panel to the right depends on */}
          <rect x={862} y={10} width={14} height={12} className="mg-lg-box" />
          <path d={hitTicks(862, 10, 14, 12, 1.5, 3)} className="mg-hit" />
          <text x={882} y={20} className="mg-lg">
            {zh ? '点击卡片 = 锁定' : 'CLICK A CARD = PIN'}
          </text>

          <rect x={FRAME_X} y={34} width={13} height={9} className="mg-lg-pend" />
          <text x={FRAME_X + 19} y={42} className="mg-lg">
            {zh ? '未写入 NOT YET WRITTEN' : 'NOT YET WRITTEN'}
          </text>

          <path d="M252 44 L252 36 L266 36" className="mg-edge prov" markerEnd="url(#mg-up)" />
          <text x={280} y={42} className="mg-lg">
            {zh ? '情景 → 洞察（反思溯源）' : 'EPISODIC → INSIGHT (PROVENANCE)'}
          </text>

          <text x={470} y={42} className="mg-lg">
            {zh ? '×N = 到本步的强化次数' : '×N = REINFORCES BY THIS STEP'}
          </text>

          <path d="M640 40 L668 40" className="mg-edge assoc" />
          <text x={675} y={42} className="mg-lg">
            {zh ? '关联链接（无方向）' : 'ASSOCIATIVE LINK (UNDIRECTED)'}
          </text>

          <path d="M905 40 L933 40" className="mg-conn-l" />
          <text x={940} y={42} className="mg-lg">
            {zh ? '已注入上下文' : 'IN CONTEXT'}
          </text>
          <path d="M1064 40 L1082 40" className="mg-conn-l" />
          <line x1={1084} y1={35} x2={1084} y2={45} className="mg-break" />
          <line x1={1088} y1={35} x2={1088} y2={45} className="mg-break" />
          <text x={1096} y={42} className="mg-lg">
            {zh ? '检索到但未注入' : 'RETRIEVED, DROPPED'}
          </text>
          <text x={VB_W - 8} y={20} className="mg-lg-sig">
            {zh ? `${records.length} 条真实记录 · 无合成值` : `${records.length} REAL RECORDS · NO SYNTHETIC VALUES`}
          </text>
        </g>
      </svg>
    </div>
  )
}
