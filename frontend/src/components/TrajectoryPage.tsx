import { useEffect, useState } from 'react'
import type { EvoData, RcaCase, TheaterEvent } from '../types'
import type { Lang } from '../i18n'
import { rc } from '../i18n'
import { LiveSituation } from './LiveSituation'
import { LiveMemory } from './LiveMemory'
import { MemoryObservatory } from './MemoryObservatory'
import './trajectory.css'

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + '…' : s)

export function TrajectoryPage({
  cases, lang, activeId, onPick, onTheater, onTrace, focusSubject,
}: {
  cases: RcaCase[]; lang: Lang; activeId: string; onPick: (id: string) => void
  onTheater?: (e: TheaterEvent) => void
  /** Jump to this subject's response chain on the diagnose page. */
  onTrace?: (subject: string) => void
  focusSubject?: string
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

      {/* NetOps disk-sink records appear above the separate offline replay. */}
      <LiveSituation zh={zh} onTheater={onTheater} onTrace={onTrace} focusSubject={focusSubject} />

      {/* Explicit source boundary: disk-sink records above, temp-dir benchmark below. */}
      <div className="tp-seam" role="separator">
        <span>{zh ? '↓ 离线基准回放 · 六案例临时目录计算' : '↓ OFFLINE BENCHMARK REPLAY · SIX-CASE TEMP-DIR RUN'}</span>
      </div>

      <header className="fx-mast">
        <div className="fx-mast-l">
          <span className="fx-mast-kick">{zh ? '重复故障排查 · 复用之前留下的记录' : 'REPEATED-FAULT CHECKS · REUSE EARLIER RECORDS'}</span>
          <h1 className="fx-mast-title">{zh ? <>多轮<mark>故障回放</mark></> : <>MULTI-RUN <mark>FAULT REPLAY</mark></>}</h1>
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
          ? <MemoryObservatory obs={evo.observatory} zh={zh} caseRoots={Object.fromEntries(cases.map((x) => [x.id, x.diagnosis.rootCauseKey]))} />
          : <div className="fx-first-wait">{zh ? '正在计算离线基准回放…' : 'RUNNING OFFLINE BENCHMARK REPLAY…'}</div>}
      </section>

      <LiveMemory lang={lang} />

      {/* Offline benchmark result returned by /api/rca/evolution. */}
    </div>
  )
}
