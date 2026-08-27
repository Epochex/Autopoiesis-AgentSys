import { describe, expect, it } from 'vitest'
import type { MemEvent, MemRecall, MemoryReplayCase } from '../types'
import { buildMemoryRunStories, countMemoryOps } from './memory-story'

const recall = (runId: string, seq: number, caseId: string): MemRecall => ({
  seq,
  pass: 0,
  case_id: caseId,
  run_id: runId,
  retrieved: {},
  retrieval_candidates: [],
  included_memory_ids: [],
  dropped_memory_ids: [],
  context_drops: [],
  probes: 1,
  shortcut: false,
  resolved: false,
  resolved_memory_ids: [],
})

const event = (runId: string, seq: number, op: MemEvent['op']): MemEvent => ({
  seq,
  pass: 0,
  case_id: 'case-a',
  run_id: runId,
  op,
  memory_id: `memory-${seq}`,
  tier: 'episodic',
  similarity: null,
  target_id: null,
  before: null,
  after: null,
  added_tags: [],
  added_assets: [],
})

describe('buildMemoryRunStories', () => {
  it('joins recalls to ordered lifecycle events by the backend run id', () => {
    const cases: MemoryReplayCase[] = [{ id: 'case-a', query: 'q', rootCause: 'r', assets: [] }]
    const stories = buildMemoryRunStories(
      {
        recall: [recall('run-b', 1, 'case-a'), recall('run-a', 0, 'case-a')],
        events: [event('run-a', 4, 'LINK'), event('run-a', 2, 'ADD'), event('run-b', 8, 'REINFORCE')],
      },
      cases,
    )

    expect(stories.map((story) => story.runId)).toEqual(['run-a', 'run-b'])
    expect(stories[0].events.map((row) => row.seq)).toEqual([2, 4])
    expect(stories[0].firstEventSeq).toBe(2)
    expect(stories[0].lastEventSeq).toBe(4)
    expect(stories[0].caseInfo?.rootCause).toBe('r')
  })

  it('counts only lifecycle operations present in the ledger', () => {
    expect(countMemoryOps([
      event('run-a', 0, 'ADD'),
      event('run-a', 1, 'ADD'),
      event('run-a', 2, 'DECAY'),
    ])).toEqual({ ADD: 2, DECAY: 1 })
  })
})
