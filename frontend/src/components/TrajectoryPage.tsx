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
  onTrace?: (subject: string, caseId?: string) => void
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
        <span>{zh ? '02 · 固定案例记忆机制调试 · 不作为业务效果' : '02 · FIXED-CASE MEMORY DIAGNOSTIC · EXCLUDED FROM BUSINESS RESULTS'}</span>
      </div>

      <header className="fx-mast">
        <div className="fx-mast-l">
          <span className="fx-mast-kick">{zh ? '开发者调试集 · 检查检索与写入事件' : 'DEVELOPER FIXTURE · INSPECT RETRIEVAL AND WRITE EVENTS'}</span>
          <h1 className="fx-mast-title">{zh ? <>离线<mark>记忆算法回放</mark></> : <>OFFLINE <mark>MEMORY ALGORITHM REPLAY</mark></>}</h1>
          <div className="fx-mast-mission">
            <span className="fx-mast-q" title={c.query}>{clip(c.query, 62)}</span>
            <mark className="fx-mast-root">{rc(c.diagnosis.rootCauseKey, lang)}</mark>
            <span className="fx-mast-facts"><b>{c.diagnosis.confidence.toFixed(2)}</b>{zh ? '把握' : 'CONF'} · <b>{evid.length}/{evid.length}</b>{zh ? '已核对' : 'VERIFIED'}</span>
          </div>
        </div>
        <div className="fx-mast-r">
          <span className="fx-mast-real">R230 · {zh ? '历史固定案例' : 'HISTORICAL FIXED CASES'}</span>
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
          ? <MemoryObservatory
              obs={evo.observatory}
              zh={zh}
              cases={cases.map((item) => ({
                id: item.id,
                query: item.query,
                rootCause: rc(item.diagnosis.rootCauseKey, lang),
                assets: item.assets,
              }))}
              source={{
                dataMode: evo.dataMode,
                onlineMemory: evo.onlineMemory,
                caseCount: evo.benchmark?.caseCount ?? cases.length,
                passes: evo.benchmark?.passes ?? evo.passes,
              }}
            />
          : <div className="fx-first-wait">{zh ? '正在计算固定案例调试回放…' : 'RUNNING FIXED-CASE DIAGNOSTIC REPLAY…'}</div>}
      </section>

      <div className="tp-seam online" role="separator">
        <span>{zh ? '03 · 本机在线记忆演化 · PostgreSQL 持久化事件账本' : '03 · ONLINE MEMORY EVOLUTION · POSTGRESQL EVENT LEDGER'}</span>
      </div>
      <LiveMemory lang={lang} />

      {/* Offline benchmark result returned by /api/rca/evolution. */}
    </div>
  )
}
