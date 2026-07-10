import { useCallback, useEffect, useState } from 'react'
import './App.css'
import type { RcaCase, RcaSnapshot } from './types'
import { rc, type Lang } from './i18n'
import { TopologyCanvas } from './components/TopologyCanvas'
import { Analyzing, ThreatCard, type Threat, type WanThreat } from './components/ThreatCard'
import { TrajectoryPage } from './components/TrajectoryPage'
import { PentestPage } from './components/PentestPage'
import { lazy, Suspense } from 'react'

const Constellation3D = lazy(() => import('./components/Constellation3D').then((m) => ({ default: m.Constellation3D })))

type MeshModel = {
  links: { src: string; dst: string; relation: string; strength: number }[]
  nodes: Record<string, { severity: string; label: string; summary: string }>
}
import type { Device } from './types'

type View = 'console' | 'trajectory' | 'pentest'

type State =
  | { s: 'load' }
  | { s: 'err'; m: string }
  | { s: 'ok'; d: RcaSnapshot }

function App() {
  const [lang, setLang] = useState<Lang>('zh')
  const [view, setView] = useState<View>('console')
  const [provider, setProvider] = useState('rule')
  const [st, setSt] = useState<State>({ s: 'load' })
  const [active, setActive] = useState('')
  const [drillSub, setDrillSub] = useState<string | null>(null)
  const [drillDev, setDrillDev] = useState<string | null>(null)
  const [tempo, setTempo] = useState(1)
  const [rate, setRate] = useState<number | null>(null)
  const [threat, setThreat] = useState<Threat | null>(null)
  const [wan, setWan] = useState<WanThreat | null>(null)
  const [marks, setMarks] = useState<Record<string, { severity: string; verdict: string }>>({})
  const [posture, setPosture] = useState<{ cidr: string; loading: boolean; high?: number; watch?: number; summary?: string } | null>(null)
  const [meshModel, setMeshModel] = useState<MeshModel | null>(null)
  const [meshLoading, setMeshLoading] = useState(false)
  const [show3D, setShow3D] = useState(false)
  const [hover3D, setHover3D] = useState<string | null>(null)
  const [focusCidr, setFocusCidr] = useState<string | null>(null)
  const [topoAlert, setTopoAlert] = useState<{ cidr: string; ip: string; verdict: string; severity: string } | null>(null)

  const research3D = async (ip: string, cidr: string) => {
    setThreat({ ip, loading: true })
    setTopoAlert({ cidr, ip, verdict: '', severity: '' })
    try {
      const r = await fetch(`/api/rca/threat?ip=${ip}&cidr=${encodeURIComponent(cidr)}&lang=${lang}`)
      const j = await r.json()
      if (j.ok) {
        setThreat({ ip, loading: false, severity: j.severity, verdict: j.verdict, analysis: j.analysis, impactPeers: j.impactPeers, mostLikely: j.mostLikely, worstCase: j.worstCase, recovery: j.recovery, model: j.model })
        setMarks((m) => ({ ...m, [ip]: { severity: j.severity, verdict: j.verdict } }))
        setTopoAlert({ cidr, ip, verdict: j.verdict, severity: j.severity })
      } else {
        setThreat({ ip, loading: false, error: j.text })
      }
    } catch (e) {
      setThreat({ ip, loading: false, error: e instanceof Error ? e.message : String(e) })
    }
  }

  const researchWan = async (ip: string) => {
    setWan({ ip, loading: true })
    setDrillSub(null)
    setDrillDev(null)
    setThreat(null)
    setPosture(null)
    try {
      const r = await fetch(`/api/rca/wan_threat?ip=${encodeURIComponent(ip)}&lang=${lang}`)
      const j = await r.json()
      if (j.ok) {
        setWan({ ...j, loading: false })
      } else {
        setWan({ ip, loading: false, error: j.text || 'failed' })
      }
    } catch (e) {
      setWan({ ip, loading: false, error: e instanceof Error ? e.message : String(e) })
    }
  }

  const analyzeMesh = async () => {
    const ds0 = st.s === 'ok' ? st.d : null
    if (meshModel) {
      setShow3D(true)
      return
    }
    const ds = ds0
    const cidrs = ds ? Object.keys(ds.meshes ?? {}) : []
    if (!cidrs.length) return
    setMeshLoading(true)
    try {
      const all = await Promise.all(
        cidrs.map((c) => fetch(`/api/rca/mesh_analyze?cidr=${encodeURIComponent(c)}&lang=${lang}`).then((r) => r.json())),
      )
      const links: MeshModel['links'] = []
      const nodes: MeshModel['nodes'] = {}
      for (const j of all) {
        if (!j.ok) continue
        for (const l of j.links ?? []) links.push(l)
        for (const n of j.nodes ?? []) nodes[n.ip] = { severity: n.severity, label: n.label, summary: n.summary }
      }
      setMeshModel({ links, nodes })
      setShow3D(true)
    } catch {
      setMeshModel(null)
    } finally {
      setMeshLoading(false)
    }
  }

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
    setWan(null)
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
        setThreat({
          ip: dev.ip, loading: false, severity: j.severity, verdict: j.verdict, analysis: j.analysis,
          impactPeers: j.impactPeers, mostLikely: j.mostLikely, worstCase: j.worstCase, recovery: j.recovery, model: j.model,
        })
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

  return (
    <div className="stage" data-view={view}>
      <header className="top">
        <div className="mark">
          selfevo<span className="mark-dot" />
        </div>
        <div className="top-right">
          <div className="pager">
            <button className={view === 'console' ? 'on' : ''} onClick={() => setView('console')}>{lang === 'zh' ? '态势' : 'CONSOLE'}</button>
            <button className={view === 'trajectory' ? 'on' : ''} onClick={() => setView('trajectory')}>{lang === 'zh' ? '长轨迹' : 'TRAJECTORY'}</button>
            <button className={view === 'pentest' ? 'on' : ''} onClick={() => setView('pentest')}>{lang === 'zh' ? '渗透' : 'PENTEST'}</button>
          </div>
          {view === 'console' ? (
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
          ) : null}
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

      {view === 'pentest' ? (
        <PentestPage lang={lang} />
      ) : view === 'trajectory' && d.datasetReady && c ? (
        <TrajectoryPage
          key={`${active}:${lang}`}
          cases={d.cases}
          baselines={d.baselines}
          reasoner={d.reasonerMode}
          lang={lang}
          activeId={active}
          onPick={setActive}
        />
      ) : d.datasetReady && s && c ? (
        <>
          <section className={`canvas-wrap ${show3D ? 'full3d' : threat || wan ? 'tall' : drillSub ? 'mid' : ''}`}>
            {topo && !show3D ? (
              <TopologyCanvas
                topo={topo}
                stats={s}
                activeKey={c.diagnosis.rootCauseKey}
                drillSub={drillSub}
                drillDev={drillDev}
                tempo={tempo}
                marks={marks}
                threat={threat}
                lang={lang}
                meshCount={Object.values(d.meshes ?? {}).reduce((a, l) => a + l.length, 0)}
                meshLoading={meshLoading}
                hover3D={hover3D}
                hover3DCidr={hover3D ? Object.entries(d.meshes ?? {}).find(([, l]) => l.some((n) => n.ip === hover3D))?.[0] ?? null : null}
                topoAlert={topoAlert}
                wan={wan}
                onWan={researchWan}
                onCloseWan={() => setWan(null)}
                onHoverSubnet={show3D ? setFocusCidr : undefined}
                onOpen3D={() => { setDrillSub(null); setDrillDev(null); setThreat(null); setWan(null); void analyzeMesh() }}
                onCloseThreat={() => setThreat(null)}
                onSub={(sub) => {
                  setDrillSub(sub?.cidr ?? null)
                  setDrillDev(null)
                  setThreat(null)
                  setWan(null)
                  setPosture(null)
                }}
                onDev={researchDevice}
                onBatch={researchSubnet}
                onPentest={() => setView('pentest')}
              />
            ) : null}
            {rate !== null ? (
              <div className="live-rate"><span className="rate-dot" />{rate}/s · R230</div>
            ) : null}
            {show3D && d.meshes && topo ? (
              <>
                <Suspense fallback={<div className="c3d-inline c3d-booting">3D…</div>}>
                  <Constellation3D
                    topo={topo}
                    stats={s}
                    meshes={d.meshes}
                    model={meshModel}
                    lang={lang}
                    onClose={() => { setShow3D(false); setHover3D(null); setFocusCidr(null); setThreat(null); setTopoAlert(null) }}
                    onHoverIp={setHover3D}
                    onClickIp={research3D}
                    focusCidr={focusCidr}
                  />
                </Suspense>
                {threat ? (
                  <div className="c3d-threat">
                    <ThreatCard th={threat} lang={lang} onClose={() => { setThreat(null); setTopoAlert(null) }} />
                  </div>
                ) : null}
              </>
            ) : null}
          </section>

          {posture ? (
            <section className="analysis-strip">
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

        </>
      ) : (
        <div className="boot err">{d.note}</div>
      )}
    </div>
  )
}

export default App
