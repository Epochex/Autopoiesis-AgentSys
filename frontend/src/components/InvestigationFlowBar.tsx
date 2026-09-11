import type { Lang } from '../i18n'
import {
  INVESTIGATION_INTENTS,
  adjacentIntent,
  type InvestigationContext,
  type InvestigationIntent,
} from './investigation-context'
import './investigation-flow.css'
import { InvestigationTrajectorySignal } from './InvestigationTrajectorySignal'

/* The five steps are the incident's own story, in the order it happened:
 * what landed, the chain playing on the topology, what the investigation
 * read, what was done and read back, and what memory it drew on and wrote. */
const LABELS: Record<InvestigationIntent, [string, string]> = {
  event_facts: ['案件事实', 'CASE FACTS'],
  impact_scope: ['全链路剧场', 'FULL-CHAIN THEATER'],
  retrieval_evidence: ['调查依据', 'SOURCES USED'],
  evidence_hypotheses: ['证据与假设', 'EVIDENCE & HYPOTHESES'],
  remediation_readback: ['处置与回读', 'ACTION & READBACK'],
  related_memory: ['记忆观测舱', 'MEMORY OBSERVATORY'],
}

const clock = (value: string | null, lang: Lang): string => {
  if (!value) return lang === 'zh' ? '时间待采集' : 'TIME PENDING'
  return new Date(value).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
}

export function InvestigationFlowBar({
  context,
  lang,
  onNavigate,
  onClose,
}: {
  context: InvestigationContext
  lang: Lang
  onNavigate: (intent: InvestigationIntent) => void
  onClose: () => void
}) {
  const zh = lang === 'zh'
  const previous = adjacentIntent(context.currentIntent, -1)
  const next = adjacentIntent(context.currentIntent, 1)
  const visited = new Set(context.visitedViews.map((visit) => visit.intent))
  const source = context.sourceEvent.id
    ? context.sourceEvent.id.length > 20 ? `${context.sourceEvent.id.slice(0, 9)}…${context.sourceEvent.id.slice(-6)}` : context.sourceEvent.id
    : (zh ? '源事件编号待采集' : 'SOURCE EVENT ID PENDING')

  return (
    <aside className="ifb" aria-label={zh ? '当前事件调查路径' : 'Current investigation path'}>
      <div className="ifb-context">
        <span>{zh ? '调查对象' : 'INVESTIGATION TARGET'}</span>
        <strong>{context.subject}</strong>
        <code>{context.caseId ?? (zh ? '案件编号待生成' : 'CASE ID PENDING')}</code>
        <small>{zh ? '源事件' : 'SOURCE'} {source}</small>
        <small>{clock(context.timeWindow.start, lang)} → {clock(context.timeWindow.end, lang)}</small>
      </div>
      <div className="ifb-steps" role="navigation" aria-label={zh ? '调查步骤' : 'Investigation steps'}>
        {INVESTIGATION_INTENTS.map((intent, index) => (
          <button
            type="button"
            key={intent}
            className={`${intent === context.currentIntent ? 'is-current' : ''}${visited.has(intent) ? ' is-visited' : ''}`}
            aria-current={intent === context.currentIntent ? 'step' : undefined}
            onClick={() => onNavigate(intent)}
          >
            <span>{String(index + 1).padStart(2, '0')}</span>
            <b>{LABELS[intent][zh ? 0 : 1]}</b>
          </button>
        ))}
      </div>
      <div className="ifb-actions">
        <button type="button" disabled={!previous} onClick={() => previous && onNavigate(previous)}>
          {zh ? '← 上一步' : '← PREVIOUS'}
        </button>
        {next ? (
          <button type="button" className="is-next" onClick={() => onNavigate(next)}>
            {zh ? `下一步：${LABELS[next][0]} →` : `NEXT: ${LABELS[next][1]} →`}
          </button>
        ) : null}
        <button type="button" className="ifb-close" onClick={onClose}>{zh ? '结束本次调查' : 'END INVESTIGATION'}</button>
      </div>
      <InvestigationTrajectorySignal caseId={context.caseId} lang={lang} />
    </aside>
  )
}
