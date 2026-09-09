import { useCallback, useEffect, useState } from 'react'
import './App.css'
import type { RcaCase, RcaSnapshot } from './types'
import { type Lang } from './i18n'
import { TopologyCanvas } from './components/TopologyCanvas'
import { Analyzing, ThreatCard, type Threat, type WanThreat } from './components/ThreatCard'
import { LiveAlerts } from './components/LiveAlerts'
import { TopoSearch } from './components/TopoSearch'
import { ProductionTopologyPage } from './components/ProductionTopologyPage'
import { lazy, Suspense } from 'react'
import { InvestigationFlowBar } from './components/InvestigationFlowBar'
import {
  createInvestigationContext,
  investigationView,
  readInvestigationContext,
  visitInvestigationContext,
  writeInvestigationContext,
  type InvestigationIntent,
  type InvestigationSourceEventInput,
  type InvestigationView,
} from './components/investigation-context'

const Constellation3D = lazy(() => import('./components/Constellation3D').then((m) => ({ default: m.Constellation3D })))
const TrajectoryPage = lazy(() => import('./components/TrajectoryPage').then((m) => ({ default: m.TrajectoryPage })))
const CostPage = lazy(() => import('./components/CostPage').then((m) => ({ default: m.CostPage })))
const DiagnosePage = lazy(() => import('./components/DiagnosePage').then((m) => ({ default: m.DiagnosePage })))
const RetrievalPage = lazy(() => import('./components/RetrievalPage').then((m) => ({ default: m.RetrievalPage })))
const BenchConsole = lazy(() => import('./components/BenchConsole').then((m) => ({ default: m.BenchConsole })))
const TrajectoryBench = lazy(() => import('./components/TrajectoryBench').then((m) => ({ default: m.TrajectoryBench })))
const GlobalMemoryPage = lazy(() => import('./components/GlobalMemoryPage').then((m) => ({ default: m.GlobalMemoryPage })))
const CaseInvestigationPage = lazy(() => import('./components/CaseInvestigationPage').then((m) => ({ default: m.CaseInvestigationPage })))
const SituationalOverview = lazy(() => import('./components/SituationalOverview').then((m) => ({ default: m.SituationalOverview })))

type MeshModel = {
  links: { src: string; dst: string; relation: string; strength: number }[]
  nodes: Record<string, { severity: string; label: string; summary: string }>
}
import type { DataStats, Device, GraphAnalysis, SubnetGraph, TheaterEvent, Topology } from './types'

type View = InvestigationView | 'network' | 'memory' | 'cost'
type Scenario = 'live' | 'bench'

type State =
  | { s: 'load' }
  | { s: 'err'; m: string }
  | { s: 'ok'; d: RcaSnapshot }

const sourceFromTheater = (event?: TheaterEvent): InvestigationSourceEventInput => ({
  id: event?.id,
  kind: event?.kind,
  observedAt: event?.ts,
  summary: event?.summary,
  severity: event?.severity,
  scope: event?.scope,
})

