/* 记忆观测舱 / MEMORY OBSERVATORY — the container.
 *
 * Owns cursor + selection + playback and hands already-derived slices to the
 * three presentational panels. Every value shown downstream is serialized from
 * the real kernel run (core/evolve/observatory.py); nothing is synthesized here.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
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

/** Read at mount, not subscribed to: playback intent is a decision taken once,
 *  when the viewer arrives. Flipping a running replay because the OS setting
 *  changed mid-demo would be a bigger surprise than not reacting to it. */
const reducedMotion = () =>
  typeof window !== 'undefined' &&
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

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
  const rootRef = useRef<HTMLElement | null>(null)
  // Under reduced motion the replay never runs on its own, so opening on cursor 0
  // would park the viewer on an empty store with no indication it fills in. Open
  // on the settled end state instead and let them scrub back.
  const [reduced] = useState(reducedMotion)
  const [cursor, setCursor] = useState(() => (reducedMotion() ? Math.max(0, last) : 0))
  const [playing, setPlaying] = useState(!reduced)
  /* No IntersectionObserver (jsdom, ancient engines) ⇒ no visibility signal to
   * gate on, so fall back to the old always-on behaviour rather than to a replay
   * that can never start. */
  const [onScreen, setOnScreen] = useState(() => typeof IntersectionObserver !== 'function')
  const [pinned, setPinned] = useState<string | null>(null)

  /* The replay is the argument this screen makes, and it only makes it if the
   * viewer watches memory fill from empty. Mounting is not watching: the
   * observatory leads a scrollable page, and at 60ms/event a mount-time start
   * had burned ~27 real events before the page even settled. Gate on visibility
   * — start on entry, hold on exit, keep the viewer's own play/pause intent. */
  useEffect(() => {
    const el = rootRef.current
    if (!el || typeof IntersectionObserver !== 'function') return
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          /* Ratio alone is not a safe test: the observatory is sized against the
           * viewport, so on a short screen it can be taller than the screen and
           * then "half of it" is never on screen at once. Half a screenful of
           * observatory counts too. */
          const vh = window.innerHeight || 1
          setOnScreen(e.intersectionRatio >= 0.5 || e.intersectionRect.height >= vh * 0.5)
        }
      },
      { threshold: [0, 0.25, 0.5, 0.75, 1] },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [])

  // Playback runs out at the last real event rather than looping — a demo that
  // never settles reads as a screensaver instead of a result. `atEnd` is derived
  // so the run-out needs no state write from inside the effect.
  const atEnd = cursor >= last
  const running = playing && onScreen && !atEnd

  useEffect(() => {
    if (!running) return
    const id = window.setTimeout(() => setCursor((c) => Math.min(last, c + 1)), TICK_MS)
    return () => window.clearTimeout(id)
  }, [running, cursor, last])

  /* Esc releases the pin. The inspector carries a real button too — this is the
   * shortcut for it, not the only way out. */
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') setPinned(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

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

  /** The real route() calls — what the write-router ruler is about.
   *
   *  Filter by OP, not by `similarity !== null`. LINK now carries a genuine
   *  similarity too (link_related computes its own A-MEM score), so keying off
   *  the field alone drags associative links onto a ruler whose axis is the
   *  ADD/UPDATE/NOOP gate they were never measured against. */
  const decisions = useMemo(
    () => obs.events.filter(
      (e) => e.similarity !== null && (e.op === 'ADD' || e.op === 'UPDATE' || e.op === 'NOOP'),
    ),
    [obs.events],
  )

  /* The thesis line. Both counts are the real array lengths — the sentence is
   * built from the data it describes, so it cannot drift away from it. Every op
   * it names actually fires on this run (ADD 18 · INSIGHT 1 · REINFORCE 235 ·
   * INSIGHT_REFRESH 22 · LINK 8); the ones that never fire are not mentioned. */
  const thesis = zh
    ? `回放一次真实内核运行：记忆从空开始，${obs.records.length} 条记录经 ${obs.events.length} 次生命周期事件写入 → 加固 → 抽象为洞察`
    : `ONE REAL KERNEL RUN, REPLAYED FROM EMPTY MEMORY — ${obs.records.length} RECORDS WRITTEN, REINFORCED AND ABSTRACTED ACROSS ${obs.events.length} LIFECYCLE EVENTS`

  return (
    <section className="mo" ref={rootRef} aria-label={zh ? '记忆观测舱' : 'Memory observatory'}>
      <header className="mo-head">
        <span className="mo-head-t">{zh ? '记忆观测舱' : 'MEMORY OBSERVATORY'}</span>
        <p className="mo-head-s">{thesis}</p>
      </header>
      <div className="mo-body">
        <div className="mo-space">
          <MemoryGraph
            records={obs.records}
            events={eventsUpTo}
            touchedIds={touchedIds}
            recall={currentRecall}
            selectedId={selectedId}
            pinnedId={pinned}
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
            pinned={pinned !== null}
            onUnpin={() => setPinned(null)}
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
