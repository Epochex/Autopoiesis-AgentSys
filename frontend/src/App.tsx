import { useEffect, useMemo, useState } from 'react'
import './App.css'
import type { Baseline, RcaCase, RcaSnapshot, TraceEvent } from './types'

const SNAPSHOT_ENDPOINT = '/api/rca/snapshot'

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; snapshot: RcaSnapshot }

function num(n: number): string {
  return n.toLocaleString('en-US')
}

function pct(n: number): string {
  return `${Math.round(n * 100)}%`
}

const TRACE_LABELS: Record<string, string> = {
  alert_received: 'Alert received',
  memory_read: 'Memory retrieved',
  skills_exposed: 'Skills selected (top-k)',
  tool_called: 'Readonly skill executed',
  context_compiled: 'Context compiled',
  verifier_result: 'Verifier checked',
  cost_observed: 'Cost recorded',
  diagnosis_completed: 'Diagnosis produced',
}

function traceDetail(ev: TraceEvent): string {
  const p = ev.payload as Record<string, unknown>
  switch (ev.kind) {
    case 'skills_exposed':
      return Array.isArray(p.skills) ? (p.skills as string[]).join(', ') : ''
    case 'tool_called':
      return [p.skill, Array.isArray(p.evidence_ids) ? `→ ${(p.evidence_ids as string[]).join(', ')}` : '']
        .filter(Boolean)
        .join(' ')
    case 'verifier_result':
      return p.passed ? 'passed' : `failed: ${(p.errors as string[] | undefined)?.join('; ') ?? ''}`
    case 'cost_observed':
      return `${p.tool_calls ?? 0} tool calls, cost ${p.tool_cost ?? 0}`
    case 'diagnosis_completed':
      return String(p.root_cause_key ?? '')
    default:
      return ''
  }
}

function Header({ snapshot, onRefresh }: { snapshot: RcaSnapshot; onRefresh: () => void }) {
  const r = snapshot.readiness
  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-mark">selfevo</span>
        <span className="brand-sub">Network RCA Console · real R230 FortiGate held-out</span>
      </div>
      <div className="header-right">
        <span className={`pill ${r.blocked ? 'pill-bad' : 'pill-ok'}`}>
          {r.blocked ? 'dataset blocked' : 'real dataset live'}
        </span>
        <span className={`pill ${r.syslogPortOpen ? 'pill-ok' : 'pill-warn'}`}>
          R230 syslog {r.syslogPortOpen ? 'reachable' : 'down'}
        </span>
        <button className="btn" onClick={onRefresh}>
          Refresh
        </button>
      </div>
    </header>
  )
}

function DataStrip({ snapshot }: { snapshot: RcaSnapshot }) {
  const s = snapshot.dataStats
  if (!s) return null
  const items = [
    { label: 'Source', value: s.source, sub: '' },
    { label: 'Window', value: s.windowDays.join(' → ') || 'n/a', sub: '' },
    {
      label: 'Failed admin logins',
      value: num(s.adminLoginFailed),
      sub: `${s.distinctSrc} src IPs · ${s.lockouts} lockouts`,
    },
    { label: 'Denied flows', value: num(s.denyCount), sub: `accept/permit ${num(s.acceptPermit)}` },
    {
      label: 'Top denied port',
      value: s.topDenyPorts[0]?.[0] ?? 'n/a',
      sub: s.topDenyPorts[0] ? `${num(s.topDenyPorts[0][1])} hits` : '',
    },
  ]
  return (
    <section className="data-strip">
      {items.map((it) => (
        <div className="stat" key={it.label}>
          <div className="stat-label">{it.label}</div>
          <div className="stat-value">{it.value}</div>
          {it.sub ? <div className="stat-sub">{it.sub}</div> : null}
        </div>
      ))}
    </section>
  )
}

function CaseList({
  cases,
  activeId,
  onSelect,
}: {
  cases: RcaCase[]
  activeId: string
  onSelect: (id: string) => void
}) {
  return (
    <aside className="case-list">
      <div className="panel-title">Held-out cases ({cases.length})</div>
      {cases.map((c) => (
        <button
          key={c.id}
          className={`case-item ${c.id === activeId ? 'active' : ''}`}
          onClick={() => onSelect(c.id)}
        >
          <div className="case-item-title">{c.title}</div>
          <div className="case-item-meta">
            <span className={`dot ${c.verifier.passed ? 'dot-ok' : 'dot-bad'}`} />
            {c.diagnosis.rootCauseKey}
          </div>
        </button>
      ))}
    </aside>
  )
}

