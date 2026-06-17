import { useCallback, useEffect, useState } from 'react'
import './App.css'
import type { RcaCase, RcaSnapshot } from './types'
import { makeT, rootCauseLabel, type Lang } from './i18n'
import { RcaFlow } from './components/RcaFlow'
import { NetworkTopology } from './components/NetworkTopology'
import { AblationChart, DenyPortChart } from './components/Charts'
import { ProviderSwitch } from './components/ProviderSwitch'

type View = 'overview' | 'topology' | 'pipeline' | 'compare'
type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; snapshot: RcaSnapshot }

const fmt = (n: number) => n.toLocaleString('en-US')

const TRACE_LABELS: Record<string, { en: string; zh: string }> = {
  alert_received: { en: 'Alert received', zh: '收到告警' },
  memory_read: { en: 'Memory retrieved', zh: '检索记忆' },
  skills_exposed: { en: 'Skills selected', zh: '选择技能' },
  tool_called: { en: 'Readonly probe', zh: '只读取证' },
  context_compiled: { en: 'Context compiled', zh: '压缩上下文' },
  verifier_result: { en: 'Verifier checked', zh: '校验结论' },
  cost_observed: { en: 'Cost recorded', zh: '记录成本' },
  diagnosis_completed: { en: 'Diagnosis produced', zh: '产出诊断' },
}

function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub ? <div className="stat-sub">{sub}</div> : null}
    </div>
  )
}

function Verdict({ rcaCase, lang }: { rcaCase: RcaCase; lang: Lang }) {
  const t = makeT(lang)
  const d = rcaCase.diagnosis
  return (
    <div className="verdict">
      <div className="verdict-main">
        <span className="kicker">
          {t('diagnosis')} · {d.readonly ? t('readonly') : 'WRITE'} ·{' '}
          {rcaCase.verifier.passed ? t('verifierPassed') : t('verifierFailed')}
        </span>
        <div className="verdict-title">{rootCauseLabel(d.rootCauseKey, lang)}</div>
        <div className="evidence-chips">
          {d.evidence.map((e) => (
            <span className="evidence-chip" key={e.evidenceId} title={e.source}>
              <code>{e.evidenceId}</code>
              {e.summary}
            </span>
          ))}
        </div>
      </div>
      <div className="gauge">
        <div className="gauge-num">{d.confidence.toFixed(2)}</div>
        <div className="gauge-label">{t('confidence')}</div>
      </div>
    </div>
  )
}

