import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { MemEvent, MemRecall, Observatory } from '../types'
import { MemoryReplayStory } from './MemoryReplayStory'

const event: MemEvent = {
  seq: 0,
  pass: 0,
  case_id: 'case-a',
  run_id: 'run-a',
  op: 'ADD',
  memory_id: 'mem-a',
  tier: 'episodic',
  similarity: null,
  target_id: null,
  before: null,
  after: null,
  added_tags: [],
  added_assets: [],
}

const recall: MemRecall = {
  seq: 0,
  pass: 0,
  case_id: 'case-a',
  run_id: 'run-a',
  retrieved: { episodic: ['mem-a'] },
  retrieval_candidates: [{
    memory_id: 'mem-a',
    tier: 'episodic',
    lexical_score: 0.4,
    vector_score: 0,
    asset_hits: 1,
    graph_hop: 0,
    graph_parent_id: null,
    structural_prior: 0.2,
    final_score: 0.6,
  }],
  included_memory_ids: ['mem-a'],
  dropped_memory_ids: [],
  context_drops: [],
  probes: 2,
  shortcut: false,
  resolved: true,
  resolved_memory_ids: ['mem-a'],
}

const observatory: Observatory = {
  records: [{
    memory_id: 'mem-a',
    tier: 'episodic',
    text: 'candidate detail remains behind disclosure',
    strength: 1,
    importance: 1,
    confidence: 1,
    tags: [],
    asset_ids: [],
    evidence_ids: [],
    links: [],
    quarantined: false,
    quarantine_reason: null,
    source_trace_ids: [],
    relations: [],
    evidence_snapshot: [],
  }],
  events: [event],
  recall: [recall],
  capabilities: {
    decay_wired: false,
    retrieval_scores: true,
    context_drop_reason: true,
    update_text_mutation: false,
  },
}

describe('MemoryReplayStory progressive disclosure', () => {
  it('renders the visual signal path while keeping record text out of the first read', () => {
    const html = renderToStaticMarkup(
      <MemoryReplayStory
        obs={observatory}
        cases={[{ id: 'case-a', query: 'what changed?', rootCause: 'root-a', assets: ['asset-a'] }]}
        cursorSeq={0}
        currentEvent={event}
        playing={false}
        onCursor={() => {}}
        onTogglePlay={() => {}}
        onSelectMemory={() => {}}
        onDeepDive={() => {}}
        zh
      />,
    )

    expect(html).toContain('mrs-dot-field')
    expect(html).toContain('mrs-funnel')
    expect(html).toContain('mrs-write-code')
    expect(html).toContain('aria-expanded="false"')
    expect(html).not.toContain('candidate detail remains behind disclosure')
    expect(html).not.toContain('mrs-detail step-')
  })
})
