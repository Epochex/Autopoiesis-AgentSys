import './retrieval.css'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import type { Case, RetrievalResp } from './retrieval-data'
import { retrievalFixture, stepsOf } from './retrieval-data'
import { CaseRecord } from './retrieval-record'

/* ── PAGE 4 · 混合检索 / RETRIEVAL — interactive case record ────────────────────
 * GET /api/rca/retrieval → real worked cases. The page REPLAYS one case: steps
 * reveal as it plays (play/pause/step/scrub), each doc expands in place to its
 * real text + journey, hovering lights a doc everywhere it appears, and the real
 * assembled context builds up. Different scenarios really run different flows
 * (memory-recall vs KB hybrid); the record adapts. Falls back to a flagged
 * sample if the gateway is down. */

type St = { s: 'load' } | { s: 'err'; m: string } | { s: 'ok'; d: RetrievalResp; fixture: boolean }
const prefersReduced = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const STEP_MS = 1500

const T = (zh: boolean) => ({
  kicker: zh ? '混合检索 · 真实案例实录' : 'RETRIEVAL · REAL CASE RECORD',
  loading: zh ? '拉取检索实录…' : 'FETCHING RECORD…',
  offline: zh ? '检索端点不可达' : 'ENDPOINT UNREACHABLE',
  sample: zh ? '样例数据 · 网关离线' : 'SAMPLE DATA · GATEWAY OFFLINE',
  refetch: zh ? '重取' : 'REFETCH', play: zh ? '播放' : 'PLAY', pause: zh ? '暂停' : 'PAUSE', replay: zh ? '重放' : 'REPLAY',
  hint: zh ? '点任一文档展开真实全文与它的全流程轨迹 · 拖动/点阶段可跳步' : 'Click any doc for its full text + journey · click a stage to jump',
})

export function RetrievalPage({ lang, scenario = 'live' }: { lang: Lang; scenario?: 'live' | 'bench' }) {
  const zh = lang === 'zh'
  const tx = T(zh)
  const [st, setSt] = useState<St>({ s: 'load' })
  const [ci, setCi] = useState(0)
  const [step, setStep] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [sel, setSel] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  const d = st.s === 'ok' ? st.d : null
  const cases = d?.cases ?? []
  const c: Case | null = cases[Math.min(ci, Math.max(0, cases.length - 1))] ?? null
  const steps = c ? stepsOf(c) : []
  const last = steps.length - 1

  const stop = useCallback(() => { if (timer.current) { window.clearInterval(timer.current); timer.current = null } setPlaying(false) }, [])
  const startPlay = useCallback((from: number, total: number) => {
    if (timer.current) window.clearInterval(timer.current)
    if (prefersReduced()) { setStep(total - 1); setPlaying(false); return }
    setStep(from); setPlaying(true)
    timer.current = window.setInterval(() => {
      setStep((s) => { if (s >= total - 1) { if (timer.current) window.clearInterval(timer.current); timer.current = null; setPlaying(false); return s } return s + 1 })
    }, STEP_MS)
  }, [])

  const load = useCallback(async () => {
    setSt({ s: 'load' }); setSel(null)
    try {
      const r = await fetch(`/api/rca/retrieval?lang=${lang}&scenario=${scenario}`, { headers: { Accept: 'application/json' } })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const j = (await r.json()) as RetrievalResp
      if (!j || !j.ok || !Array.isArray(j.cases) || !j.cases.length) throw new Error('NO DATA')
      setSt({ s: 'ok', d: j, fixture: false })
    } catch { setSt({ s: 'ok', d: retrievalFixture(lang === 'zh'), fixture: true }) }
  }, [lang, scenario])

  useEffect(() => { void load() }, [load])
  // (re)play from the top whenever payload lands or the case changes
  useEffect(() => {
    if (st.s === 'ok' && cases.length) { setSel(null); startPlay(0, stepsOf(cases[Math.min(ci, cases.length - 1)]).length) }
    return stop
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [st, ci])

  const pickCase = (i: number) => { stop(); setCi(i) }
  const jump = (i: number) => { stop(); setStep(i) }
  const togglePlay = () => { if (playing) stop(); else startPlay(step >= last ? 0 : step, last + 1) }

  return (
    <div className="rt">
      <header className="rt-head">
        <div className="rt-head-l">
          <div className="rt-head-code">
            {st.s === 'ok' && st.fixture ? <span className="rt-sample">{tx.sample}</span> : null}
            <span>{tx.kicker}</span>
          </div>
          <h1 className="rt-head-title">{zh ? <>混合<mark>检索</mark></> : <>HYBRID·<mark>RETRIEVAL</mark></>}</h1>
        </div>
        <button className="rt-run" onClick={() => void load()} disabled={st.s === 'load'}><span className="rt-run-dot" />{tx.refetch}</button>
      </header>

      {st.s === 'load' ? <div className="rt-state">{tx.loading}</div>
        : st.s === 'err' ? <div className="rt-state">{tx.offline} · {st.m}</div>
        : c ? (
          <>
            {/* case selector — real worked examples; badge shows real trigger count */}
            <div className="rt-cases" role="tablist">
              {cases.map((cc, i) => (
                <button key={cc.id} role="tab" aria-selected={i === ci} className={`rt-case${i === ci ? ' on' : ''}`} onClick={() => pickCase(i)}>
                  <span className="rt-case-flow">{cc.flow === 'kb_hybrid' ? (zh ? '知识混合 · KB' : 'KB HYBRID') : (zh ? '记忆召回' : 'MEMORY RECALL')}</span>
                  <span className="rt-case-l">{zh ? cc.label.zh : cc.label.en}</span>
                  <span className={`rt-case-badge${cc.triggers.live ? ' live' : ''}`}>{cc.triggers.count}× {cc.triggers.live ? (zh ? '实跑' : 'LIVE') : 'EVAL'}</span>
                </button>
              ))}
            </div>

            {/* transport: play/pause + clickable stage playhead */}
            <div className="rt-transport">
              <button className="rt-play" onClick={togglePlay}>{playing ? '❚❚ ' + tx.pause : (step >= last ? '↺ ' + tx.replay : '▶ ' + tx.play)}</button>
              <div className="rt-timeline">
                {steps.map((s, i) => (
                  <button key={s.id} className={`rt-tl${step >= i ? ' done' : ''}${step === i ? ' now' : ''}`} onClick={() => jump(i)}>
                    <span className="rt-tl-n">{String(i + 1).padStart(2, '0')}</span>
                    <span className="rt-tl-l">{zh ? s.zh : s.en}</span>
                  </button>
                ))}
              </div>
            </div>

            <div className="rt-body" onMouseLeave={() => setSel(null)}>
              <CaseRecord c={c} step={step} zh={zh} sel={sel} onSel={setSel} />
            </div>
            <div className="rt-hint">{tx.hint}</div>
          </>
        ) : null}
    </div>
  )
}
