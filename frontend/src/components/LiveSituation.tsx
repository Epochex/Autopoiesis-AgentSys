/* Operator-facing live case ledger.
 *
 * This view consumes only detection facts and the durable business decision
 * attached to the same case id.  Draft hypotheses, provider names, confidence
 * bars and generated runbooks stay outside the primary incident surface.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { TheaterEvent } from '../types'
import './live-situation.css'

interface IncidentFacts {
  dataClassification?: string
  alertId?: string
  observedAt?: string
  sourceIp?: string
  sourcePort?: number | string
  destinationIp?: string
  destinationPort?: number | string
  protocol?: number | string
  service?: string
  action?: string
  trafficSubtype?: string
  policyId?: number | string
  policyType?: string
  sourceInterface?: string
  sourceInterfaceRole?: string
  destinationInterface?: string
  destinationInterfaceRole?: string
  denyCount?: number
  threshold?: number
  windowSeconds?: number
  recentSimilar1h?: number
  clusterSize?: number
}

interface DecisionEvidence {
  evidenceId: string
  label: string
  value: string
  source: string
  observedAt?: string | null
}

interface MissingObservation { code: string; question: string; probe?: string | null }

interface BusinessDecision {
  caseId: string
  sessionId: string
  state: 'investigating' | 'action_ready' | 'observing' | 'resolved' | 'escalated'
  classification: string
  headline: string
  summary: string
  disposition: string
  action: string
  service: string
  impactedAssets: string[]
  evidence: DecisionEvidence[]
  missingObservations: MissingObservation[]
  nextProbe?: string | null
  readback?: Record<string, unknown> | null
  generatedAt: string
}

interface Suggestion {
  id: string
  ts: string
  scope: string
  severity: string
  priority: string
  summary: string
  caseId?: string | null
  service: string
  device: string
  deviceKey: string
  clusterSize: number
  incidentFacts?: IncidentFacts
  caseDecision?: BusinessDecision | null
}

interface FeedItem {
  id: string
  kind: string
  ts: string
  severity?: string
  caseId?: string | null
  device?: string
  deviceKey?: string
  summary?: string
  incidentFacts?: IncidentFacts
}

export interface SituationSnapshot {
  ready: boolean
  feed: FeedItem[]
  suggestions: Suggestion[]
  runtime: { latestAlertTs: string; latestSuggestionTs: string; windowSec: number }
  defaultSuggestionId: string
}

const hms = (iso?: string | null): string => {
  if (!iso) return 'N/A'
  const match = iso.match(/T(\d{2}):(\d{2}):(\d{2})/)
  return match ? `${match[1]}:${match[2]}:${match[3]}` : iso
}

const ymd = (iso?: string | null): string => {
  if (!iso) return 'N/A'
  const match = iso.match(/(\d{4})-(\d{2})-(\d{2})/)
  return match ? `${match[1]}-${match[2]}-${match[3]}` : iso
}

const stateLabel = (state: BusinessDecision['state'] | undefined, zh: boolean): string => {
  const labels: Record<string, [string, string]> = {
    investigating: ['调查中', 'INVESTIGATING'],
    action_ready: ['动作就绪', 'ACTION READY'],
    observing: ['结果观察中', 'OBSERVING'],
    resolved: ['已形成结论', 'DECIDED'],
    escalated: ['需要升级', 'ESCALATED'],
  }
  const pair = labels[state ?? 'investigating'] ?? labels.investigating
  return zh ? pair[0] : pair[1]
}

const stateStep = (state?: BusinessDecision['state']): number => {
  if (state === 'resolved') return 4
  if (state === 'observing') return 4
  if (state === 'action_ready' || state === 'escalated') return 3
  return 2
}

const eventFor = (suggestion: Suggestion): TheaterEvent => ({
  kind: 'suggestion',
  id: suggestion.id,
  ts: suggestion.ts,
  device: suggestion.deviceKey || suggestion.device,
  deviceLabel: suggestion.device,
  severity: suggestion.severity,
  priority: suggestion.priority,
  summary: suggestion.caseDecision?.headline || suggestion.summary,
  scope: suggestion.scope,
  stageIds: ['correlator', 'alerts-topic', 'cluster-window'],
})

export function LiveSituation({
  zh,
  onTheater,
  onTrace,
  scenario = 'disk',
  focusSubject,
}: {
  zh: boolean
  onTheater?: (event: TheaterEvent) => void
  onTrace?: (subject: string, caseId?: string) => void
  scenario?: 'disk' | 'bench'
  focusSubject?: string
}) {
  const [snapshot, setSnapshot] = useState<SituationSnapshot | null>(null)
  const [state, setState] = useState<'load' | 'ok' | 'empty' | 'err'>('load')
  const [pickedId, setPickedId] = useState<string | null>(null)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => {
    let disposed = false
    const load = () => {
      fetch(`/api/rca/${scenario === 'bench' ? 'bench-live-situation' : 'live-situation'}?lang=${zh ? 'zh' : 'en'}`)
        .then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          return response.json()
        })
        .then((data: SituationSnapshot) => {
          if (disposed) return
          setSnapshot(data)
          setState(data?.ready ? 'ok' : 'empty')
        })
        .catch(() => { if (!disposed) setState((current) => current === 'load' ? 'err' : current) })
    }
    load()
    timer.current = window.setInterval(load, 8000)
    return () => {
      disposed = true
      if (timer.current) window.clearInterval(timer.current)
    }
  }, [scenario, zh])

  const suggestions = useMemo(
    () => (snapshot?.suggestions ?? []).filter((item) => item.incidentFacts?.dataClassification !== 'controlled_test'),
    [snapshot],
  )
  const selected = useMemo(() => {
    if (pickedId) {
      const picked = suggestions.find((item) => item.id === pickedId)
      if (picked) return picked
    }
    if (focusSubject) {
      const focused = suggestions.find((item) => item.deviceKey === focusSubject || item.device === focusSubject)
      if (focused) return focused
    }
    return suggestions.find((item) => item.id === snapshot?.defaultSuggestionId) ?? suggestions[0] ?? null
  }, [focusSubject, pickedId, snapshot, suggestions])

  useEffect(() => {
    if (focusSubject) document.querySelector('.ls')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [focusSubject])

  if (state === 'load') return <section className="ls ls-msg">{zh ? '读取实时案件…' : 'READING LIVE CASES…'}</section>
  if (state === 'err') return <section className="ls ls-msg err">{zh ? '实时案件接口不可达' : 'LIVE CASE ENDPOINT UNREACHABLE'}</section>
  if (state === 'empty' || !snapshot) return <section className="ls ls-msg">{zh ? '当前没有实时案件' : 'NO LIVE CASES'}</section>

  const latest = snapshot.runtime.latestSuggestionTs || snapshot.runtime.latestAlertTs
  const resolved = suggestions.filter((item) => item.caseDecision?.state === 'resolved').length
  const investigating = suggestions.filter((item) => !item.caseDecision || item.caseDecision.state === 'investigating').length
  const activeStep = stateStep(selected?.caseDecision?.state)
  const facts = selected?.incidentFacts ?? {}
  const decision = selected?.caseDecision ?? null

  const openCase = () => {
    if (!selected) return
    const jump = () => onTrace?.(selected.deviceKey || selected.device || selected.id, selected.caseId ?? undefined)
    if (!selected.caseId || scenario === 'bench') { jump(); return }
    fetch(`/api/rca/investigation-cases/${encodeURIComponent(selected.caseId)}/open`, { method: 'POST' }).finally(jump)
  }

  return (
    <section className="ls ls-business" aria-label={zh ? '实时案件决策' : 'Live case decisions'}>
      <header className="ls-head">
        <div className="ls-head-l">
          <span className="ls-kick">{zh ? '01 · 实时案件 · 业务结果' : '01 · LIVE CASES · BUSINESS OUTCOMES'}</span>
          <h2 className="ls-title">{zh ? <>事件<mark>处置台</mark></> : <>INCIDENT <mark>DECISIONS</mark></>}</h2>
        </div>
        <div className="ls-head-r">
          <span className="ls-src">{zh ? '检测事实 → 调查 → 决定 → 结果回读' : 'DETECTION → INVESTIGATION → DECISION → READBACK'}</span>
          <span className="ls-stamp">{ymd(latest)} {hms(latest)}</span>
          <span className="ls-counts"><b>{suggestions.length}</b> {zh ? '案件' : 'cases'} · <b>{resolved}</b> {zh ? '已决定' : 'decided'} · <b>{investigating}</b> {zh ? '调查中' : 'open'}</span>
        </div>
      </header>

      <div className="ls-decision-rail" aria-label={zh ? '案件状态' : 'Case state'}>
        {[
          [zh ? '检测事实' : 'DETECTION', selected?.summary || ''],
          [zh ? '案件调查' : 'INVESTIGATION', decision?.classification || (zh ? '等待决策' : 'PENDING')],
          [zh ? '处置决定' : 'DECISION', decision?.action || (zh ? '未形成' : 'NOT READY')],
          [zh ? '结果回读' : 'READBACK', String(decision?.readback?.outcome ?? (decision?.state === 'resolved' ? (zh ? '无需变更' : 'NO CHANGE') : (zh ? '等待' : 'PENDING')))],
        ].map(([label, value], index) => (
          <div className={`ls-decision-step ${index + 1 <= activeStep ? 'is-done' : ''}`} key={label}>
            <span>{String(index + 1).padStart(2, '0')}</span>
            <small>{label}</small>
            <b>{value || 'N/A'}</b>
          </div>
        ))}
      </div>

      <div className="ls-business-body">
        <aside className="ls-case-ledger" aria-label={zh ? '案件列表' : 'Case list'}>
          <div className="ls-col-h">{zh ? '案件 · 新到旧' : 'CASES · NEWEST FIRST'}</div>
          {suggestions.length ? suggestions.map((item) => (
            <button
              type="button"
              key={item.id}
              className={`ls-case-row ${item.id === selected?.id ? 'is-active' : ''}`}
              onClick={() => setPickedId(item.id)}
            >
              <time>{hms(item.ts)}</time>
              <span className={`ls-case-state is-${item.caseDecision?.state ?? 'investigating'}`}>{stateLabel(item.caseDecision?.state, zh)}</span>
              <strong>{item.caseDecision?.headline || item.summary}</strong>
              <code>{item.caseId || item.id}</code>
            </button>
          )) : <p className="ls-case-empty">{zh ? '当前没有观测案件' : 'NO OBSERVED CASES'}</p>}
        </aside>

        <article className="ls-business-decision">
          {selected ? (
            <>
              <div className="ls-business-title">
                <span className={`ls-case-state is-${decision?.state ?? 'investigating'}`}>{stateLabel(decision?.state, zh)}</span>
                <div>
                  <h3>{decision?.headline || (zh ? '检测已落案，调查尚未形成结论' : 'DETECTED; DECISION PENDING')}</h3>
                  <p>{decision?.summary || selected.summary}</p>
                </div>
                {selected.caseId ? <code>{selected.caseId}</code> : null}
              </div>

              <dl className="ls-fact-line">
                <div><dt>{zh ? '来源' : 'SOURCE'}</dt><dd>{facts.sourceIp || selected.device}</dd></div>
                <div><dt>{zh ? '目标' : 'TARGET'}</dt><dd>{facts.destinationIp || 'N/A'}{facts.destinationPort ? `:${facts.destinationPort}` : ''}</dd></div>
                <div><dt>{zh ? '服务' : 'SERVICE'}</dt><dd>{facts.service || selected.service || 'N/A'}</dd></div>
                <div><dt>{zh ? '设备结果' : 'DEVICE RESULT'}</dt><dd>{facts.action || 'N/A'}{facts.policyType ? ` · ${facts.policyType}` : ''}</dd></div>
              </dl>

              {decision ? (
                <>
                  <section className="ls-outcome-line">
                    <div><span>{zh ? '处置决定' : 'DISPOSITION'}</span><p>{decision.disposition}</p></div>
                    <div><span>{zh ? '执行动作' : 'ACTION'}</span><p>{decision.action}</p></div>
                    <div><span>{zh ? '影响对象' : 'AFFECTED ASSETS'}</span><p>{decision.impactedAssets.join(' · ') || (zh ? '未发现受影响业务资产' : 'NO AFFECTED BUSINESS ASSET FOUND')}</p></div>
                  </section>

                  <section className="ls-evidence-lines">
                    <h4>{zh ? '关键证据' : 'DECISIVE EVIDENCE'}</h4>
                    {decision.evidence.slice(0, 3).map((item) => (
                      <div key={`${item.evidenceId}-${item.label}`}>
                        <code>{item.evidenceId}</code><b>{item.label}</b><span>{item.value}</span>
                      </div>
                    ))}
                  </section>

                  {decision.missingObservations.length ? (
                    <section className="ls-missing-lines">
                      <h4>{zh ? '阻止结案的缺失观察' : 'MISSING OBSERVATIONS BLOCKING CLOSURE'}</h4>
                      {decision.missingObservations.map((item) => (
                        <div key={item.code}><b>{item.question}</b>{item.probe ? <code>{item.probe}</code> : null}</div>
                      ))}
                    </section>
                  ) : null}
                </>
              ) : (
                <p className="ls-pending-copy">{zh ? '该记录当前只有检测事实。系统还没有产出可用于关闭、处置或升级的案件决定。' : 'This record currently has detection facts only. No close, act, or escalate decision exists yet.'}</p>
              )}

              <div className="ls-business-actions">
                {onTrace ? <button type="button" onClick={openCase}>{zh ? '查看完整证据 ▸' : 'OPEN EVIDENCE ▸'}</button> : null}
                {onTheater ? <button type="button" onClick={() => onTheater(eventFor(selected))}>{zh ? '查看网络位置 ▸' : 'OPEN NETWORK POSITION ▸'}</button> : null}
                <details>
                  <summary>{zh ? '查看原始检测字段' : 'RAW DETECTION FIELDS'}</summary>
                  <pre>{JSON.stringify(facts, null, 2)}</pre>
                </details>
              </div>
            </>
          ) : <p>{zh ? '没有可展开案件' : 'NO CASE TO OPEN'}</p>}
        </article>
      </div>
    </section>
  )
}
