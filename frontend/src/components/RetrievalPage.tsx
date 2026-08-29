import './retrieval.css'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import type { Case, RetrievalResp } from './retrieval-data'
import { stepsOf } from './retrieval-data'
import { CaseRecord } from './retrieval-record'

/* ── PAGE 4 · 混合检索 / RETRIEVAL — interactive case record ────────────────────
 * GET /api/rca/retrieval → real worked cases. The page REPLAYS one case: steps
 * reveal as it plays (play/pause/step/scrub), each doc expands in place to its
 * real text + journey, hovering lights a doc everywhere it appears, and the real
 * assembled context builds up. Different scenarios really run different flows
 * (memory-recall vs KB hybrid); the record adapts. Falls back to a flagged
 * sample if the gateway is down. */

type St = { s: 'load' } | { s: 'err'; m: string } | { s: 'ok'; d: RetrievalResp }
const prefersReduced = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const STEP_MS = 1500

const T = (zh: boolean) => ({
  kicker: zh ? '查资料过程 · 真实案例实录' : 'HOW RECORDS WERE FOUND · REAL CASE',
  loading: zh ? '正在读取查找记录…' : 'FETCHING LOOKUP RECORD…',
  offline: zh ? '查找接口不可达' : 'LOOKUP ENDPOINT UNREACHABLE',
  empty: zh ? '还没有真实调查检索回执。请从实时事件打开调查，完成首次检索后再查看。' : 'No live investigation retrieval receipt yet. Open an investigation from a live event first.',
  refetch: zh ? '重取' : 'REFETCH', play: zh ? '播放' : 'PLAY', pause: zh ? '暂停' : 'PAUSE', replay: zh ? '重放' : 'REPLAY',
  hint: zh ? '点击任一文档可查看全文和每一步处理记录，点击阶段名称可直接跳转' : 'Click any document for its full text and processing steps; click a stage to jump',
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
      if (!j || !j.ok || !Array.isArray(j.cases)) throw new Error('INVALID DATA')
      setSt({ s: 'ok', d: j })
    } catch (error) {
      setSt({ s: 'err', m: error instanceof Error ? error.message : 'REQUEST FAILED' })
    }
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
            <span>{tx.kicker}</span>
          </div>
          <h1 className="rt-head-title">{zh ? <>怎么<mark>找到资料</mark></> : <>HOW RECORDS WERE <mark>FOUND</mark></>}</h1>
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
                  <span className="rt-case-flow">{cc.flow === 'kb_hybrid' ? (zh ? '查知识库' : 'SEARCH KB') : (zh ? '查已有记录' : 'SEARCH RECORDS')}</span>
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
        ) : st.s === 'ok' ? <div className="rt-state">{tx.empty}</div> : null}
    </div>
  )
}
