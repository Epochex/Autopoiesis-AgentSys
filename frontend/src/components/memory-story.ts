import type { MemEvent, MemRecall, MemoryReplayCase, Observatory } from '../types'

export interface MemoryRunStory {
  runId: string
  pass: number
  caseId: string
  caseIndex: number
  recall: MemRecall
  events: MemEvent[]
  firstEventSeq: number
  lastEventSeq: number
  caseInfo: MemoryReplayCase | null
}

/** Join the read side and write side of every replay run.
 *
 * `recall` is emitted once per run. Lifecycle events carry the same run_id, so
 * this is a lossless grouping over backend identifiers. No score, operation or
 * ordering value is manufactured here.
 */
export function buildMemoryRunStories(
  obs: Pick<Observatory, 'recall' | 'events'>,
  cases: MemoryReplayCase[],
): MemoryRunStory[] {
  const caseOrder = new Map(cases.map((item, index) => [item.id, index]))
  const caseById = new Map(cases.map((item) => [item.id, item]))
  const eventsByRun = new Map<string, MemEvent[]>()

  for (const event of obs.events) {
    const rows = eventsByRun.get(event.run_id) ?? []
    rows.push(event)
    eventsByRun.set(event.run_id, rows)
  }

  return [...obs.recall]
    .sort((a, b) => a.seq - b.seq)
    .map((recall) => {
      const events = [...(eventsByRun.get(recall.run_id) ?? [])].sort((a, b) => a.seq - b.seq)
      return {
        runId: recall.run_id,
        pass: recall.pass,
        caseId: recall.case_id,
        caseIndex: caseOrder.get(recall.case_id) ?? -1,
        recall,
        events,
        firstEventSeq: events[0]?.seq ?? 0,
        lastEventSeq: events[events.length - 1]?.seq ?? 0,
        caseInfo: caseById.get(recall.case_id) ?? null,
      }
    })
}

export function countMemoryOps(events: MemEvent[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const event of events) counts[event.op] = (counts[event.op] ?? 0) + 1
  return counts
}
