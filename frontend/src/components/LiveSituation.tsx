/* 实时态势 / LIVE SITUATION — the NetOps real-time subsystem, read-only.
 *
 * Sits above the long-trajectory replay: the top band is the live incident
 * pipeline as it stands right now, the board below is the same store's history
 * learned over time. Real-time diagnosis vs. historical learning, one screen.
 *
 * Every value comes from GET /api/rca/live-situation, which tails the NetOps
 * disk sinks (alerts + AIOps suggestions + cluster-state). Nothing is synthesized
 * here, and the panel states the real timestamp of the data it is showing rather
 * than pretending the stream is live at this instant. The two subsystems never
 * share a process — they meet only at that read-only file boundary.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { TheaterEvent } from '../types'
import { PIPELINE } from './netops-pipeline'
import './live-situation.css'

interface Stage { stageId: string; label: string; provider: string; ts: string; detail: string }
interface TPt { ts: string; label: string; kind: string }
interface Hypo { id: string; rank: number; statement: string; confidence: number; confidenceLabel: string; evidenceRefs: string[] }
interface Suggestion {
  id: string; ts: string; scope: string; severity: string; priority: string; summary: string
  service: string; device: string; clusterSize: number; adaptiveMode: string
  triggerReasons: string[]; impactLevel: string
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
  priority?: string; device?: string; summary?: string; ruleId?: string; scenario?: string
}
interface ClusterWatch { key: string; severity: string; ruleId: string; progress: number; target: number; lastEmitTs: string }
export interface LiveSnapshot {
  ready: boolean; feed: FeedItem[]; clusterWatch: ClusterWatch[]; suggestions: Suggestion[]
  runtime: { latestAlertTs: string; latestSuggestionTs: string; windowSec: number }
  defaultSuggestionId: string
}

/** ISO → HH:MM:SS, in whatever zone the stamp carries. n/a stays n/a. */
const hms = (iso: string): string => {
  if (!iso || iso === 'n/a') return '—'
  const m = iso.match(/T(\d{2}):(\d{2}):(\d{2})/)
  return m ? `${m[1]}:${m[2]}:${m[3]}` : iso
}
const ymd = (iso: string): string => {
  if (!iso || iso === 'n/a') return '—'
  const m = iso.match(/(\d{4})-(\d{2})-(\d{2})/)
  return m ? `${m[1]}-${m[2]}-${m[3]}` : iso
}
/* severity carried by weight/structure, never hue — the page allows no second accent */
const sevRank = (s: string | undefined): number =>
  s === 'critical' ? 3 : s === 'major' || s === 'high' ? 2 : s === 'warning' || s === 'minor' ? 1 : 0

/** Which pipeline stages a suggestion's own scope implies are "hot" right now. */
const hotStages = (s: Suggestion | null): Set<string> => {
  if (!s) return new Set()
  return s.scope === 'cluster'
    ? new Set(['cluster-window', 'aiops-agent', 'suggestions-topic', 'remediation'])
    : new Set(['aiops-agent', 'suggestions-topic', 'remediation'])
}

/* feed item / selected suggestion → the theater event page 1 will play out */
const alertEvent = (f: FeedItem): TheaterEvent => ({
  kind: 'alert', id: f.id, ts: f.ts, device: f.device || '', severity: f.severity,
  scenario: f.scenario, stageIds: ['correlator', 'alerts-topic', 'cluster-window'],
})
const suggestionEvent = (s: Suggestion): TheaterEvent => ({
  kind: 'suggestion', id: s.id, ts: s.ts, device: s.device, severity: s.severity,
  priority: s.priority, summary: s.summary, scope: s.scope,
  stageIds: s.scope === 'cluster'
    ? ['correlator', 'alerts-topic', 'cluster-window', 'aiops-agent', 'suggestions-topic', 'remediation']
    : ['aiops-agent', 'suggestions-topic', 'remediation'],
})

