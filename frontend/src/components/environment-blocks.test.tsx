import { describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { CoverageRail, OwnershipTimeline, ScopeStrip } from './environment-blocks'
import type { EnvCoverageRow, EnvFinding, EnvSource } from '../types'
vi.mock('./useDisplayTranslation', () => ({ useDisplayTranslation: () => ({translate: (text: string) => /[\u3400-\u9fff]/.test(text) ? 'Translated description' : text, loading:false,error:null,retry:()=>{}}) }))
describe('continuous network English display', () => {
  it('translates source labels, units and explanatory notes', () => {
    const sources = [{id:'traffic',label:'生产流量',note:'持续采集',unit:'事件',flowing:true,volume:12,age_seconds:5,window_start:null,window_end:null}] as unknown as EnvSource[]
    const html = renderToStaticMarkup(<ScopeStrip sources={sources} zh={false}/>)
    expect(html).not.toMatch(/[\u3400-\u9fff]/)
    expect(html).toContain('Translated description')
  })
  it('translates a Chinese fallback coverage explanation', () => {
    const rows = [{fault_class:'rogue_dhcp',label:'Second DHCP server',coverage:'blind',requires:['dhcp'],present:[],missing:['dhcp'],closes_with:'需要补充采集',closes_with_zh:null}] as EnvCoverageRow[]
    const html = renderToStaticMarkup(<CoverageRail rows={rows} zh={false}/>)
    expect(html).not.toMatch(/[\u3400-\u9fff]/)
    expect(html).toContain('SECOND DHCP SERVER')
  })
  it('renders operator, cached identity, ARP and retirement annotations in English', () => {
    const finding = {subject:'10.0.0.1',measured:{macs:['aa','bb'],transitions:[{captured_at:'2026-01-01T00:00:00Z',mac:'aa'},{captured_at:'2026-01-01T00:01:00Z',mac:'bb'}]}} as unknown as EnvFinding
    const profiles = [{mac:'aa',name:'会议室设备',name_source:'operator',identity_fresh:true,in_arp_snapshot:true,has_historical_dhcp:true,retired:true},{mac:'bb',name:'备用设备',identity_fresh:false,in_arp_snapshot:false,has_historical_dhcp:false,retired:false}]
    const html = renderToStaticMarkup(<OwnershipTimeline finding={finding} profiles={profiles} zh={false}/>)
    expect(html).not.toMatch(/[\u3400-\u9fff]/)
    expect(html).toContain('Operator label')
    expect(html).toContain('Cached identity')
    expect(html).toContain('Retired record')
  })
})
