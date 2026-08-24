/* Landed situation records from the event-processing audit files.
 *
 * Sits above the separate offline trajectory replay. These two panels have
 * independent data sources and timestamps.
 *
 * GET /api/rca/live-situation supplies alerts, response suggestions, correlation
 * state, and the timestamp of the newest landed record.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { TheaterEvent } from '../types'
import { railFor, sentinelStageIds } from './netops-pipeline'
import './live-situation.css'

interface Stage { stageId: string; label: string; provider: string; ts: string; detail: string }
interface TPt { ts: string; label: string; kind: string }
interface Hypo { id: string; rank: number; statement: string; confidence: number; confidenceLabel: string; evidenceRefs: string[] }
interface Suggestion {
  id: string; ts: string; scope: string; severity: string; priority: string; summary: string
  service: string; device: string; deviceKey: string; clusterSize: number; adaptiveMode: string
  triggerReasons: string[]; impactLevel: string
  anchorIp?: string | null; originIp?: string | null
  timeline: TPt[]; stageTelemetry: Stage[]
  hypothesisSet: { setId: string; primaryHypothesisId: string; items: Hypo[]; summary: Record<string, number> }
  runbookDraft: {
    planId: string; title: string; planStatus: string; actions: string[]
    applicability: Record<string, string>
    approvalBoundary: { approvalRequired: boolean; disposition: string; reviewerApprovalFlag: boolean }
  }
  reviewVerdict: {
    verdictId: string; verdictStatus: string; recommendedDisposition: string
    checks: { overreachRisk: { status: string; approvalRequired: boolean } }
  }
}
interface FeedItem {
  id: string; kind: string; scope?: string; ts: string; severity?: string
  priority?: string; device?: string; deviceKey?: string; summary?: string; ruleId?: string; scenario?: string
}
interface ClusterWatch { key: string; severity: string; ruleId: string; progress: number; target: number; lastEmitTs: string }
export interface SituationSnapshot {
  ready: boolean; feed: FeedItem[]; clusterWatch: ClusterWatch[]; suggestions: Suggestion[]
  runtime: { latestAlertTs: string; latestSuggestionTs: string; windowSec: number }
  defaultSuggestionId: string
}

/** ISO → HH:MM:SS, in whatever zone the stamp carries. n/a stays n/a. */
const hms = (iso: string): string => {
  if (!iso || iso === 'n/a') return 'N/A'
  const m = iso.match(/T(\d{2}):(\d{2}):(\d{2})/)
  return m ? `${m[1]}:${m[2]}:${m[3]}` : iso
}
const ymd = (iso: string): string => {
  if (!iso || iso === 'n/a') return 'N/A'
  const m = iso.match(/(\d{4})-(\d{2})-(\d{2})/)
  return m ? `${m[1]}-${m[2]}-${m[3]}` : iso
}
/* Severity is carried by weight and structure. */
const sevRank = (s: string | undefined): number =>
  s === 'critical' ? 3 : s === 'major' || s === 'high' ? 2 : s === 'warning' || s === 'minor' ? 1 : 0

/** Which stages the selected incident actually lit, on whichever rail it ran. */
const hotStages = (s: Suggestion | null): Set<string> => {
  if (!s) return new Set()
  // A sentinel chain reports its own steps, so read them off the record rather
  // than inferring from scope the way the NetOps cards have to.
  if (s.scope === 'sentinel') return new Set(sentinelStageIds(s.timeline))
  return s.scope === 'cluster'
    ? new Set(['cluster-window', 'aiops-agent', 'suggestions-topic', 'remediation'])
    : new Set(['aiops-agent', 'suggestions-topic', 'remediation'])
}

/* feed item / selected suggestion → the theater event page 1 will play out */
const alertEvent = (f: FeedItem): TheaterEvent => ({
  kind: 'alert', id: f.id, ts: f.ts, device: f.deviceKey || f.device || '', deviceLabel: f.device,
  severity: f.severity,
  scenario: f.scenario, stageIds: ['correlator', 'alerts-topic', 'cluster-window'],
})
const suggestionEvent = (s: Suggestion): TheaterEvent => ({
  kind: 'suggestion', id: s.id, ts: s.ts, device: s.deviceKey || s.device, deviceLabel: s.device,
  severity: s.severity,
  priority: s.priority, summary: s.summary, scope: s.scope,
  anchorIp: s.anchorIp ?? undefined,
  originIp: s.originIp ?? undefined,
  blastScope: s.impactLevel,
  blastSummary: s.stageTelemetry.find((t) => t.stageId === 'preflight')?.detail,
  stageIds: s.scope === 'sentinel'
    ? sentinelStageIds(s.timeline)
    : s.scope === 'cluster'
      ? ['correlator', 'alerts-topic', 'cluster-window', 'aiops-agent', 'suggestions-topic', 'remediation']
      : ['aiops-agent', 'suggestions-topic', 'remediation'],
})