function CaseDetail({ rcaCase }: { rcaCase: RcaCase }) {
  const d = rcaCase.diagnosis
  return (
    <div className="case-detail">
      <div className="case-query">
        <span className="kicker">Incident query</span>
        <h2>{rcaCase.title}</h2>
        <p>{rcaCase.query}</p>
        <div className="asset-tags">
          {rcaCase.assets.map((a) => (
            <span className="tag" key={a}>
              {a}
            </span>
          ))}
        </div>
      </div>

      <div className="diagnosis-card">
        <div className="diagnosis-head">
          <span className="kicker">Diagnosis · {d.readonly ? 'readonly' : 'WRITE'}</span>
          <span className="confidence">confidence {d.confidence.toFixed(2)}</span>
        </div>
        <div className="root-cause-key">{d.rootCauseKey}</div>
        <p className="root-cause">{d.rootCause}</p>

        <div className="subsection-title">Cited evidence ({d.evidence.length})</div>
        <ul className="evidence-list">
          {d.evidence.map((e) => (
            <li key={e.evidenceId}>
              <code>{e.evidenceId}</code>
              <span className="evidence-summary">{e.summary}</span>
              <span className="evidence-source">{e.source}</span>
            </li>
          ))}
        </ul>

        <div className="subsection-title">Recommended actions</div>
        <ul className="action-list">
          {d.recommendedActions.map((a, i) => (
            <li key={i}>{a}</li>
          ))}
        </ul>
      </div>

      <div className="trace-card">
        <div className="subsection-title">
          Execution trace · {rcaCase.verifier.passed ? 'verifier passed' : 'verifier failed'}
        </div>
        <ol className="trace">
          {rcaCase.trace.map((ev, i) => (
            <li key={i} className="trace-step">
              <span className="trace-kind">{TRACE_LABELS[ev.kind] ?? ev.kind}</span>
              <span className="trace-detail">{traceDetail(ev)}</span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  )
}

function BaselineTable({ baselines }: { baselines: Baseline[] }) {
  if (!baselines.length) return null
  return (
    <section className="baseline">
      <div className="panel-title">
        Ablation on real held-out (rule reasoner — deterministic baseline)
      </div>
      <table>
        <thead>
          <tr>
            <th>configuration</th>
            <th>root-cause acc</th>
            <th>evidence recall</th>
            <th>cases</th>
            <th>note</th>
          </tr>
        </thead>
        <tbody>
          {baselines.map((b) => {
            const degraded = b.rootCauseAccuracy < 1
            return (
              <tr key={b.name} className={degraded ? 'row-degraded' : ''}>
                <td className="mono">{b.name}</td>
                <td className={degraded ? 'bad' : 'good'}>{pct(b.rootCauseAccuracy)}</td>
                <td className={b.evidenceRecall < 1 ? 'bad' : 'good'}>{pct(b.evidenceRecall)}</td>
                <td>{b.cases}</td>
                <td className="note">{b.notes}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="baseline-note">
        Informative signal: removing skill control (<code>full_tools</code>) lets the window&apos;s
        dominant brute-force evidence swamp the deny case → misdiagnosis. The 100% rows are a
        deterministic rule baseline on real data, not a proof of reasoning quality.
      </p>
    </section>
  )
}

function App() {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [activeId, setActiveId] = useState<string>('')

  const load = useMemo(
    () => async (refresh = false) => {
      setState({ status: 'loading' })
      try {
        const res = await fetch(`${SNAPSHOT_ENDPOINT}${refresh ? '?refresh=true' : ''}`, {
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
    void load()
  }, [load])

  if (state.status === 'loading') {
    return <div className="centered">Loading real RCA snapshot…</div>
  }
  if (state.status === 'error') {
    return (
      <div className="centered error">
        <p>Could not reach the RCA gateway ({state.message}).</p>
        <button className="btn" onClick={() => void load()}>
          Retry
        </button>
      </div>
    )
  }

  const { snapshot } = state
  const activeCase = snapshot.cases.find((c) => c.id === activeId) ?? snapshot.cases[0]

  return (
    <div className="app">
      <Header snapshot={snapshot} onRefresh={() => void load(true)} />
      {snapshot.datasetReady ? (
        <>
          <DataStrip snapshot={snapshot} />
          <main className="workbench">
            <CaseList cases={snapshot.cases} activeId={activeCase?.id ?? ''} onSelect={setActiveId} />
            {activeCase ? <CaseDetail rcaCase={activeCase} /> : <div className="centered">No cases.</div>}
          </main>
          <BaselineTable baselines={snapshot.baselines} />
        </>
      ) : (
        <div className="blocked-banner">
          <h2>Real held-out dataset not available</h2>
          <p>{snapshot.note}</p>
        </div>
      )}
      <footer className="app-footer">{snapshot.note}</footer>
    </div>
  )
}

export default App