export function LiveSituation({ zh, onTheater }: { zh: boolean; onTheater?: (e: TheaterEvent) => void }) {
  const [snap, setSnap] = useState<LiveSnapshot | null>(null)
  const [state, setState] = useState<'load' | 'ok' | 'empty' | 'err'>('load')
  const [selId, setSelId] = useState<string | null>(null)
  const timer = useRef<number | undefined>(undefined)

  /* Poll: the panel is a live tail, so it re-reads on an interval. A restarting
   * backend is survived by simply keeping the last good snapshot on error. */
  useEffect(() => {
    let gone = false
    const load = () => {
      fetch('/api/rca/live-situation')
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
        .then((d: LiveSnapshot) => {
          if (gone) return
          setSnap(d)
          setState(d && d.ready ? 'ok' : 'empty')
        })
        .catch(() => { if (!gone) setState((s) => (s === 'load' ? 'err' : s)) })
    }
    load()
    timer.current = window.setInterval(load, 20000)
    return () => { gone = true; if (timer.current) window.clearInterval(timer.current) }
  }, [])

  const suggestions = useMemo(() => snap?.suggestions ?? [], [snap])
  const feed = useMemo(() => snap?.feed ?? [], [snap])
  const selected = useMemo(
    () => suggestions.find((s) => s.id === (selId ?? snap?.defaultSuggestionId)) ?? suggestions[0] ?? null,
    [suggestions, selId, snap],
  )
  const hot = useMemo(() => hotStages(selected), [selected])

  if (state === 'load') return <section className="ls ls-msg">{zh ? '接入 NetOps 实时流…' : 'CONNECTING TO NETOPS STREAM…'}</section>
  if (state === 'err') return <section className="ls ls-msg err">{zh ? '实时子系统不可达' : 'LIVE SUBSYSTEM UNREACHABLE'}</section>
  if (state === 'empty' || !snap) return <section className="ls ls-msg">{zh ? '当前无落地的实时态势' : 'NO LANDED LIVE SITUATION'}</section>

  const latest = snap.runtime.latestSuggestionTs !== 'n/a' ? snap.runtime.latestSuggestionTs : snap.runtime.latestAlertTs

  return (
    <section className="ls" aria-label={zh ? '实时态势' : 'Live situation'}>
      <header className="ls-head">
        <div className="ls-head-l">
          <span className="ls-kick">{zh ? '实时态势 · 内网流处理' : 'LIVE SITUATION · STREAM PROCESSING'}</span>
          <h2 className="ls-title">{zh ? <>实时<mark>态势</mark></> : <>LIVE <mark>SITUATION</mark></>}</h2>
        </div>
        <div className="ls-head-r">
          <span className="ls-src">Redpanda · NetOps</span>
          <span className="ls-stamp">{zh ? '最新落地' : 'LATEST'} · {ymd(latest)} {hms(latest)}</span>
          <span className="ls-counts">
            <b>{feed.filter((f) => f.kind === 'alert').length}</b> {zh ? '告警' : 'alerts'} · <b>{suggestions.length}</b> {zh ? '建议' : 'suggestions'}
          </span>
        </div>
      </header>

      {/* the fixed pipeline — the stages the selected incident lit up read as ink */}
      <div className="ls-pipe" role="list" aria-label={zh ? '流处理管线' : 'Stream pipeline'}>
        {PIPELINE.map((p, i) => (
          <div key={p.id} className={`ls-stage ${hot.has(p.id) ? 'hot' : ''}`} role="listitem">
            <span className="ls-stage-n">{String(i + 1).padStart(2, '0')}</span>
            <span className="ls-stage-l">{zh ? p.zh : p.en}</span>
            {i < PIPELINE.length - 1 && <span className="ls-stage-arm" aria-hidden="true" />}
          </div>
        ))}
      </div>

      <div className="ls-body">
        {/* left · the live feed, newest first */}
        <aside className="ls-feed" aria-label={zh ? '实时事件流' : 'Live feed'}>
          <div className="ls-col-h">{zh ? '事件流 · 新→旧' : 'FEED · NEW→OLD'}</div>
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
                  onClick={() => (isSug ? setSelId(`${f.id}`.replace('feed-suggestion-', '')) : onTheater?.(alertEvent(f)))}
                >
                  <span className="ls-fi-top">
                    <span className={`ls-tag sev${sevRank(f.severity)}`}>{isSug ? f.priority || 'P?' : (zh ? '告警' : 'ALERT')}</span>
                    <span className="ls-fi-kind">{isSug ? (f.scope === 'cluster' ? (zh ? '簇建议' : 'CLUSTER') : (zh ? '单点建议' : 'SINGLE')) : (f.scenario || '—')}</span>
                    <time className="ls-fi-ts">{hms(f.ts)}</time>
                  </span>
                  <span className="ls-fi-dev">{f.device || '—'}</span>
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
              {onTheater ? (
                <button className="ls-theater-cta" onClick={() => onTheater(suggestionEvent(selected))}>
                  ⧉ {zh ? '全链路拓扑剧场' : 'TOPOLOGY THEATER'} ▸
                </button>
              ) : null}
            </div>
            <p className="ls-d-sum">{selected.summary}</p>

            {/* real timeline: alert → inference → suggestion → critique → runbook */}
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

            {/* per-stage telemetry — the provider + detail each stage actually ran */}
            <div className="ls-block">
              <div className="ls-block-h">{zh ? '各阶段遥测' : 'STAGE TELEMETRY'}</div>
              <div className="ls-stages">
                {selected.stageTelemetry.map((s) => (
                  <div key={s.stageId} className="ls-st">
                    <span className="ls-st-id">{s.stageId}</span>
                    <span className="ls-st-detail">{s.detail || s.provider || '—'}</span>
                    {s.provider && <span className="ls-st-prov">{s.provider}</span>}
                  </div>
                ))}
              </div>
            </div>

            {/* hypothesis set: ranked, with real confidence and evidence refs */}
            <div className="ls-block">
              <div className="ls-block-h">
                {zh ? '假设集' : 'HYPOTHESES'} · <b>{selected.hypothesisSet.items.length}</b>
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

            {/* runbook draft + the approval boundary it can never cross on its own */}
            <div className="ls-block ls-runbook">
              <div className="ls-block-h">{zh ? '处置预案 · 草案' : 'RUNBOOK · DRAFT'}</div>
              <div className="ls-rb-title">{selected.runbookDraft.title || '—'}</div>
              {selected.runbookDraft.actions.length > 0 && (
                <ol className="ls-rb-actions">
                  {selected.runbookDraft.actions.map((a, i) => <li key={i}>{a}</li>)}
                </ol>
              )}
              <div className="ls-gate">
                <span className="ls-gate-lock" aria-hidden="true" />
                <span className="ls-gate-t">
                  {zh ? '审批边界 · 预案永不自动执行' : 'APPROVAL BOUNDARY · NEVER AUTO-EXECUTED'}
                </span>
                <span className={`ls-gate-risk ${selected.reviewVerdict.checks.overreachRisk.status}`}>
                  {zh ? '越权风险' : 'OVERREACH'} · {selected.reviewVerdict.checks.overreachRisk.status}
                </span>
              </div>
            </div>
          </div>
        ) : (
          <div className="ls-detail ls-detail-empty">{zh ? '无建议可展开' : 'NO SUGGESTION TO EXPAND'}</div>
        )}
      </div>

      {/* cluster watch — the correlation windows filling toward a cluster */}
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
