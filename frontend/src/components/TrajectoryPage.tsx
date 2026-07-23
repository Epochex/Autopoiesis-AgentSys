import { useEffect, useState } from 'react'
import type { Baseline, RcaCase, TheaterEvent } from '../types'
import type { Lang } from '../i18n'
import { rc } from '../i18n'
import { LiveSituation } from './LiveSituation'
import { MemoryObservatory } from './MemoryObservatory'
import { TraceReplay } from './TraceReplay'
import type { EvoData } from './EvolutionStream'
import './trajectory.css'

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + '…' : s)

export function TrajectoryPage({
  cases, lang, activeId, onPick, onTheater,
}: {
  cases: RcaCase[]; baselines: Baseline[]; reasoner: string; lang: Lang; activeId: string; onPick: (id: string) => void
  onTheater?: (e: TheaterEvent) => void
}) {
  const zh = lang === 'zh'
  const c = cases.find((x) => x.id === activeId) ?? cases[0]
  const [evo, setEvo] = useState<EvoData | null>(null)
  /* One fetch was one chance: a request landing in the dev gateway's reload
   * window got a proxy error and parked the page on the placeholder forever.
   * Retry with backoff (~12s span) so a restarting backend is survived; give
   * up only after that, and stop retrying once unmounted. */
  useEffect(() => {
    let gone = false
    let timer: number | undefined
    const load = (attempt: number) => {
      fetch('/api/rca/evolution?passes=4')
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`)
          return r.json()
        })
        .then((d) => { if (!gone) setEvo(d) })
        .catch(() => {
          if (gone) return
          if (attempt >= 4) { setEvo(null); return }
          timer = window.setTimeout(() => load(attempt + 1), 800 * 2 ** attempt)
        })
    }
    load(0)
    return () => { gone = true; if (timer !== undefined) window.clearTimeout(timer) }
  }, [])

  if (!c) return null
  const evid = c.diagnosis.evidence

  return (
    <div className="traj-page">
      <div className="tp-grid" />

      {/* 实时态势在最上 —— NetOps 流处理此刻正在诊断什么 */}
      <LiveSituation zh={zh} onTheater={onTheater} />

      {/* the seam: live diagnosis above, the same store's history below */}
      <div className="tp-seam" role="separator">
        <span>{zh ? '↓ 长轨迹 · 同一记忆如何随时间演化' : '↓ LONG TRAJECTORY · HOW THE SAME MEMORY EVOLVED OVER TIME'}</span>
      </div>

      <header className="fx-mast">
        <div className="fx-mast-l">
          <span className="fx-mast-kick">{zh ? '自我进化 AI · 内网排查' : 'SELF-EVOLVING AI · NETWORK TRIAGE'}</span>
          <h1 className="fx-mast-title">{zh ? <>长<mark>轨迹</mark></> : <>LONG <mark>TRAJECTORY</mark></>}</h1>
          <div className="fx-mast-mission">
            <span className="fx-mast-q" title={c.query}>{clip(c.query, 62)}</span>
            <mark className="fx-mast-root">{rc(c.diagnosis.rootCauseKey, lang)}</mark>
            <span className="fx-mast-facts"><b>{c.diagnosis.confidence.toFixed(2)}</b>{zh ? '把握' : 'CONF'} · <b>{evid.length}/{evid.length}</b>{zh ? '已核对' : 'VERIFIED'}</span>
          </div>
        </div>
        <div className="fx-mast-r">
          <span className="fx-mast-real">R230 · {zh ? '内网留出集' : 'HELD-OUT'}</span>
          <div className="fx-mast-cases">
            <span className="fx-mast-cases-lab">{zh ? '事件' : 'CASE'}</span>
            {cases.map((x, i) => (
              <button key={x.id} className={`fx-case ${x.id === c.id ? 'on' : ''} ${x.verifier.passed ? 'pass' : ''}`} onClick={() => onPick(x.id)} title={rc(x.diagnosis.rootCauseKey, lang)}>
                {String(i + 1).padStart(2, '0')}
              </button>
            ))}
          </div>
        </div>
      </header>

      {/* the memory the run learns from, replayed from empty */}
      <section className="fx-first">
        {evo?.ready && evo.observatory
          ? <MemoryObservatory obs={evo.observatory} zh={zh} />
          : <div className="fx-first-wait">{zh ? '正在跑自我进化流…' : 'RUNNING SELF-EVOLUTION STREAM…'}</div>}
      </section>

      {/* the run itself, replayed node by node from the observability ledger */}
      <TraceReplay zh={zh} />
    </div>
  )
}
