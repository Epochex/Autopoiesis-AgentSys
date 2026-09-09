import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { analysisUrl, readAnalysis, observationFor, observedCount } from './analysis-read'
import { ThreatCard } from './ThreatCard'
import { AnalysisStatus } from './AnalysisStatus'
afterEach(() => vi.unstubAllGlobals())
describe('observation-first analysis', () => {
  it('uses the selected source and its real same-netblock counts without substituting zero', () => {
    const facts = {selected:[{ip:'203.0.113.7',events:12,eventTypes:['admin_login_failed'],ports:[443],lastSeenAt:'2026-09-08T08:00:00Z'}],sameNetblock:[{ip:'203.0.113.8',events:27}]}
    expect(observationFor(facts,'203.0.113.7')?.eventTypes).toEqual(['admin_login_failed'])
    expect(observedCount(observationFor(facts,'203.0.113.8','sameNetblock')?.events)).toBe(27)
    expect(observedCount(observationFor(facts,'203.0.113.9','sameNetblock')?.events)).toBeUndefined()
  })
  it('suppresses a duplicated baseline basis beneath the factual verdict', () => {
    const basis = 'Observed attack attempts; impact requires verification'
    const html = renderToStaticMarkup(<ThreatCard th={{ip:'10.0.0.1',loading:false,analysisAvailable:false,analysisState:'baseline',verdict:basis,analysis:basis,riskAssessment:{basis,attackAttempt:true}}} lang="en" onClose={()=>{}}/>)
    expect(html.split(basis)).toHaveLength(2)
    expect(html).not.toContain('Assessment basis')
  })
  it('uses read mode for ordinary WAN, host and subnet requests', async () => {
    const fetch = vi.fn().mockResolvedValue({ok:true,json:async()=>({ok:true,analysisState:'baseline'})})
    vi.stubGlobal('fetch',fetch)
    await Promise.all([readAnalysis('wan','203.0.113.7','en'),readAnalysis('host','10.0.0.1','en'),readAnalysis('subnet','10.0.0.0/24','en')])
    for (const [url] of fetch.mock.calls) {
      const query = new URL(url,'http://localhost').searchParams
      expect(query.get('mode')).toBe('read')
      expect(query.get('force')).toBe('false')
    }
    expect(fetch.mock.calls.some(([url]) => String(url).includes('mode=analyze'))).toBe(false)
  })
  it('requests paid analysis only through explicit analyze mode and respects forced refresh', () => {
    const query = new URL(analysisUrl('wan','203.0.113.7','en','analyze',true),'http://localhost').searchParams
    expect(query.get('mode')).toBe('analyze')
    expect(query.get('force')).toBe('true')
  })
  it('keeps the current observed facts visible throughout loading and failure', () => {
    const html = renderToStaticMarkup(<ThreatCard th={{ip:'10.0.0.1',loading:true,analysisState:'baseline',analysisAvailable:false,verdict:'Observed activity',analysis:'12 observed flows',error:'Model unavailable'}} lang="en" onClose={()=>{}} onAnalyze={()=>{}}/>)
    expect(html).toContain('12 observed flows')
    expect(html).toContain('Model unavailable')
    expect(html).toContain('current observations remain visible')
    expect(html).not.toContain('sev-critical')
  })
  it('does not claim unchanged evidence when no historical result exists', () => {
    const html = renderToStaticMarkup(<AnalysisStatus value={{analysisState:'baseline',cache:{status:'miss',analyzedAt:null,evidenceChanged:false}}} lang="en"/>)
    expect(html).not.toContain('Evidence unchanged')
    expect(html).toContain('Current observed facts')
  })
})
