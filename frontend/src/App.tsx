import { useCallback, useEffect, useState } from 'react'
import './App.css'
import type { RcaCase, RcaSnapshot } from './types'
import { rc, t, type Lang } from './i18n'
import { TopologyCanvas } from './components/TopologyCanvas'
import { ThreatCard, Analyzing, type Threat } from './components/ThreatCard'
import { CountUp, ConfidenceRing } from './components/Motion'
import type { Device } from './types'

type State =
  | { s: 'load' }
  | { s: 'err'; m: string }
  | { s: 'ok'; d: RcaSnapshot }

function App() {
  const [lang, setLang] = useState<Lang>('zh')
  const [provider, setProvider] = useState('rule')
  const [st, setSt] = useState<State>({ s: 'load' })
  const [active, setActive] = useState('')
  const [drillSub, setDrillSub] = useState<string | null>(null)
  const [drillDev, setDrillDev] = useState<string | null>(null)
  const [tempo, setTempo] = useState(1)
  const [rate, setRate] = useState<number | null>(null)
  const [threat, setThreat] = useState<Threat | null>(null)
  const [marks, setMarks] = useState<Record<string, { severity: string; verdict: string }>>({})
  const [posture, setPosture] = useState<{ cidr: string; loading: boolean; high?: number; watch?: number; summary?: string } | null>(null)

  const load = useCallback(async (p: string) => {
    setSt({ s: 'load' })
    try {
      const r = await fetch(`/api/rca/snapshot?provider=${p}`, { headers: { Accept: 'application/json' } })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const d = (await r.json()) as RcaSnapshot
      setSt({ s: 'ok', d })
      setActive((prev) => (d.cases.some((c) => c.id === prev) ? prev : d.cases[0]?.id ?? ''))
    } catch (e) {
      setSt({ s: 'err', m: e instanceof Error ? e.message : String(e) })
    }
  }, [])

  useEffect(() => {
    void load(provider)
  }, [provider, load])

  // poll R230 live event-rate → pulse tempo
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const r = await fetch('/api/rca/pulse')
        const j = await r.json()
        if (alive && j.live && typeof j.eventsPerSec === 'number') {
          setRate(j.eventsPerSec)
          setTempo(Math.max(0.6, Math.min(3, j.eventsPerSec / 12)))
        }
      } catch {
        /* keep last */
      }
    }
    void tick()
    const id = setInterval(tick, 4500)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  const researchDevice = async (dev: Device | null, cidr: string) => {
    setDrillDev(dev?.ip ?? null)
    if (!dev || dev.threat === 'ok') {
      setThreat(null)
      return
    }
    setThreat({ ip: dev.ip, loading: true })
    try {
      const r = await fetch(`/api/rca/threat?ip=${dev.ip}&cidr=${encodeURIComponent(cidr)}&lang=${lang}`)
      const j = await r.json()
      if (j.ok) {
        setThreat({ ip: dev.ip, loading: false, severity: j.severity, verdict: j.verdict, analysis: j.analysis, model: j.model })
        setMarks((m) => ({ ...m, [dev.ip]: { severity: j.severity, verdict: j.verdict } }))
      } else {
        setThreat({ ip: dev.ip, loading: false, error: j.text || 'failed' })
      }
    } catch (e) {
      setThreat({ ip: dev.ip, loading: false, error: e instanceof Error ? e.message : String(e) })
    }
  }

  const researchSubnet = async (cidr: string) => {
    setPosture({ cidr, loading: true })
    try {
      const r = await fetch(`/api/rca/threat_subnet?cidr=${encodeURIComponent(cidr)}&lang=${lang}`)
      const j = await r.json()
      if (j.ok) {
        setMarks((m) => {
          const next = { ...m }
          for (const dv of j.devices) next[dv.ip] = { severity: dv.severity, verdict: dv.verdict }
          return next
        })
        setPosture({ cidr, loading: false, high: j.posture.high, watch: j.posture.watch, summary: j.posture.summary })
      } else {
        setPosture({ cidr, loading: false, summary: j.text || 'failed' })
      }
    } catch (e) {
      setPosture({ cidr, loading: false, summary: e instanceof Error ? e.message : String(e) })
    }
  }

  if (st.s === 'load') return <div className="boot"><span className="orbit" /></div>
  if (st.s === 'err') return <div className="boot err">gateway · {st.m}</div>

  const d = st.d
  const s = d.dataStats
  const topo = d.topology
  const c: RcaCase | undefined = d.cases.find((x) => x.id === active) ?? d.cases[0]
  const withCtl = d.baselines.find((b) => b.name === 'selfevo_light_path')?.rootCauseAccuracy ?? 1
  const noCtl = d.baselines.find((b) => b.name === 'full_tools')?.rootCauseAccuracy ?? 0

  return (
    <div className="stage">
      <header className="top">
        <div className="mark">
          selfevo<span className="mark-dot" />
        </div>
        <div className="top-right">
          <div className="cases">
            {d.cases.map((x) => (
              <button
                key={x.id}
                className={`case ${x.id === c?.id ? 'on' : ''}`}
                onClick={() => setActive(x.id)}
                title={rc(x.diagnosis.rootCauseKey, lang)}
              >
                <span className={`tick ${x.verifier.passed ? 'ok' : ''}`} />
              </button>
            ))}
          </div>
          <div className="engines">
            {d.providers.map((p) => (
              <button
                key={p.id}
                className={`eng ${p.id === d.provider ? 'on' : ''}`}
                disabled={!p.reachable && p.id !== 'rule'}
                onClick={() => setProvider(p.id)}
                title={`${p.label} · ${p.model}`}
              >
                <span className={`gem ${p.reachable ? 'live' : ''}`} />
                {p.label.split(' ')[0]}
              </button>
            ))}
          </div>
          <div className="lang">
            <button className={lang === 'zh' ? 'on' : ''} onClick={() => setLang('zh')}>中</button>
            <button className={lang === 'en' ? 'on' : ''} onClick={() => setLang('en')}>EN</button>
          </div>
        </div>
      </header>

      {d.datasetReady && s && c ? (
        <>
          <section className={`canvas-wrap ${drillSub ? 'big' : ''}`}>
            {topo ? (
              <TopologyCanvas
                topo={topo}
                stats={s}
                activeKey={c.diagnosis.rootCauseKey}
                drillSub={drillSub}
                drillDev={drillDev}
                tempo={tempo}
                marks={marks}
                onSub={(sub) => {
                  setDrillSub(sub?.cidr ?? null)
                  setDrillDev(null)
                  setThreat(null)
                  setPosture(null)
                }}
                onDev={researchDevice}
                onBatch={researchSubnet}
              />
            ) : null}
            {rate !== null ? (
              <div className="live-rate"><span className="rate-dot" />{rate}/s · R230</div>
            ) : null}
          </section>

          {threat || posture ? (
            <section className="analysis-strip">
              {threat ? <ThreatCard th={threat} lang={lang} onClose={() => setThreat(null)} /> : null}
              {posture ? (
                <aside className={`posture-card ${posture.high ? 'sev-high' : ''}`}>
                  <div className="tc-head">
                    <span className="tc-kicker">{lang === 'zh' ? '子网态势汇总' : 'subnet posture'} · {posture.cidr}</span>
                    <button className="tc-x" onClick={() => setPosture(null)}>✕</button>
                  </div>
                  {posture.loading ? (
                    <Analyzing lang={lang} />
                  ) : (
                    <div className="tc-body">
                      <div className="posture-counts">
                        <span className="pc high">{posture.high ?? 0}</span> high
                        <span className="pc watch">{posture.watch ?? 0}</span> watch
                      </div>
                      <p>{posture.summary}</p>
                    </div>
                  )}
                </aside>
              ) : null}
            </section>
          ) : null}

          <section className="deck">
            <div className="verdict">
              <ConfidenceRing value={c.diagnosis.confidence} />
              <div className="verdict-text">
                <span className="vk">{t('engine', lang)} · {d.reasonerMode}{c.verifier.passed ? ` · ${t('verified', lang)}` : ''}</span>
                <h1>{rc(c.diagnosis.rootCauseKey, lang)}</h1>
                <div className="ev-ids">
                  {c.diagnosis.evidence.map((e) => (
                    <code key={e.evidenceId}>{e.evidenceId}</code>
                  ))}
                </div>
              </div>
            </div>

            <div className="metric">
              <span className="big"><CountUp value={s.adminLoginFailed} /></span>
              <span className="lab">{t('failedLogins', lang)} · {s.distinctSrc} {t('sources', lang)} · {s.lockouts} {t('lockouts', lang)}</span>
            </div>
            <div className="metric">
              <span className="big"><CountUp value={s.denyCount} /></span>
              <span className="lab">{t('denied', lang)}</span>
            </div>
            <div className="metric">
              <div className="bars">
                <div className="bar-row">
                  <span className="bar-num good">{Math.round(withCtl * 100)}<i>%</i></span>
                  <span className="bar"><b style={{ width: `${withCtl * 100}%` }} className="good" /></span>
                  <span className="bar-lab">{t('withControl', lang)}</span>
                </div>
                <div className="bar-row">
                  <span className="bar-num bad">{Math.round(noCtl * 100)}<i>%</i></span>
                  <span className="bar"><b style={{ width: `${noCtl * 100}%` }} className="bad" /></span>
                  <span className="bar-lab">{t('withoutControl', lang)}</span>
                </div>
              </div>
              <span className="lab">{t('accuracy', lang)}</span>
            </div>
          </section>
        </>
      ) : (
        <div className="boot err">{d.note}</div>
      )}
    </div>
  )
}

export default App
