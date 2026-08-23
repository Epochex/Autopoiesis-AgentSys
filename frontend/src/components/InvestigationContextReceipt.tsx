import { useEffect, useMemo, useState } from 'react'
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

export interface InvestigationReceipt {
  probe_candidates: string[]
  probe_prior: ProbePriorReceipt
  historical_context: Record<string, unknown>
  knowledge_context: KnowledgeReceipt[]
  trace_events: ContextTraceEvent[]
}

type Influence = {
  kind: string
  at: string
  subject: string
  what_changed: string
  evidence: Record<string, unknown>
}

type InfluenceState = 'idle' | 'checking' | 'confirmed' | 'missing' | 'error'

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []

const number = (value: unknown): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : 0

const counts = (context: Record<string, unknown>): [string, number][] =>
  ['dossiers', 'risks', 'features']
    .map((key) => [key, Array.isArray(context[key]) ? context[key].length : 0] as [string, number])
    .filter(([, value]) => value > 0)

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
  const shortcut = receipt.trace_events.find((event) => event.kind === 'memory_shortcut')
  const payload = shortcut?.payload ?? {}
  const memoryIds = useMemo(
    () => strings(payload.memory_ids).length ? strings(payload.memory_ids) : (receipt.probe_prior.memory_ids ?? []),
    [payload.memory_ids, receipt.probe_prior.memory_ids],
  )
  const [influenceState, setInfluenceState] = useState<InfluenceState>(memoryIds.length ? 'checking' : 'idle')
  const [influences, setInfluences] = useState<Influence[]>([])

  useEffect(() => {
    let alive = true
    if (!memoryIds.length) {
      setInfluenceState('idle')
      setInfluences([])
      return () => { alive = false }
    }
    setInfluenceState('checking')
    void Promise.all(memoryIds.map(async (memoryId) => {
      const response = await fetch(`/api/rca/memory/${encodeURIComponent(memoryId)}/influence`, {
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const body = await response.json() as { influences?: Influence[] }
      return (body.influences ?? []).filter(
        (item) => String(item.evidence?.source_trace_id ?? '') === sessionId,
      )
    }))
      .then((rows) => {
        if (!alive) return
        const matched = rows.flat()
        setInfluences(matched)
        setInfluenceState(matched.length ? 'confirmed' : 'missing')
      })
      .catch(() => { if (alive) setInfluenceState('error') })
    return () => { alive = false }
  }, [memoryIds, sessionId])

  const preferred = strings(payload.preferred_probes)
  const original = strings(payload.original_probe_order)
  const planned = strings(payload.planned_probe_order)
  const executed = strings(payload.executed_probe_order)
  const skipped = strings(payload.skipped_probes)
  const operationalCounts = counts(receipt.historical_context)

  return (
    <section className="dx-cr" data-testid="investigation-context-receipt">
      <header className="dx-cr-head">
        <div>
          <span>{zh ? '本次调查上下文回执' : 'INVESTIGATION CONTEXT RECEIPT'}</span>
          <b>{sessionId}</b>
        </div>
        <span className={`dx-cr-state is-${influenceState}`}>
          {influenceState === 'confirmed'
            ? (zh ? `影响记录已持久化 · ${influences.length}` : `INFLUENCE PERSISTED · ${influences.length}`)
            : influenceState === 'checking'
              ? (zh ? '正在核对影响记录' : 'CHECKING INFLUENCE')
              : influenceState === 'missing'
                ? (zh ? '已检索，未形成可归因影响' : 'RETRIEVED · NO ATTRIBUTED EFFECT')
                : influenceState === 'error'
                  ? (zh ? '影响端点不可读' : 'INFLUENCE ENDPOINT UNREADABLE')
                  : (zh ? '本轮没有相关程序性记忆' : 'NO RELEVANT PROCEDURAL MEMORY')}
        </span>
      </header>

      <div className="dx-cr-grid">
        <section className="dx-cr-col">
          <header><b>{zh ? '历史记忆' : 'HISTORICAL MEMORY'}</b><span>{memoryIds.length}</span></header>
          {memoryIds.length ? (
            <>
              <p className="dx-cr-line"><span>{zh ? '命中' : 'RECALLED'}</span>{memoryIds.join(' · ')}</p>
              <p className="dx-cr-line"><span>{zh ? '根因族' : 'ROOT FAMILY'}</span>{String(payload.confirmed_root_key ?? receipt.probe_prior.root_key ?? '无')}</p>
              <p className="dx-cr-line"><span>{zh ? '优先探针' : 'PREFERRED'}</span>{preferred.join(' · ') || '无'}</p>
              <p className="dx-cr-line"><span>{zh ? '探针变化' : 'PROBE EFFECT'}</span>
                {zh
                  ? `${original.length || number(payload.candidate_probe_count)} 条候选 → ${executed.length || receipt.probe_candidates.length} 条实际执行，跳过 ${skipped.length}`
                  : `${original.length || number(payload.candidate_probe_count)} candidates → ${executed.length || receipt.probe_candidates.length} executed, ${skipped.length} skipped`}
              </p>
              {original.length && planned.length ? (
                <details className="dx-cr-detail">
                  <summary>{zh ? '展开探针顺序变化' : 'SHOW PROBE ORDER CHANGE'}</summary>
                  <div><span>{zh ? '原顺序' : 'BEFORE'}</span><code>{original.join(' → ')}</code></div>
                  <div><span>{zh ? '记忆排序后' : 'AFTER MEMORY'}</span><code>{planned.join(' → ')}</code></div>
                  <div><span>{zh ? '本轮实际执行' : 'EXECUTED'}</span><code>{executed.join(' → ') || '无'}</code></div>
                </details>
              ) : null}
            </>
          ) : <p className="dx-cr-empty">{zh ? '本轮保持默认探针顺序' : 'DEFAULT PROBE ORDER RETAINED'}</p>}
          {operationalCounts.length ? (
            <p className="dx-cr-line"><span>{zh ? '业务记忆' : 'DOMAIN MEMORY'}</span>
              {operationalCounts.map(([key, value]) => `${key} ${value}`).join(' · ')}
            </p>
          ) : null}
        </section>

        <section className="dx-cr-col">
          <header><b>{zh ? '知识检索' : 'KNOWLEDGE RETRIEVAL'}</b><span>{receipt.knowledge_context.length}</span></header>
          {receipt.knowledge_context.length ? receipt.knowledge_context.map((item) => (
            <details className="dx-cr-doc" key={item.document_id}>
              <summary><b>{item.title}</b><span>BM25 {item.score.toFixed(3)}</span></summary>
              <p>{item.text}</p>
              <footer>{item.source} · {item.locator} · {item.matched_terms.join(', ')}</footer>
            </details>
          )) : <p className="dx-cr-empty">{zh ? '知识库对本问题选择了弃答' : 'KNOWLEDGE RETRIEVAL ABSTAINED'}</p>}
          <p className="dx-cr-boundary">
            {zh
              ? '知识片段解释命令和操作约束；当前探针确认现场状态；动作策略负责授权。'
              : 'Knowledge explains commands and constraints; fresh probes establish state; action policy authorizes writes.'}
          </p>
        </section>
      </div>
    </section>
  )
}
