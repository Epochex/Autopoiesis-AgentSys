import { describe, expect, it } from 'vitest'
import { contextAt } from './live-context'
describe('context history', () => {
  const first = { at:'2026-09-07T00:00:00Z', kind:'memory_candidates_ranked', payload:{results:[{item_id:'m1', selected_for_context:true},{item_id:'m2', selected_for_context:false}]}}
  const next = { at:'2026-09-07T00:01:00Z', kind:'context_compiled', payload:{retrieval_results:[{item_id:'m2', selected_for_context:true}],messages:[{role:'user',content:'actual input'}]}}
  it('does not expose future input before its recorded step', () => {
    expect(contextAt([]).current).toBeUndefined()
    expect(contextAt([first]).current?.step.kind).toBe('memory_candidates_ranked')
    expect(contextAt([first]).current?.payload.messages).toBeUndefined()
  })
  it('shows actual model input and selection changes when available', () => {
    const result=contextAt([first,next])
    expect([...result.added]).toEqual(['m2'])
    expect(result.current?.payload.messages).toEqual([{role:'user',content:'actual input'}])
  })
  it('links filtered candidates by canonical identity without claiming adoption', () => {
    const result = contextAt([{ at:first.at, kind:'retrieval_candidates_filtered', payload:{kept_ids:['retrieval:indexed_memory:m1','retrieval:knowledge_document:doc#2']} }])
    expect(result.current?.rows.map(r => r.item_id)).toEqual(['m1','doc#2'])
    expect(result.current?.rows.every(r => r.candidate && !r.selected_for_context)).toBe(true)
    expect(result.added.size).toBe(0)
    expect(result.current?.rows[0].locator).toBe('retrieval:indexed_memory:m1')
  })
})
