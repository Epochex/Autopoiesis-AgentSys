import './trajectory-bench.css'
import './trajectory.css'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import { rc } from '../i18n'
import type { Observatory, TheaterEvent } from '../types'
import { MemoryObservatory } from './MemoryObservatory'
import { LiveSituation } from './LiveSituation'

/* ── SCENARIO 2 · 长轨迹-bench — 网络 RCA 流式注故障 · 自愈/自演化基准(可回放)──────
 * GET /api/rca/replay drives the REAL network-RCA orchestrator over a stream of
 * real fault cases (reasoner=rule, offline), scoring each diagnosis vs
 * ground-truth and evolving memory. The trajectory REPLAYS event-by-event
 * (play/pause/step): watch each fault get detected→diagnosed→healed and memory
 * accumulate across recurrences. "注入故障" streams the same events through the
 * real isolated Redpanda topic. Every number is real; nothing fabricated. */

type PerEvent = { i: number; pass: number; case: string; correct: number; passed: number; probes: number; retrieved: number; shortcut: boolean; memory: number }
type CaseRow = { id: string; query: string; root_cause_key: string; assets: string[] }
type Summary = { passes: number; n_cases: number; accuracy_warm: number; accuracy_cold: number; memory_grown: number; probes_warm: number; probes_cold: number; probes_saved_pct: number; insights: number }
type Resp = {
  ok: boolean; live: boolean; topic: string; topic_events: number | null
  dataMode?: string; onlineMemory?: boolean
  streamed?: { ok: boolean; produced: number; degraded: boolean; note?: string } | null
  cases: CaseRow[]; per_event: PerEvent[]; summary: Summary
  observatory?: Observatory | null
}
type St = { s: 'load' } | { s: 'err'; m: string } | { s: 'ok'; d: Resp }
const prefersReduced = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const STEP_MS = 360

const T = (zh: boolean) => ({
  kicker: zh ? '多轮故障回放 · 注入网络故障并检查自动处置结果' : 'MULTI-RUN FAULT REPLAY · INJECT FAULTS AND CHECK AUTOMATED ACTIONS',
  loading: zh ? '正在运行多轮真实故障回放…' : 'RUNNING REAL MULTI-RUN FAULT REPLAY…',
  err: zh ? '回放端点不可达' : 'REPLAY ENDPOINT UNREACHABLE',
  inject: zh ? '注入故障 → Redpanda' : 'INJECT FAULT → REDPANDA', injecting: zh ? '注入中…' : 'INJECTING…',
  play: zh ? '回放' : 'PLAY', pause: zh ? '暂停' : 'PAUSE', replay: zh ? '重放' : 'REPLAY',
  acc: zh ? '诊断准确率' : 'ACCURACY', mem: zh ? '已有记录（回放中）' : 'RECORDS (DURING REPLAY)', ncase: zh ? '故障用例' : 'FAULT CASES', passes: zh ? '重复轮次' : 'PASSES',
  saved: zh ? '少做的检查' : 'CHECKS SAVED', ins: zh ? '归纳出的总结' : 'SUMMARIES',
  topic: zh ? '流 topic' : 'STREAM TOPIC', ev: zh ? '事件' : 'events',
  offline: zh ? '离线规则判断 · 使用真实任务编排与结果核对代码' : 'offline rules · real task orchestration and result checker',
  ev_n: zh ? '事件' : 'event', round: zh ? '轮' : 'pass',
  note0: zh ? '这 6 个案例没有减少检查次数；目前只看到诊断准确率和记录数量的变化，更大的案例集才能判断是否省时。' : 'These 6 cases saved no checks. The run only shows changes in accuracy and record count; a larger set is needed to judge time savings.',
  gridk: zh ? '重复故障的处理结果 · 每列一轮 · ✓=诊断正确 · 数字=当时已有记录数' : 'REPEATED-FAULT RESULTS · one pass per column · ✓=correct · number=records at that point',
  streamed: zh ? '已注入' : 'streamed', degraded: zh ? '(网关无 rpk,轨迹仍真实离线)' : '(no rpk; trajectory still real offline)',
  cite: zh ? '用例与根因均来自真实数据集;评分用现有 verifier;流经真实 Redpanda(隔离 topic,不动生产 facts.raw)。' : 'Real dataset + verifier; streamed through real Redpanda (isolated topic; prod facts.raw untouched).',
  seam: zh ? '↓ 本轮采用了哪些旧记录，又写入了什么新记录' : '↓ WHICH OLD RECORDS THIS RUN USED AND WHAT IT WROTE',
  liveh: zh ? '自动处置回放 · 独立旁路进程接收注入故障并产出诊断，不影响生产数据' : 'AUTOMATED-ACTION REPLAY · isolated side processes consume injected faults and produce diagnoses without touching production data',
})

