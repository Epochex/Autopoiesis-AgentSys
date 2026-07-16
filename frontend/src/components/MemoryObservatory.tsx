/* 记忆观测舱 / MEMORY OBSERVATORY — the container.
 *
 * Owns cursor + selection + playback and hands already-derived slices to the
 * three presentational panels. Every value shown downstream is serialized from
 * the real kernel run (core/evolve/observatory.py); nothing is synthesized here.
 */
import { useEffect, useMemo, useState } from 'react'
import type { MemEvent, MemRecall, Observatory } from '../types'
import { MemoryGraph } from './MemoryGraph'
import { MemoryInspector } from './MemoryInspector'
import { RouteRuler } from './RouteRuler'
import { CausalRibbon } from './CausalRibbon'
import { MemoryTimeline } from './MemoryTimeline'
import './memory-observatory.css'

export interface ObsByPass {
  pass: number
  probes: number
  recalled: number
  accuracy: number
  memory_end: number
}

/** One real event per tick: 257 events ≈ 15s, the length of a demo beat. */
const TICK_MS = 60

export function MemoryObservatory({
  obs,
  byPass,
  zh,
}: {
  obs: Observatory
  byPass: ObsByPass[]
  zh: boolean
}) {
  const last = obs.events.length - 1
  const [cursor, setCursor] = useState(0)
  const [playing, setPlaying] = useState(true)
  const [pinned, setPinned] = useState<string | null>(null)

  // Playback runs out at the last real event rather than looping — a demo that
  // never settles reads as a screensaver instead of a result. `atEnd` is derived
  // so the run-out needs no state write from inside the effect.
  const atEnd = cursor >= last
  const running = playing && !atEnd

  useEffect(() => {
    if (!running) return
    const id = window.setTimeout(() => setCursor((c) => Math.min(last, c + 1)), TICK_MS)
    return () => window.clearTimeout(id)
  }, [running, cursor, last])

  const scrub = (seq: number) => {
    setPlaying(false)
    setCursor(Math.max(0, Math.min(last, seq)))
  }

  const toggle = () => {
    if (atEnd) {
      setCursor(0)
      setPlaying(true)
    } else {
      setPlaying((p) => !p)
    }
  }

  const eventsUpTo = useMemo(
    () => obs.events.filter((e) => e.seq <= cursor),
    [obs.events, cursor],
  )

  const atCursor: MemEvent | null = obs.events[cursor] ?? null

  /** Ids mutated at exactly this step — the sole meaning of the acid accent. */
  const touchedIds = useMemo(() => {
    const s = new Set<string>()
    if (atCursor) s.add(atCursor.memory_id)
    return s
  }, [atCursor])

  /** recall rows join to events by run_id (24 runs, all covered). */
  const recallByRun = useMemo(() => {
    const m = new Map<string, MemRecall>()
    for (const r of obs.recall) m.set(r.run_id, r)
    return m
  }, [obs.recall])

  const currentRecall = atCursor ? recallByRun.get(atCursor.run_id) ?? null : null

  /** Until the viewer pins a record, the inspector follows the cursor. */
  const selectedId = pinned ?? atCursor?.memory_id ?? null
  const selected = useMemo(
    () => obs.records.find((r) => r.memory_id === selectedId) ?? null,
    [obs.records, selectedId],
  )
  const selectedEvents = useMemo(
    () => (selectedId ? obs.events.filter((e) => e.memory_id === selectedId) : []),
    [obs.events, selectedId],
  )

  /** The 6 real route() calls — the only events carrying a similarity score. */
  const decisions = useMemo(
    () => obs.events.filter((e) => e.similarity !== null),
    [obs.events],
  )

  return (
    <section className="mo" aria-label={zh ? '记忆观测舱' : 'Memory observatory'}>
      <div className="mo-body">
        <div className="mo-space">
          <MemoryGraph
            records={obs.records}
            events={eventsUpTo}
            touchedIds={touchedIds}
            recall={currentRecall}
            selectedId={selectedId}
            onSelect={(id: string | null) => setPinned((p) => (p === id ? null : id))}
            zh={zh}
          />
        </div>
        <aside className="mo-side">
          <MemoryInspector
            record={selected}
            events={selectedEvents}
            cursorSeq={cursor}
            recall={currentRecall}
            capabilities={obs.capabilities}
            zh={zh}
          />
          <RouteRuler decisions={decisions} zh={zh} />
        </aside>
      </div>
      <MemoryTimeline
        events={obs.events}
        cursorSeq={cursor}
        onCursor={scrub}
        playing={running}
        onTogglePlay={toggle}
        zh={zh}
      />
      <CausalRibbon
        recall={obs.recall}
        byPass={byPass}
        capabilities={obs.capabilities}
        zh={zh}
      />
    </section>
  )
}