function App() {
  const [lang, setLang] = useState<Lang>('zh')
  const [view, setView] = useState<View>('console')
  // Set when a live-situation row asks to see its response chain; the
  // diagnose page opens focused on that subject instead of everything.
  const [traceSubject, setTraceSubject] = useState<string | null>(null)
  const [traceCaseId, setTraceCaseId] = useState<string | null>(null)
  const [investigationContext, setInvestigationContext] = useState(readInvestigationContext)
  const [investigationActive, setInvestigationActive] = useState(false)
  const focusedContext = investigationActive ? investigationContext : null
  const [scenario, setScenario] = useState<Scenario>('live')
  const [provider, setProvider] = useState('rule')
  const [st, setSt] = useState<State>({ s: 'load' })
  const [active, setActive] = useState('')
  const [drillSub, setDrillSub] = useState<string | null>(null)
  const [drillDev, setDrillDev] = useState<string | null>(null)
  const [tempo] = useState(1)
  const [rate] = useState<number | null>(null)
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
  const [graph, setGraph] = useState<SubnetGraph | null>(null)
  const [graphAnalysis, setGraphAnalysis] = useState<GraphAnalysis | null>(null)
  const [hoverDev, setHoverDev] = useState<string | null>(null)
  // ── event-driven full-chain topology theater ──
  const [theater, setTheater] = useState<TheaterEvent | null>(null)
  const [allGraphs, setAllGraphs] = useState<Record<string, SubnetGraph>>({})
  // bench full-chain theater: the benchmark memory topology (same snapshot BenchConsole uses)
  const [benchSnap, setBenchSnap] = useState<{ topology: Topology; stats: DataStats; graph: SubnetGraph; cidr: string } | null>(null)

  useEffect(() => writeInvestigationContext(investigationContext), [investigationContext])

  const beginInvestigation = useCallback((
    subject: string,
    caseId?: string | null,
    sourceEvent: InvestigationSourceEventInput = {},
    originView?: InvestigationView,
    intent: InvestigationIntent = 'evidence_hypotheses',
    navigate = true,
  ) => {
    const sourceView: InvestigationView = originView ?? (view === 'cost' || view === 'network' || view === 'memory' ? 'overview' : view)
    const started = createInvestigationContext({
      caseId,
      subject,
      sourceEvent,
      originView: sourceView,
      intent: 'event_facts',
    })
    const focused = visitInvestigationContext(started, intent, navigate ? investigationView(intent) : sourceView)
    setTraceSubject(subject)
    setTraceCaseId(caseId ?? null)
    setInvestigationActive(true)
    setInvestigationContext(focused)
    if (navigate) setView(investigationView(intent))
  }, [view])

  const navigateInvestigation = useCallback((intent: InvestigationIntent) => {
    setInvestigationActive(true)
    setInvestigationContext((current) => current ? visitInvestigationContext(current, intent) : current)
    setView(investigationView(intent))
  }, [])

  const openInvestigationView = useCallback((nextView: InvestigationView, intent: InvestigationIntent) => {
    setInvestigationActive(true)
    setInvestigationContext((current) => current
      ? visitInvestigationContext(current, intent, nextView)
      : current)
    setView(nextView)
  }, [])

  const openGlobalView = (nextView: View) => {
    setInvestigationActive(false)
    setTraceSubject(null)
    setTraceCaseId(null)
    setView(nextView)
  }

  /* the theater expands EVERY subnet at once, so it needs every mined graph */
  const loadAllGraphs = useCallback(async (cidrs: string[]) => {
    const entries = await Promise.all(
      cidrs.map(async (c) => {
        try {
          const r = await fetch(`/api/rca/subnet_graph?cidr=${encodeURIComponent(c)}`)
          const j = await r.json()
          return j.ok ? ([c, j as SubnetGraph] as const) : null
        } catch {
          return null
        }
      }),
    )
    const next: Record<string, SubnetGraph> = {}
    for (const e of entries) if (e) next[e[0]] = e[1]
    setAllGraphs(next)
  }, [])

  useEffect(() => {
    if (scenario !== 'bench' || benchSnap) return
    let alive = true
    fetch('/api/rca/bench-snapshot', { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error())))
      .then((dd) => { if (alive && dd && dd.ok) setBenchSnap(dd) })
      .catch(() => {})
    return () => { alive = false }
  }, [scenario, benchSnap])

  const openTheater = (evt: TheaterEvent) => {
    setTheater(evt)
    // live-only view/drill resets; the bench theater is a standalone full-screen overlay
    if (scenario === 'live') {
      setView('console')
      setDrillSub(null)
      setGraph(null)
      setGraphAnalysis(null)
      setDrillDev(null)
      setThreat(null)
      setWan(null)
      setPosture(null)
      setShow3D(false)
      if (st.s === 'ok' && st.d.topology && !Object.keys(allGraphs).length) {
        void loadAllGraphs(st.d.topology.subnets.map((x) => x.cidr))
      }
    }
  }
  const closeTheater = () => {
    setTheater(null)
    setWan(null)
    setThreat(null)
  }

  // Opening a segment loads its FULL device graph — every host the firewall has ever
  // seen on that /24, plus the device<->device relations mined from the raw syslog.
  const openSubnet = async (cidr: string | null) => {
    setDrillSub(cidr)
    setDrillDev(null)
    setThreat(null)
    setWan(null)
    setPosture(null)
    setGraphAnalysis(null)
    setHoverDev(null)
    if (!cidr) {
      setGraph(null)
      return
    }
    setGraph(null)
    try {
      const r = await fetch(`/api/rca/subnet_graph?cidr=${encodeURIComponent(cidr)}`)
      const j = await r.json()
      setGraph(j.ok ? (j as SubnetGraph) : null)
    } catch {
      setGraph(null)
    }
  }

  // search → drill to the device's subnet and focus it (opens its profile/画像)
  const onSearchPick = async (ip: string, cidr: string) => {
    await openSubnet(cidr)
    setDrillDev(ip)
  }

  const correlateGraph = async (cidr: string) => {
    setGraphAnalysis({ cidr, loading: true })
    try {
      const r = await fetch(`/api/rca/graph_analyze?cidr=${encodeURIComponent(cidr)}&lang=${lang}`)
      const j = await r.json()
      setGraphAnalysis(j.ok ? { ...j, loading: false } : { cidr, loading: false, error: j.text || 'failed' })
    } catch (e) {
      setGraphAnalysis({ cidr, loading: false, error: e instanceof Error ? e.message : String(e) })
    }
  }

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
    setGraph(null)
    setGraphAnalysis(null)
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
    if (scenario === 'bench' && view !== 'trajectory') void load(provider)
  }, [provider, load, scenario, view])

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

  const d = st.s === 'ok' ? st.d : null
  const s = d?.dataStats
  const topo = d?.topology
  const c: RcaCase | undefined = d?.cases.find((x) => x.id === active) ?? d?.cases[0]

  return (
    <div className="stage" data-view={view}>
      <header className="top">
        <div className="mark">
          autopoiesis<span className="mark-dot" />
        </div>
        <div className="top-right">
          <div className="scen" role="group" aria-label={lang === 'zh' ? '场景切换' : 'scenario'}>
            <button className={scenario === 'live' ? 'on' : ''} onClick={() => setScenario('live')}>{lang === 'zh' ? '生产环境' : 'PRODUCTION'}</button>
            <button className={scenario === 'bench' ? 'on' : ''} onClick={() => { setScenario('bench'); setInvestigationActive(false); if (['overview', 'constellation', 'memory', 'investigation'].includes(view)) setView('console') }}>{lang === 'zh' ? '基准 · ITBench+LME' : 'BENCHMARK'}</button>
          </div>
            <nav className="pager" aria-label={lang === 'zh' ? '常态工作区' : 'Global workspaces'}>
              <button className={view === 'console' ? 'on' : ''} onClick={() => openGlobalView('console')}>{lang === 'zh' ? '网络态势' : 'NETWORK MAP'}</button>
              {scenario === 'live' ? <button className={view === 'constellation' ? 'on' : ''} onClick={() => openGlobalView('constellation')}>{lang === 'zh' ? '3D 星图' : '3D MAP'}</button> : null}
              {scenario === 'live' ? <button className={view === 'overview' ? 'on' : ''} onClick={() => openGlobalView('overview')}>{lang === 'zh' ? '待处理事件' : 'INCIDENTS'}</button> : null}
              {scenario === 'live' ? <button className={view === 'memory' ? 'on' : ''} onClick={() => openGlobalView('memory')}>{lang === 'zh' ? '长期记忆' : 'LONG-TERM MEMORY'}</button> : <button className={view === 'trajectory' ? 'on' : ''} onClick={() => openGlobalView('trajectory')}>{lang === 'zh' ? '案例与记忆' : 'CASES & MEMORY'}</button>}
              <button className={view === 'network' ? 'on' : ''} onClick={() => openGlobalView('network')}>{lang === 'zh' ? '网络背景' : 'NETWORK CONTEXT'}</button>
              <button className={view === 'diagnose' ? 'on' : ''} onClick={() => openGlobalView('diagnose')}>{lang === 'zh' ? '配置与处置' : 'CHANGES & ACTIONS'}</button>
              <button className={view === 'retrieval' ? 'on' : ''} onClick={() => openGlobalView('retrieval')}>{lang === 'zh' ? '调查依据' : 'SOURCES USED'}</button>
              <button className={view === 'cost' ? 'on' : ''} onClick={() => openGlobalView('cost')}>{lang === 'zh' ? '成本' : 'COST'}</button>
            </nav>

          {/* The selector governs exactly one thing: the case diagnosis in
              /api/rca/snapshot. Baselines, evolution and the live threat cards
              resolve their own reasoner server-side and ignore it — so it is
              captioned with its real scope, and hidden on the diagnose page where it
              governs nothing at all. */}
          {scenario !== 'live' || view !== 'trajectory' || !d ? null : (
            <div className="engines" role="group" aria-label={lang === 'zh' ? '案例诊断引擎' : 'Case-diagnosis reasoner'}>
              <span className="eng-scope">{lang === 'zh' ? '案例诊断' : 'Case diag'}</span>
              {d.providers.map((p) => {
                const off = !p.reachable && p.id !== 'rule'
                return (
                  <button
                    key={p.id}
                    className={`eng ${p.id === d.provider ? 'on' : ''} ${off ? 'off' : ''}`}
                    disabled={off}
                    onClick={() => setProvider(p.id)}
                    title={off ? `${p.label} · ${lang === 'zh' ? '不可达' : 'unreachable'}；${p.note}` : `${p.label} · ${p.model}`}
                  >
                    <span className={`gem ${p.reachable ? 'live' : ''}`} />
                    <span className="eng-name">{p.label.split(' ')[0]}</span>
                    {off ? <span className="eng-off">{lang === 'zh' ? '离线' : 'Off'}</span> : null}
                  </button>
                )
              })}
              {d.providerError ? (
                <span className="eng-err" title={d.providerError}>
                  {lang === 'zh' ? '引擎失败' : 'Engine failed'}
                </span>
              ) : null}
            </div>
          )}
          <div className="lang">
            <button className={lang === 'zh' ? 'on' : ''} onClick={() => setLang('zh')}>中</button>
            <button className={lang === 'en' ? 'on' : ''} onClick={() => setLang('en')}>EN</button>
          </div>
        </div>
      </header>

      {scenario === 'live' && focusedContext ? (
        <InvestigationFlowBar
          context={focusedContext}
          lang={lang}
          onNavigate={navigateInvestigation}
          onClose={() => {
            setInvestigationActive(false)
            setInvestigationContext(null)
            setTraceSubject(null)
            setTraceCaseId(null)
            setView('overview')
          }}
        />
      ) : null}

      {scenario === 'live' && !investigationActive && investigationContext ? <div className="investigation-resume">
        <span>{lang === 'zh' ? '保留的调查' : 'SAVED INVESTIGATION'} · {investigationContext.subject}</span>
        <button onClick={() => navigateInvestigation(investigationContext.currentIntent)}>{lang === 'zh' ? '继续调查' : 'RESUME INVESTIGATION'} →</button>
      </div> : null}
      <Suspense fallback={<div className="boot"><span className="orbit" /></div>}>
      {(view === 'console' || view === 'constellation') && scenario === 'live' ? (
        <ProductionTopologyPage
          lang={lang}
          focusSubject={focusedContext?.subject ?? (investigationActive ? traceSubject ?? undefined : undefined)}
          showConstellation={view === 'constellation'}
          onShowConstellation={(show) => setView(show ? 'constellation' : 'console')}
          onOpenExposure={() => openGlobalView('network')}
          onTrackIncident={(subject, event, caseId) => beginInvestigation(
            subject, caseId, sourceFromTheater(event), view === 'constellation' ? 'constellation' : 'console', 'impact_scope', false,
          )}
          onOpenCase={(subject, caseId, sourceEvent) => beginInvestigation(
            subject, caseId, sourceEvent, view === 'constellation' ? 'constellation' : 'console', 'evidence_hypotheses', true,
          )}
        />
      ) : view === 'overview' && scenario === 'live' ? (
        <SituationalOverview
          lang={lang}
          onOpenCase={(subject, caseId, sourceEvent) => beginInvestigation(
            subject, caseId, sourceEvent, 'overview', sourceEvent?.kind === 'controlled_demo' ? 'related_memory' : 'evidence_hypotheses', true,
          )}
          onOpenTrace={(subject, caseId) => {
            beginInvestigation(subject, caseId, {}, 'overview', 'impact_scope')
          }}
        />
      ) : view === 'memory' && scenario === 'live' ? (
        <GlobalMemoryPage lang={lang} onOpenSource={({ caseId, subject }) => beginInvestigation(subject, caseId, {}, 'trajectory', 'event_facts')} />
      ) : view === 'investigation' && scenario === 'live' ? (
        <CaseInvestigationPage
          lang={lang}
          caseId={focusedContext?.caseId ?? traceCaseId ?? undefined}
          subject={focusedContext?.subject ?? traceSubject ?? undefined}
          onOpenSources={() => navigateInvestigation('retrieval_evidence')}
          onOpenMemory={() => navigateInvestigation('related_memory')}
          onOpenActions={() => navigateInvestigation('remediation_readback')}
        />
      ) : view === 'network' ? (
        <DiagnosePage key={`network:${scenario}`} lang={lang} scenario={scenario} workspace="network" />
      ) : view === 'cost' ? (
        <CostPage lang={lang} />
      ) : view === 'retrieval' ? (
        <RetrievalPage lang={lang} scenario={scenario} focusCaseId={focusedContext?.caseId ?? undefined} focusSubject={focusedContext?.subject} />
      ) : view === 'diagnose' ? (
        <DiagnosePage
          key={`response:${focusedContext?.caseId ?? "global"}`}
          workspace={scenario === 'bench' ? 'network' : 'response'}
          lang={lang}
          scenario={scenario}
          focusSubject={focusedContext?.subject ?? (investigationActive ? traceSubject ?? undefined : undefined)}
          focusCaseId={focusedContext?.caseId ?? (investigationActive ? traceCaseId ?? undefined : undefined)}
          investigationIntent={focusedContext?.currentIntent}
          onIntentChange={focusedContext ? (intent) => openInvestigationView('diagnose', intent) : undefined}
        />
      ) : view === 'console' && scenario === 'bench' ? (
        <BenchConsole lang={lang} />
      ) : view === 'trajectory' && scenario === 'bench' ? (
        <TrajectoryBench lang={lang} />
      ) : view === 'trajectory' ? (
        <TrajectoryPage
          key={`${active}:${lang}`}
          cases={d?.cases ?? []}
          lang={lang}
          activeId={active}
          onPick={setActive}
          onTheater={openTheater}
          onSourceChange={(subject, caseId) => beginInvestigation(subject, caseId, {}, 'trajectory', 'related_memory', true)}
          onTrace={(subject, caseId, event) => beginInvestigation(
            subject, caseId, sourceFromTheater(event), 'trajectory', 'evidence_hypotheses', true,
          )}
          focusSubject={focusedContext?.subject ?? (investigationActive ? traceSubject ?? undefined : undefined)}
          focusCaseId={focusedContext?.caseId ?? (investigationActive ? traceCaseId ?? undefined : undefined)}
          focusArea={focusedContext?.currentIntent === 'related_memory' ? 'live-memory' : undefined}
        />
      ) : st.s === 'load' ? (
        <div className="boot"><span className="orbit" /></div>
      ) : st.s === 'err' ? (
        <div className="boot err">gateway · {st.m}</div>
      ) : d?.datasetReady && s && c ? (
        <>
          {scenario === 'live' ? (
            <LiveAlerts
              lang={lang}
              theaterActive={Boolean(theater)}
              activeSubject={theater?.device}
              onOpen={(subject, event, caseId) => {
                beginInvestigation(subject, caseId, sourceFromTheater(event), view === 'memory' ? 'overview' : view, 'evidence_hypotheses', true)
                if (theater) openTheater(event)
              }}
            />
          ) : null}
          <section className={`canvas-wrap ${show3D ? 'full3d' : threat || wan ? 'tall' : drillSub ? 'mid' : ''}`}>
            {scenario === 'live' && !show3D ? <TopoSearch lang={lang} onPick={onSearchPick} /> : null}
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
                graph={graph}
                graphAnalysis={graphAnalysis}
                hoverDev={hoverDev}
                onHoverDev={setHoverDev}
                onGraphAnalyze={correlateGraph}
                onCloseGraphAnalysis={() => setGraphAnalysis(null)}
                onWan={researchWan}
                onCloseWan={() => setWan(null)}
                onHoverSubnet={show3D ? setFocusCidr : undefined}
                onOpen3D={() => { void openSubnet(null); void analyzeMesh() }}
                onCloseThreat={() => setThreat(null)}
                onSub={(sub) => void openSubnet(sub?.cidr ?? null)}
                onDev={researchDevice}
                onBatch={researchSubnet}
                onDiagnose={() => setView('diagnose')}
                theater={theater}
                allGraphs={allGraphs}
                onCloseTheater={closeTheater}
                onOpenTheater={() =>
                  openTheater({ kind: 'browse', id: `browse-${Date.now()}`, ts: '', device: '', stageIds: [] })
                }
              />
            ) : null}
            {rate !== null ? (
              <div className="live-rate"><span className="rate-dot" />{rate}/s · {lang === 'zh' ? '实时事实流' : 'LIVE FACT STREAM'}</div>
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
                    {/* /api/rca/threat_subnet takes no provider — it always runs
                        DeepSeek server-side. Name the real reasoner, not the header's. */}
                    <span className="prov-tag">{lang === 'zh' ? '引擎 · DEEPSEEK' : 'Reasoner · DeepSeek'}</span>
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
        <div className="boot err">{d?.note ?? (lang === 'zh' ? '当前视图没有可用数据' : 'NO DATA FOR THIS VIEW')}</div>
      )}
      </Suspense>

      {/* Bench full-chain theater: standalone full-screen overlay REUSING the live
          TopologyCanvas in theater mode, fed the benchmark memory snapshot. Opened from
          the benchmark 长轨迹 LiveSituation cards to replay the injected fault flowing
          correlator -> alerts -> cluster -> aiops -> suggestion -> remediation. */}
      {scenario === 'bench' && theater && benchSnap ? (
        <div className="bench-theater-overlay" role="dialog" aria-label={lang === 'zh' ? '全链路剧场' : 'full-chain theater'}>
          <button className="bench-theater-x" onClick={closeTheater}>{lang === 'zh' ? '✕ 退出剧场' : '✕ EXIT'}</button>
          <section className="canvas-wrap tall">
            <TopologyCanvas
              topo={benchSnap.topology}
              stats={benchSnap.stats}
              activeKey=""
              drillSub={benchSnap.cidr}
              drillDev={null}
              tempo={1}
              marks={{}}
              threat={null}
              lang={lang}
              meshCount={0}
              meshLoading={false}
              hover3D={null}
              hover3DCidr={null}
              topoAlert={null}
              wan={null}
              graph={benchSnap.graph}
              graphAnalysis={null}
              hoverDev={null}
              onHoverDev={() => {}}
              onGraphAnalyze={() => {}}
              onCloseGraphAnalysis={() => {}}
              onWan={() => {}}
              onCloseWan={() => {}}
              onHoverSubnet={undefined}
              onOpen3D={() => {}}
              onCloseThreat={() => {}}
              onSub={() => {}}
              onDev={() => {}}
              onBatch={() => {}}
              onDiagnose={() => {}}
              theater={theater}
              allGraphs={{ [benchSnap.cidr]: benchSnap.graph }}
              onCloseTheater={closeTheater}
              onOpenTheater={() => {}}
            />
          </section>
        </div>
      ) : null}
    </div>
  )
}

export default App
