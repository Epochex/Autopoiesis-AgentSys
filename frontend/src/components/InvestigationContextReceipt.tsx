import { useMemo } from 'react'
import type { Lang } from '../i18n'

export interface ProbePriorReceipt {
  preferred?: string[]
  memory_ids?: string[]
  root_key?: string | null
  procedural_confidence?: number
  strictly_narrowed?: boolean
  considered?: {
    memory_id: string
    tier: string
    staleness: number
    effective_confidence: number
    influenced_order: boolean
  }[]
}

export interface KnowledgeReceipt {
  document_id: string
  title: string
  source: string
  locator: string
  text: string
  route: string
  score: number
  matched_terms: string[]
}

export interface ContextTraceEvent {
  kind: string
  at: string
  payload: Record<string, unknown>
  persistence_error?: string
}

export interface RetrievalReceipt {
  kind: string
  item_id: string
  title?: string
  summary: string
  source: string
  locator: string
  route: string | string[]
  score: number
  matched_on?: string[]
  selected_for_context: boolean
  context_rank?: number
  selection_reasons?: string[]
  drop_reasons?: string[]
}

export interface InvestigationReceipt {
  probe_candidates: string[]
  probe_prior: ProbePriorReceipt
  historical_context: Record<string, unknown>
  knowledge_context: KnowledgeReceipt[]
  retrieval_results: RetrievalReceipt[]
  trace_events: ContextTraceEvent[]
}

const text = (zh: boolean) => ({
  title: zh ? '检索候选' : 'RETRIEVAL CANDIDATES',
  open: zh ? '按需展开' : 'OPEN ON DEMAND',
  selected: zh ? '进入分析' : 'IN CONTEXT',
  dropped: zh ? '已过滤' : 'FILTERED',
  source: zh ? '来源' : 'SOURCE',
  match: zh ? '匹配' : 'MATCH',
  reason: zh ? '选择依据' : 'SELECTION',
  empty: zh ? '本轮没有检索候选' : 'NO RETRIEVAL CANDIDATES',
  boundary: zh
    ? '历史事故和文档用于提出候选；现场探针负责确认当前状态。'
    : 'Incidents and documents propose candidates; fresh probes establish current state.',
})

export function InvestigationContextReceipt({
  lang,
  sessionId,
  receipt,
}: {
  lang: Lang
  sessionId: string
  receipt: InvestigationReceipt
}) {
  const zh = lang === 'zh'
  const tx = useMemo(() => text(zh), [zh])
  const rows = receipt.retrieval_results ?? []
  const selected = rows.filter((item) => item.selected_for_context)
  const dropped = rows.filter((item) => !item.selected_for_context)

  return (
    <details className="dx-cr" data-testid="investigation-context-receipt">
      <summary className="dx-cr-head">
        <div>
          <span>{tx.title}</span>
          <b>{sessionId}</b>
        </div>
        <span className="dx-cr-state">
          {tx.selected} {selected.length} · {tx.dropped} {dropped.length} · {tx.open}
        </span>
      </summary>

      <div className="dx-cr-body">
        {rows.length ? rows.map((item) => (
          <details className={`dx-cr-hit${item.selected_for_context ? ' is-selected' : ' is-dropped'}`} key={`${item.kind}:${item.item_id}`}>
            <summary>
              <span>{item.selected_for_context ? String(item.context_rank ?? '✓') : '×'}</span>
              <b>{item.title || item.item_id}</b>
              <em>{item.kind}</em>
            </summary>
            <p>{item.summary}</p>
            <dl>
              <div><dt>{tx.source}</dt><dd>{item.source} · {item.locator}</dd></div>
              <div><dt>{tx.match}</dt><dd>{(item.matched_on ?? []).join(' · ') || 'none'}</dd></div>
              <div><dt>{tx.reason}</dt><dd>{[
                ...(item.selection_reasons ?? []),
                ...(item.drop_reasons ?? []),
              ].join(' · ') || 'ranked candidate'}</dd></div>
            </dl>
          </details>
        )) : <p className="dx-cr-empty">{tx.empty}</p>}
        <p className="dx-cr-boundary">{tx.boundary}</p>
      </div>
    </details>
  )
}
