import { describe, expect, it } from 'vitest'
import { memoryIdsForEvent } from './event-memory-links'
describe('event memory links', () => {
  it('joins exact structured IDs and deduplicates', () => {
    expect(memoryIdsForEvent({ memory_ids: ['m1', 'm1'], candidates: [{ memory_id: 'm2' }] }, ['m1', 'm2'])).toEqual(['m1', 'm2'])
  })
  it('rejects unrelated IDs, prose mentions and empty scope', () => {
    expect(memoryIdsForEvent({ id: 'm-other', text: 'previous m1 was used' }, ['m1'])).toEqual([])
    expect(memoryIdsForEvent({ memory_id: 'm1' }, [])).toEqual([])
  })
})