function App() {
  const [lang, setLang] = useState<Lang>('zh')
  const [provider, setProvider] = useState('rule')
  const [view, setView] = useState<View>('overview')
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [activeId, setActiveId] = useState('')
  const t = makeT(lang)

  const load = useCallback(async (prov: string, refresh = false) => {
    setState({ status: 'loading' })
    try {
      const res = await fetch(`/api/rca/snapshot?provider=${prov}${refresh ? '&refresh=true' : ''}`, {
        headers: { Accept: 'application/json' },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const snapshot = (await res.json()) as RcaSnapshot
      setState({ status: 'ready', snapshot })
      setActiveId((prev) => (snapshot.cases.some((c) => c.id === prev) ? prev : snapshot.cases[0]?.id ?? ''))
    } catch (err) {
      setState({ status: 'error', message: err instanceof Error ? err.message : String(err) })
    }
  }, [])

  useEffect(() => {
    void load(provider)
  }, [provider, load])

  if (state.status === 'loading') {
    return <div className="centered"><span className="spinner" /> {provider} · {t('refresh')}…</div>
  }
  if (state.status === 'error') {
    return (
      <div className="centered error">
        <p>gateway: {state.message}</p>
        <button className="btn" onClick={() => void load(provider)}>{t('refresh')}</button>
      </div>
    )
  }

  const { snapshot } = state
  const r = snapshot.readiness
  const s = snapshot.dataStats
  const activeCase = snapshot.cases.find((c) => c.id === activeId) ?? snapshot.cases[0]
  const views: View[] = ['overview', 'topology', 'pipeline', 'compare']
  const viewLabel: Record<View, { en: string; zh: string }> = {
    overview: { en: 'Overview', zh: '总览' },
    topology: { en: 'Topology', zh: '拓扑' },
    pipeline: { en: 'Pipeline', zh: '推理管道' },
    compare: { en: 'Compare', zh: '消融对照' },
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">selfevo</span>
          <span className="brand-sub">{t('brandSub')}</span>
        </div>
        <div className="header-right">
          <ProviderSwitch providers={snapshot.providers} active={snapshot.provider} lang={lang} onChange={setProvider} />
          <div className="lang-toggle">
            <button className={lang === 'zh' ? 'active' : ''} onClick={() => setLang('zh')}>中</button>
            <button className={lang === 'en' ? 'active' : ''} onClick={() => setLang('en')}>EN</button>
          </div>
          <span className={`pill ${r.blocked ? 'pill-bad' : 'pill-ok'}`}>{r.blocked ? t('blocked') : t('live')}</span>
          <button className="btn" onClick={() => void load(provider, true)} title={t('refresh')}>↻</button>
        </div>
      </header>

      {snapshot.providerError ? (
        <div className="provider-error">⚠ {t('providerError')}: {snapshot.providerError}</div>
      ) : null}

      {!snapshot.datasetReady || !s ? (
        <div className="blocked-banner">
          <h2>{t('noDataset')}</h2>
          <p>{snapshot.note}</p>
        </div>
      ) : (
        <>
          <nav className="tabs">
            {views.map((v) => (
              <button key={v} className={`tab ${view === v ? 'active' : ''}`} onClick={() => setView(v)}>
                {viewLabel[v][lang]}
              </button>
            ))}
            <span className="reasoner-flag">{t('reasoner')}: {snapshot.reasonerMode}</span>
          </nav>

          {view === 'overview' && (
            <>
              <section className="data-strip">
                <StatTile label={t('source')} value={s.source} />
                <StatTile label={t('window')} value={s.windowDays.join(' → ') || 'n/a'} />
                <StatTile label={t('failedLogins')} value={fmt(s.adminLoginFailed)} sub={`${s.distinctSrc} ${t('srcIps')} · ${s.lockouts} ${t('lockouts')}`} />
                <StatTile label={t('deniedFlows')} value={fmt(s.denyCount)} sub={`accept ${fmt(s.acceptPermit)}`} />
                <StatTile label={t('topPort')} value={s.topDenyPorts[0]?.[0] ?? 'n/a'} sub={s.topDenyPorts[0] ? `${fmt(s.topDenyPorts[0][1])} ${t('hits')}` : ''} />
              </section>
              <div className="overview-grid">
                <aside className="case-list">
                  <div className="panel-title">{t('cases')} ({snapshot.cases.length})</div>
                  {snapshot.cases.map((c) => (
                    <button key={c.id} className={`case-item ${c.id === activeCase?.id ? 'active' : ''}`} onClick={() => setActiveId(c.id)}>
                      <span className={`dot ${c.verifier.passed ? 'dot-ok' : 'dot-bad'}`} />
                      <span className="case-item-title">{rootCauseLabel(c.diagnosis.rootCauseKey, lang)}</span>
                    </button>
                  ))}
                </aside>
                <div className="stage">
                  {activeCase ? (
                    <>
                      <Verdict rcaCase={activeCase} lang={lang} />
                      <div className="panel-title">{t('pipeline')}</div>
                      <RcaFlow rcaCase={activeCase} lang={lang} />
                    </>
                  ) : null}
                </div>
              </div>
            </>
          )}

          {view === 'topology' && (
            <section className="full-panel">
              <div className="panel-title">{t('topology')}</div>
              <NetworkTopology stats={s} lang={lang} />
              <div className="legend">
                <span><i className="lg-red" /> {t('attackers')} → FortiGate</span>
                <span><i className="lg-amber" /> {t('internalHosts')} → {t('deniedPorts')}</span>
                <span><i className="lg-blue" /> FortiGate → {t('syslogSink')}</span>
                <span><i className="lg-green" /> → {t('consoleNode')}</span>
              </div>
            </section>
          )}

          {view === 'pipeline' && activeCase && (
            <section className="full-panel">
              <div className="case-tabs">
                {snapshot.cases.map((c) => (
                  <button key={c.id} className={`case-tab ${c.id === activeCase.id ? 'active' : ''}`} onClick={() => setActiveId(c.id)}>
                    {rootCauseLabel(c.diagnosis.rootCauseKey, lang)}
                  </button>
                ))}
              </div>
              <RcaFlow rcaCase={activeCase} lang={lang} />
              <Verdict rcaCase={activeCase} lang={lang} />
              <div className="trace-card">
                <div className="subsection-title">{t('inspect')} · {activeCase.trace.length}</div>
                <ol className="trace">
                  {activeCase.trace.map((ev, i) => (
                    <li key={i} className="trace-step">
                      <span className="trace-kind">{TRACE_LABELS[ev.kind]?.[lang] ?? ev.kind}</span>
                      <span className="trace-detail">
                        {ev.kind === 'skills_exposed' && Array.isArray(ev.payload.skills) ? (ev.payload.skills as string[]).join(' · ') : ''}
                        {ev.kind === 'tool_called' ? String(ev.payload.skill ?? '') : ''}
                        {ev.kind === 'diagnosis_completed' ? String(ev.payload.root_cause_key ?? '') : ''}
                      </span>
                    </li>
                  ))}
                </ol>
              </div>
            </section>
          )}

          {view === 'compare' && (
            <section className="charts">
              <AblationChart baselines={snapshot.baselines} lang={lang} />
              <DenyPortChart stats={s} lang={lang} />
            </section>
          )}
        </>
      )}
      <footer className="app-footer">{snapshot.note}</footer>
    </div>
  )
}

export default App