export function TrajectoryBench({ lang, onTheater }: { lang: Lang; onTheater?: (e: TheaterEvent) => void }) {
  const zh = lang === 'zh'
  const tx = T(zh)
  const [st, setSt] = useState<St>({ s: 'load' })
  const [injecting, setInjecting] = useState(false)
  const [step, setStep] = useState(0)
  const [playing, setPlaying] = useState(false)
  const tmr = useRef<number | null>(null)

  const stop = useCallback(() => { if (tmr.current) { window.clearInterval(tmr.current); tmr.current = null } setPlaying(false) }, [])
  const play = useCallback((total: number, from = 0) => {
    if (tmr.current) window.clearInterval(tmr.current)
    if (prefersReduced()) { setStep(total); setPlaying(false); return }
    setStep(from); setPlaying(true)
    tmr.current = window.setInterval(() => setStep((s) => {
      if (s >= total) { if (tmr.current) window.clearInterval(tmr.current); tmr.current = null; setPlaying(false); return s }
      return s + 1
    }), STEP_MS)
  }, [])

  const load = useCallback(async (inject = false) => {
    if (inject) setInjecting(true); else setSt({ s: 'load' })
    try {
      const r = await fetch(`/api/rca/replay?lang=${lang}&passes=4${inject ? '&inject=1' : ''}`, { headers: { Accept: 'application/json' } })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const d = (await r.json()) as Resp
      if (!d || !d.ok) throw new Error('NO DATA')
      setSt({ s: 'ok', d })
      play(d.per_event.length, 0)
    } catch (e) { setSt({ s: 'err', m: e instanceof Error ? e.message : String(e) }) }
    finally { setInjecting(false) }
  }, [lang, play])

  useEffect(() => { void load() }, [load])
  useEffect(() => stop, [stop])

  if (st.s === 'load') return <div className="tb"><div className="tb-state">{tx.loading}</div></div>
  if (st.s === 'err') return <div className="tb"><div className="tb-state">{tx.err} · {st.m}</div></div>
  const d = st.d
  const sm = d.summary
  const total = d.per_event.length
  const done = step >= total
  const pct = (v: number) => `${Math.round(v * 100)}%`
  const cur = step > 0 ? d.per_event[Math.min(step, total) - 1] : null
  // live memory = memory of the most-recently-revealed event
  const liveMem = cur ? cur.memory : 0
  const revealedCorrect = d.per_event.slice(0, step).filter((e) => e.correct).length
  const revealedAcc = step > 0 ? revealedCorrect / step : sm.accuracy_warm

  const byCase = new Map<string, PerEvent[]>()
  for (const e of d.per_event) { const a = byCase.get(e.case) ?? []; a.push(e); byCase.set(e.case, a) }
  for (const a of byCase.values()) a.sort((x, y) => x.pass - y.pass)

  const togglePlay = () => { if (playing) stop(); else play(total, done ? 0 : step) }

  return (
    <div className="tb">
      <header className="tb-head">
        <div className="tb-code">{tx.kicker}</div>
        <h1 className="tb-title">{zh ? <>多轮<mark>故障回放</mark></> : <>MULTI-RUN <mark>FAULT REPLAY</mark></>}</h1>
      </header>

      <div className="tb-metrics">
        <div className="tb-metric ok"><b>{pct(revealedAcc)}</b><span>{tx.acc}</span></div>
        <div className="tb-metric"><b>{liveMem}<i>/{sm.memory_grown}</i></b><span>{tx.mem}</span></div>
        <div className="tb-metric"><b>{sm.n_cases}</b><span>{tx.ncase}</span></div>
        <div className="tb-metric"><b>{sm.passes}</b><span>{tx.passes}</span></div>
        <div className="tb-metric"><b>{pct(sm.probes_saved_pct / 100)}</b><span>{tx.saved}</span></div>
        <div className="tb-metric"><b>{sm.insights}</b><span>{tx.ins}</span></div>
      </div>

      {/* transport: replay playhead */}
      <div className="tb-transport">
        <button className="tb-play" onClick={togglePlay}>{playing ? '❚❚ ' + tx.pause : (done ? '↺ ' + tx.replay : '▶ ' + tx.play)}</button>
        <div className="tb-progress"><span className="tb-progress-fill" style={{ width: `${(step / Math.max(1, total)) * 100}%` }} /></div>
        <span className="tb-counter">{tx.ev_n} {Math.min(step, total)}/{total}{cur ? ` · ${tx.round} P${cur.pass + 1}` : ''}</span>
      </div>

      {/* Redpanda stream strip + live inject */}
      <div className="tb-stream">
        <span className="tb-stream-tag">{tx.topic}</span>
        <code>{d.topic}</code>
        {d.topic_events != null ? <span className="tb-stream-ev">{d.topic_events} {tx.ev}</span> : null}
        <button className="tb-inject" onClick={() => void load(true)} disabled={injecting}>
          <span className="tb-inject-dot" />{injecting ? tx.injecting : tx.inject}
        </button>
        {d.streamed ? <span className={`tb-streamed${d.streamed.degraded ? ' deg' : ''}`}>{tx.streamed} {d.streamed.produced} {tx.ev}{d.streamed.degraded ? ' ' + tx.degraded : ''}</span> : null}
        <span className="tb-offline">{tx.offline}</span>
      </div>

      {/* Running-pod LIVE self-heal: the isolated replay side-car (correlator →
          alerts-sink → aiops-agent) consuming the injected faults in real time.
          Same LiveSituation component as the live 长轨迹, benchmark data source. */}
      <div className="tb-live">
        <div className="tb-live-h">{tx.liveh}</div>
        <LiveSituation zh={zh} scenario="bench" onTheater={onTheater} />
      </div>

      {/* self-evolution grid: cases × passes, fills as it replays */}
      <div className="tb-gridk">{tx.gridk}</div>
      <div className="tb-grid" style={{ ['--passes' as string]: sm.passes }}>
        <div className="tb-grow tb-ghead">
          <span className="tb-gcase">{zh ? '故障用例 · 期望根因' : 'FAULT CASE · EXPECTED ROOT CAUSE'}</span>
          {Array.from({ length: sm.passes }, (_, p) => <span key={p} className="tb-gp">P{p + 1}</span>)}
        </div>
        {d.cases.map((c) => {
          const evs = byCase.get(c.id) ?? []
          return (
            <div key={c.id} className="tb-grow">
              <span className="tb-gcase"><b>{c.query}</b><em>{c.root_cause_key}</em></span>
              {Array.from({ length: sm.passes }, (_, p) => {
                const e = evs.find((x) => x.pass === p)
                const shown = e && e.i < step
                const now = e && e.i === step - 1
                if (!e) return <span key={p} className="tb-cell" />
                return (
                  <span key={p} className={`tb-cell${shown ? (e.correct ? ' ok' : ' bad') : ' pend'}${e.shortcut && shown ? ' sc' : ''}${now ? ' now' : ''}`}
                    title={`memory ${e.memory} · probes ${e.probes}${e.shortcut ? ' · shortcut' : ''}`}>
                    {shown ? <><i className="tb-mark">{e.correct ? '✓' : '✗'}</i><i className="tb-mem">{e.memory}</i></> : <i className="tb-wait">·</i>}
                  </span>
                )
              })}
            </div>
          )
        })}
      </div>

      <p className="tb-note">{tx.note0}</p>
      <p className="tb-cite">{tx.cite}</p>

      {/* The benchmark observatory reuses the live trajectory's exact memory graph,
          context packet, and write-routing views because both are driven by the same
          compare_cold_vs_warm self-evolution data. */}
      {d.observatory ? (
        /* Wrap in .traj-page so the live trajectory's 148 scoped .tr/.tv styles apply
           verbatim — the reuse is now pixel-identical to the live 长轨迹, not a
           restyled copy. .traj-page's own rule is just `gap:0`, so no layout leaks. */
        <div className="traj-page tb-reuse">
          <div className="tp-seam" role="separator"><span>{tx.seam}</span></div>
          <MemoryObservatory
            obs={d.observatory}
            zh={zh}
            cases={d.cases.map((item) => ({
              id: item.id,
              query: item.query,
              rootCause: rc(item.root_cause_key, lang),
              assets: item.assets,
            }))}
            source={{
              dataMode: d.dataMode,
              onlineMemory: d.onlineMemory,
              caseCount: d.cases.length,
              passes: d.summary.passes,
            }}
          />
        </div>
      ) : null}
    </div>
  )
}