export function LiveSituation({ zh, onTheater, onTrace, scenario = 'disk', focusSubject }: { zh: boolean; onTheater?: (e: TheaterEvent) => void; onTrace?: (subject: string) => void; scenario?: 'disk' | 'bench'; focusSubject?: string }) {
  const [snap, setSnap] = useState<SituationSnapshot | null>(null)
  const [state, setState] = useState<'load' | 'ok' | 'empty' | 'err'>('load')
  // A manual pick remembers which focus request it was made under, so arriving
  // from a new alert supersedes it while a click made after arriving stays put.
  const [pick, setPick] = useState<{ id: string; under: string | null } | null>(null)
  const timer = useRef<number | undefined>(undefined)

  /* Poll the landed files for additions. A restarting
   * backend is survived by simply keeping the last good snapshot on error. */
  useEffect(() => {
    let gone = false
    const load = () => {
      fetch(`/api/rca/${scenario === 'bench' ? 'bench-live-situation' : 'live-situation'}?lang=${zh ? 'zh' : 'en'}`)
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
        .then((d: SituationSnapshot) => {
          if (gone) return
          setSnap(d)
          setState(d && d.ready ? 'ok' : 'empty')
        })
        .catch(() => { if (!gone) setState((s) => (s === 'load' ? 'err' : s)) })
    }
    load()
    // 8s, not 20s: a sentinel chain moves through six steps in about two
    // minutes, and a list that lags the incident by a third of it reads as broken.
    timer.current = window.setInterval(load, 8000)
    return () => { gone = true; if (timer.current) window.clearInterval(timer.current) }
  }, [zh, scenario])

  const suggestions = useMemo(() => snap?.suggestions ?? [], [snap])
  const feed = useMemo(() => snap?.feed ?? [], [snap])
  const selected = useMemo(() => {
    if (pick && pick.under === (focusSubject ?? null)) {
      const picked = suggestions.find((s) => s.id === pick.id)
      if (picked) return picked
    }
    if (focusSubject) {
      const focus = suggestions.find((s) => s.deviceKey === focusSubject || s.device === focusSubject)
      if (focus) return focus
    }
    return suggestions.find((s) => s.id === snap?.defaultSuggestionId) ?? suggestions[0] ?? null
  }, [suggestions, pick, focusSubject, snap])
  const hot = useMemo(() => hotStages(selected), [selected])
  const rail = useMemo(() => railFor(selected?.scope, selected?.timeline), [selected])
  const reportOnly = selected?.reviewVerdict.verdictStatus === 'reported'

  /* Arriving from the situational page's alert strip: bring the panel into view.
   * Which card is selected is derived above, not set here. */
  useEffect(() => {
    if (!focusSubject) return
    document.querySelector('.ls')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [focusSubject])

  if (state === 'load') return <section className="ls ls-msg">{zh ? '读取 NetOps 磁盘落地记录…' : 'READING NETOPS DISK SINKS…'}</section>
  if (state === 'err') return <section className="ls ls-msg err">{zh ? '磁盘态势端点不可达' : 'DISK-SINK ENDPOINT UNREACHABLE'}</section>
  if (state === 'empty' || !snap) return <section className="ls ls-msg">{zh ? '落地文件中无态势记录' : 'NO SITUATION RECORDS IN DISK SINKS'}</section>

  const latest = snap.runtime.latestSuggestionTs !== 'n/a' ? snap.runtime.latestSuggestionTs : snap.runtime.latestAlertTs

  return (
    <section className="ls" aria-label={zh ? '落地态势记录' : 'Landed situation records'}>
      <header className="ls-head">
        <div className="ls-head-l">
          <span className="ls-kick">{zh ? '事件处置 · 审计记录' : 'INCIDENT RESPONSE · AUDIT RECORDS'}</span>
          <h2 className="ls-title">{zh ? <>处置<mark>记录</mark></> : <>RESPONSE <mark>RECORDS</mark></>}</h2>
        </div>
        <div className="ls-head-r">
          <span className="ls-src">{zh ? '检测事实 + 候选动作 + 关联状态' : 'DETECTION FACTS + CANDIDATE ACTIONS + CORRELATION STATE'}</span>
          <span className="ls-stamp">{zh ? '最新记录' : 'LATEST RECORD'} · {ymd(latest)} {hms(latest)}</span>
          <span className="ls-counts">
            <b>{feed.filter((f) => f.kind === 'alert').length}</b> {zh ? '告警' : 'alerts'} · <b>{suggestions.length}</b> {zh ? '建议' : 'suggestions'}
          </span>
        </div>
      </header>

      {/* The processing rail for the selected incident.
          Two subsystems, two rails: a sentinel chain never enters the correlator. */}
      <div className="ls-pipe" role="list" aria-label={zh ? '处理链路' : 'Processing chain'}>
        <span className="ls-pipe-k">
          {selected?.scope === 'sentinel'
            ? reportOnly
              ? (zh ? '安全门决策流程' : 'SAFETY-GATE DECISION FLOW')
              : (zh ? '受控处置流程' : 'CONTROLLED RESPONSE FLOW')
            : (zh ? '事件处理流程' : 'EVENT PROCESSING FLOW')}
        </span>
        {rail.map((p, i) => (
          <div key={p.id} className={`ls-stage ${hot.has(p.id) ? 'hot' : ''}`} role="listitem">
            <span className="ls-stage-n">{String(i + 1).padStart(2, '0')}</span>
            <span className="ls-stage-l">{zh ? p.zh : p.en}</span>
            {i < rail.length - 1 && <span className="ls-stage-arm" aria-hidden="true" />}
          </div>
        ))}
      </div>

      <div className="ls-body">
        {/* left · landed records, newest first */}
        <aside className="ls-feed" aria-label={zh ? '落地事件记录' : 'Landed event records'}>
          <div className="ls-col-h">{zh ? '落地记录 · 新→旧' : 'LANDED RECORDS · NEW→OLD'}</div>
          <div className="ls-feed-list">
            {feed.map((f) => {
              const isSug = f.kind === 'suggestion'
              const on = isSug && selected?.id === `${f.id}`.replace('feed-suggestion-', '')
              return (
                <button
                  key={f.id}
                  className={`ls-fi ${f.kind} ${on ? 'on' : ''} ${!isSug && onTheater ? 'linkable' : ''}`}
                  disabled={!isSug && !onTheater}
                  title={!isSug && onTheater ? (zh ? '在全链路拓扑剧场中展开' : 'Open in the topology theater') : undefined}
                  onClick={() => (isSug
                    ? setPick({ id: `${f.id}`.replace('feed-suggestion-', ''), under: focusSubject ?? null })
                    : onTheater?.(alertEvent(f)))}
                >
                  <span className="ls-fi-top">
                    <span className={`ls-tag sev${sevRank(f.severity)}`}>{isSug ? f.priority || 'P?' : (zh ? '告警' : 'ALERT')}</span>
                    <span className="ls-fi-kind">{isSug ? (f.scope === 'cluster' ? (zh ? '簇建议' : 'CLUSTER') : (zh ? '单点建议' : 'SINGLE')) : (f.scenario || 'N/A')}</span>
                    <time className="ls-fi-ts">{hms(f.ts)}</time>
                  </span>
                  <span className="ls-fi-dev">{f.device || 'N/A'}</span>
                  {f.summary && <span className="ls-fi-sum">{f.summary}</span>}
                </button>
              )
            })}
          </div>
        </aside>

        {/* right · the selected suggestion's full diagnosis chain */}
        {selected ? (
          <div className="ls-detail">
            <div className="ls-d-head">
              <span className={`ls-tag sev${sevRank(selected.severity)}`}>{selected.priority}</span>
              <span className="ls-d-dev">{selected.device}</span>
              <span className="ls-d-svc">{selected.service}</span>
              <span className="ls-d-mode">{selected.adaptiveMode} · {selected.impactLevel}</span>
              {onTrace ? (
                <button
                  className="ls-theater-cta"
                  onClick={() => onTrace(selected.deviceKey || selected.device || selected.id)}
                >
                  {zh ? '看处置链路 ▸' : 'RESPONSE CHAIN ▸'}
                </button>
              ) : null}
              {onTheater ? (
                <button className="ls-theater-cta" onClick={() => onTheater(suggestionEvent(selected))}>
                  ⧉ {zh ? '全链路拓扑剧场' : 'TOPOLOGY THEATER'} ▸
                </button>
              ) : null}
            </div>
            <p className="ls-d-sum">{selected.summary}</p>

            {/* Recorded event and decision timeline. */}
            <div className="ls-block">
              <div className="ls-block-h">{zh ? '诊断时间线' : 'DIAGNOSIS TIMELINE'}</div>
              <ol className="ls-tl">
                {selected.timeline.map((t, i) => (
                  <li key={i} className={`ls-tl-i ${t.kind}`}>
                    <time>{hms(t.ts)}</time><span>{t.label}</span>
                  </li>
                ))}
              </ol>
            </div>

            {/* Per-stage observations and providers. */}
            <div className="ls-block">
              <div className="ls-block-h">{zh ? '检测与决策事实' : 'DETECTION & DECISION FACTS'}</div>
              <div className="ls-stages">
                {selected.stageTelemetry.map((s) => (
                  <div key={s.stageId} className="ls-st">
                    <span className="ls-st-id">{s.stageId}</span>
                    <span className="ls-st-detail">{s.detail || s.provider || 'N/A'}</span>
                    {s.provider && <span className="ls-st-prov">{s.provider}</span>}
                  </div>
                ))}
              </div>
            </div>

            {/* Ranked root-cause candidates with confidence and evidence references. */}
            <div className="ls-block">
              <div className="ls-block-h">
                {zh ? '根因候选' : 'ROOT-CAUSE CANDIDATES'} · <b>{selected.hypothesisSet.items.length}</b>
                {selected.hypothesisSet.summary.contradictory_ref_count != null && (
                  <span className="ls-block-sub">
                    {zh ? '支持' : 'supp'} {selected.hypothesisSet.summary.supporting_ref_count ?? 0} · {zh ? '反证' : 'contra'} {selected.hypothesisSet.summary.contradictory_ref_count ?? 0}
                  </span>
                )}
              </div>
              <ul className="ls-hypos">
                {selected.hypothesisSet.items.map((h) => (
                  <li key={h.id} className={`ls-hy ${h.id === selected.hypothesisSet.primaryHypothesisId ? 'primary' : ''}`}>
                    <span className="ls-hy-rank">#{h.rank}</span>
                    <span className="ls-hy-stmt">{h.statement}</span>
                    <span className="ls-hy-conf" title={h.confidenceLabel}>
                      <i style={{ width: `${Math.round(Math.max(0, Math.min(1, h.confidence)) * 100)}%` }} />
                      <em>{(h.confidence * 100).toFixed(0)}%</em>
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            {/* Candidate action, safety decision, and follow-up owner. */}
            <div className="ls-block ls-runbook">
              <div className="ls-block-h">
                {reportOnly
                  ? (zh ? '候选动作与安全门条件' : 'CANDIDATE ACTION & SAFETY GATE')
                  : (zh ? '候选处置方案' : 'CANDIDATE RESPONSE PLAN')}
              </div>
              <div className="ls-rb-title">{selected.runbookDraft.title || 'N/A'}</div>
              {selected.runbookDraft.actions.length > 0 && (
                <ol className="ls-rb-actions">
                  {selected.runbookDraft.actions.map((a, i) => <li key={i}>{a}</li>)}
                </ol>
              )}
              <div className="ls-gate">
                <span className="ls-gate-lock" aria-hidden="true" />
                <span className="ls-gate-t">
                  {reportOnly
                    ? (zh ? '决策结果 · 写操作未授权' : 'DECISION · WRITE NOT AUTHORIZED')
                    : (zh ? '审批状态 · 等待授权' : 'APPROVAL STATUS · AUTHORIZATION PENDING')}
                </span>
                <span className={`ls-gate-risk ${selected.reviewVerdict.checks.overreachRisk.status}`}>
                  {reportOnly
                    ? (zh ? '后续责任 · 安全运营核验并处置' : 'OWNER · SECURITY OPERATIONS VALIDATION')
                    : `${zh ? '安全门风险' : 'GATE RISK'} · ${selected.reviewVerdict.checks.overreachRisk.status}`}
                </span>
              </div>
            </div>
          </div>
        ) : (
          <div className="ls-detail ls-detail-empty">{zh ? '无建议可展开' : 'NO SUGGESTION TO EXPAND'}</div>
        )}
      </div>

      {/* Correlation windows progressing toward a cluster threshold. */}
      {snap.clusterWatch.length > 0 && (
        <div className="ls-clusters">
          <div className="ls-col-h">{zh ? '关联窗口' : 'CORRELATION WINDOWS'} · {snap.runtime.windowSec}s</div>
          <div className="ls-cw-list">
            {snap.clusterWatch.map((c, i) => (
              <div key={i} className="ls-cw">
                <span className={`ls-tag sev${sevRank(c.severity)}`}>{c.severity}</span>
                <span className="ls-cw-key">{c.key}</span>
                <span className="ls-cw-bar"><i style={{ width: `${Math.round((c.progress / Math.max(1, c.target)) * 100)}%` }} /></span>
                <span className="ls-cw-n">{c.progress}/{c.target}</span>
                <time className="ls-cw-ts">{hms(c.lastEmitTs)}</time>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}
