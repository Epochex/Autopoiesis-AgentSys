import { afterEach, describe, expect, it, vi } from 'vitest'
import { filterGlobalMemory, loadGlobalMemory } from './global-memory'
import type { LiveMemoryListRecord } from '../types'
const record = (id: string, quarantined = false) => ({memory_id:id,text:`Knowledge ${id}`,tier:'semantic',asset_ids:['10.0.0.1'],tags:[],quarantined,source_trace_ids:0,confidence:1,importance:1,strength:1,access_count:0,first_observed_at:null,last_observed_at:null,valid_from:null,valid_to:null}) satisfies LiveMemoryListRecord
const inventory = {ok:true,durable:true,counts:{semantic:2,asset_profile:0,procedural:0,episodic:0,quarantined:0},budget:{active:2,configured:null},retention:{decay_wired:false,eviction_wired:false,last_decay_at:null}}
afterEach(() => vi.unstubAllGlobals())
describe('global memory reads', () => {
  it('reads every inventory page and follows raw-event high water even when visible events are empty', async () => {
    const fetch = vi.fn().mockResolvedValueOnce({ok:true,json:async()=>({...inventory,records:[record('a')],total:2,next_offset:1})})
      .mockResolvedValueOnce({ok:true,json:async()=>({...inventory,records:[record('b')],total:2,next_offset:null})})
      .mockResolvedValueOnce({ok:true,json:async()=>({ok:true,durable:true,events:[],next_offset:20,high_water:20})})
      .mockResolvedValueOnce({ok:true,json:async()=>({ok:true,durable:true,events:[{offset:21,memory_id:'removed',version:3,event_type:'QUARANTINE'}],next_offset:null,high_water:23})})
    vi.stubGlobal('fetch', fetch)
    const result = await loadGlobalMemory(new AbortController().signal)
    expect(result.records.map(row=>row.memory_id)).toEqual(['a','b'])
    expect(result.events[0].memory_id).toBe('removed')
    expect(result.after).toBe(23)
    expect(fetch.mock.calls[1][0]).toContain('offset=1&')
    expect(fetch.mock.calls[3][0]).toContain('after=20&')
  })
  it('rejects non-durable inventory instead of presenting it as online memory', async()=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,json:async()=>({...inventory,durable:false,records:[]})}))
    await expect(loadGlobalMemory(new AbortController().signal)).rejects.toThrow('MEMORY_UNAVAILABLE')
  })
  it('filters globally by asset, tier and quarantine state',()=>{
    expect(filterGlobalMemory([record('a'),record('b',true)],'10.0.0.1','semantic','quarantined').map(row=>row.memory_id)).toEqual(['b'])
  })
})
