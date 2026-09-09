import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { MemoryGraph } from './MemoryGraph'
import { ContextPacket } from './ContextPacket'
import type { MemRecord } from '../types'
const record = (id: string): MemRecord => ({memory_id:id,tier:'procedural',text:'核对地址归属和设备租约',tags:[],asset_ids:[],evidence_ids:[],confidence:1,importance:1,strength:1,quarantined:false,quarantine_reason:null,source_trace_ids:[],links:[],relations:[],evidence_snapshot:[]})
describe('original memory graph with online records', () => {
  it('routes recorded reads and writes and leaves unknown document sources unconnected', () => {
    const html = renderToStaticMarkup(<MemoryGraph records={[record('m1')]} events={[]} touchedIds={new Set(['m1'])} recall={null} selectedId={null} pinnedId={null} onSelect={()=>{}} zh online={{versions:{m1:'v2'},phase:'已完成选材',entries:[{id:'read',label:'核对租约',memoryId:'m1'},{id:'write',label:'写回方法',memoryId:'m1',write:true},{id:'document',label:'设备手册'}]}}/>)
    expect(html.match(/data-memory-id="m1"/g)).toHaveLength(2)
    expect(html).toContain('data-action="read"')
    expect(html).toContain('data-action="write"')
    expect(html).toContain('mg-node live touch')
  })
  it('packs untagged records without overlapping boxes', () => {
    const html = renderToStaticMarkup(<MemoryGraph records={Array.from({length:7},(_,i)=>record('m'+i))} events={[]} touchedIds={new Set()} recall={null} selectedId={null} pinnedId={null} onSelect={()=>{}} zh online={{versions:{},phase:'等待检索',entries:[]}}/>)
    const positions = [...html.matchAll(/<rect x="([^"]+)" y="([^"]+)" width="[^"]+" height="[^"]+" class="mg-node-bg"/g)].map(m=>m[1]+':'+m[2])
    expect(positions).toHaveLength(7)
    expect(new Set(positions).size).toBe(7)
  })
  it('restores the context packet with real missing-score states', () => {
    const html = renderToStaticMarkup(<ContextPacket online={{rows:[{item_id:'m1',candidate:true,selected_for_context:false}],previous:[],phase:'检索候选',text:{m1:'核对租约'},onSelect:()=>{}}}/>)
    expect(html).toContain('完整上下文')
    expect(html).toContain('核对租约')
    expect(html).not.toContain('0.0000')
  })
})
