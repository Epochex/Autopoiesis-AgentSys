import { useCallback, useEffect, useState } from 'react'
import './App.css'
import type { RcaCase, RcaSnapshot } from './types'
import { makeT, rootCauseLabel, type Lang } from './i18n'
import { RcaFlow } from './components/RcaFlow'
import { AblationChart, DenyPortChart } from './components/Charts'
import { ProviderSwitch } from './components/ProviderSwitch'

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; snapshot: RcaSnapshot }

const fmt = (n: number) => n.toLocaleString('en-US')

function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub ? <div className="stat-sub">{sub}</div> : null}
    </div>
  )
}

function App() {
  const [lang, setLang] = useState<Lang>('zh')
  const [provider, setProvider] = useState('rule')
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [activeId, setActiveId] = useState('')
  const t = makeT(lang)

  const load = useCallback(
    async (prov: string, refresh = false) => {
      setState({ status: 'loading' })
      try {
        const res = await fetch(`/api/rca/snapshot?provider=${prov}${refresh ? '&refresh=true' : ''}`, {
          headers: { Accept: 'application/json' },
        })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const snapshot = (await res.json()) as RcaSnapshot
        setState({ status: 'ready', snapshot })
        setActiveId((prev) =>
          snapshot.cases.some((c) => c.id === prev) ? prev : snapshot.cases[0]?.id ?? '',
        )
      } catch (err) {
        setState({ status: 'error', message: err instanceof Error ? err.message : String(err) })
      }
    },
    [],
  )

  useEffect(() => {
    void load(provider)
  }, [provider, load])

  if (state.status === 'loading') {
    return <div className="centered">{t('refresh')}…</div>
  }
  if (state.status === 'error') {
    return (
      <div className="centered error">
        <p>gateway: {state.message}</p>
        <button className="btn" onClick={() => void load(provider)}>
          {t('refresh')}
        </button>
      </div>
    )
  }

  const { snapshot } = state
  const r = snapshot.readiness
  const s = snapshot.dataStats
  const activeCase: RcaCase | undefined =
    snapshot.cases.find((c) => c.id === activeId) ?? snapshot.cases[0]

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">selfevo</span>
          <span className="brand-sub">{t('brandSub')}</span>
        </div>
        <div className="header-right">
          <ProviderSwitch
            providers={snapshot.providers}
            active={snapshot.provider}
            lang={lang}
            onChange={setProvider}
          />
          <div className="lang-toggle">
            <button className={lang === 'zh' ? 'active' : ''} onClick={() => setLang('zh')}>
              中
            </button>
            <button className={lang === 'en' ? 'active' : ''} onClick={() => setLang('en')}>
              EN
            </button>
          </div>
          <span className={`pill ${r.blocked ? 'pill-bad' : 'pill-ok'}`}>
            {r.blocked ? t('blocked') : t('live')}
          </span>
          <button className="btn" onClick={() => void load(provider, true)}>
            ↻
          </button>
        </div>
      </header>

      {snapshot.providerError ? (
        <div className="provider-error">⚠ {t('providerError')}: {snapshot.providerError}</div>
      ) : null}

      {snapshot.datasetReady && s ? (
        <>
          <section className="data-strip">
            <StatTile label={t('source')} value={s.source} />
            <StatTile label={t('window')} value={s.windowDays.join(' → ') || 'n/a'} />
            <StatTile
              label={t('failedLogins')}
              value={fmt(s.adminLoginFailed)}
              sub={`${s.distinctSrc} ${t('srcIps')} · ${s.lockouts} ${t('lockouts')}`}
            />
            <StatTile label={t('deniedFlows')} value={fmt(s.denyCount)} sub={`accept ${fmt(s.acceptPermit)}`} />
            <StatTile
              label={t('topPort')}
              value={s.topDenyPorts[0]?.[0] ?? 'n/a'}
              sub={s.topDenyPorts[0] ? `${fmt(s.topDenyPorts[0][1])} ${t('hits')}` : ''}
            />
          </section>

          <main className="workbench">
            <aside className="case-list">
              <div className="panel-title">{t('cases')} ({snapshot.cases.length})</div>
              {snapshot.cases.map((c) => (
                <button
                  key={c.id}
                  className={`case-item ${c.id === activeCase?.id ? 'active' : ''}`}
                  onClick={() => setActiveId(c.id)}
                >
                  <span className={`dot ${c.verifier.passed ? 'dot-ok' : 'dot-bad'}`} />
                  <span className="case-item-title">{rootCauseLabel(c.diagnosis.rootCauseKey, lang)}</span>
                </button>
              ))}
            </aside>

            <div className="stage">
              {activeCase ? (
                <>
                  <div className="panel-title">{t('pipeline')}</div>
                  <RcaFlow rcaCase={activeCase} lang={lang} />

                  <div className="verdict">
                    <div className="verdict-main">
                      <span className="kicker">
                        {t('diagnosis')} · {activeCase.diagnosis.readonly ? t('readonly') : 'WRITE'} ·{' '}
                        {activeCase.verifier.passed ? t('verifierPassed') : t('verifierFailed')}
                      </span>
                      <div className="verdict-title">
                        {rootCauseLabel(activeCase.diagnosis.rootCauseKey, lang)}
                      </div>
                    </div>
                    <div className="gauge">
                      <div className="gauge-num">{activeCase.diagnosis.confidence.toFixed(2)}</div>
                      <div className="gauge-label">{t('confidence')}</div>
                    </div>
                  </div>

                  <div className="evidence-chips">
                    <span className="chip-label">{t('evidence')}</span>
                    {activeCase.diagnosis.evidence.map((e) => (
                      <span className="evidence-chip" key={e.evidenceId} title={e.source}>
                        <code>{e.evidenceId}</code>
                        {e.summary}
                      </span>
                    ))}
                  </div>
                </>
              ) : null}
            </div>
          </main>

          <section className="charts">
            <AblationChart baselines={snapshot.baselines} lang={lang} />
            <DenyPortChart stats={s} lang={lang} />
          </section>
        </>
      ) : (
        <div className="blocked-banner">
          <h2>{t('noDataset')}</h2>
          <p>{snapshot.note}</p>
        </div>
      )}
    </div>
  )
}

export default App
