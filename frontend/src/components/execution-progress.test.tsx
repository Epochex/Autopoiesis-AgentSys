import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { executionCommands, executionProgress } from './execution-progress'
import { subjectIncidentCycle } from './sentinel-cycle'
import { currentRound, railFor } from './autopoiesis-pipeline'
import { ExecutionProgress } from './ExecutionProgress'
import { theaterRailPresentation } from './TheaterStage'
import type { ChainStep, CommandLine } from './use-sentinel-chain'
const step = (kind:string,extra:Partial<ChainStep>={}):ChainStep=>({kind,at:'2026-09-08T09:00:00Z',subject:'demo.service',...extra})
const command = (extra:Partial<CommandLine>={}):CommandLine=>({command_id:'c1',at:'2026-09-08T09:00:00Z',argv:['systemctl','is-active','demo.service'],rc:null,out:'',phase:'started',...extra})
describe('live execution evidence',()=>{
  it('merges command lifecycle events by id and preserves their actual nonzero exit code',()=>{
    const rows=executionCommands([command(),command({phase:'completed',rc:3,stdout:'failed',out:'failed',duration_ms:12})])
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({state:'failed',rc:3,stdout:'failed',duration_ms:12})
  })
  it('does not mark recovery verified for exit zero, a remediation receipt, or an unverified resolved marker',()=>{
    for(const steps of [[step('remediated',{outcome:'passed'})],[step('resolved')]]) {
      expect(executionProgress({steps,commands:[command({phase:'completed',rc:0})]}).verified).toBe(false)
    }
  })
  it('marks recovery verified only with the actual recovery result and recorded verification',()=>{
    const progress=executionProgress({steps:[step('bakein_sampled',{phase:'fast'}),step('bakein_passed'),step('resolved',{outcome:'passed',samples:1,target_recovered:true})],commands:[]})
    expect(progress.verified).toBe(true)
    expect(progress.fastSamples).toBe(1)
  })
  it('shows aggregate verification counts without inventing missing individual sample records',()=>{
    const chain={steps:[step('remediated',{outcome:'passed',samples:7}),step('resolved',{samples:7,target_recovered:true})],commands:[]}
    const progress=executionProgress(chain)
    expect(progress.sampleCount).toBe(7)
    expect(progress.samples).toHaveLength(0)
    expect(progress.fastSamples).toBeUndefined()
    expect(progress.stableSamples).toBeUndefined()
    const html=renderToStaticMarkup(<ExecutionProgress lang="en" chain={chain}/>)
    expect(html).toContain('Individual sample details not recorded')
    expect(html).toContain('No command start or return is recorded')
  })
  it('keeps a cleared condition distinct from a verified repair',()=>{
    const progress=executionProgress({steps:[step('declined'),step('condition_cleared')],commands:[]})
    expect(progress.status).toBe('cleared')
    expect(progress.verified).toBe(false)
  })
  it('matches exact subjects and drops commands from previous cycles even with equal timestamps',()=>{
    const events=[step('resolved'),{...command({phase:'completed',rc:0}),kind:'command',subject:'demo.service'},step('detected'),step('detected',{subject:'other-demo.service'}),{...command(),kind:'command',subject:'demo.service'}]
    const cycle=subjectIncidentCycle(events,'demo.service')
    expect(cycle).toHaveLength(2)
    expect(cycle[0].kind).toBe('detected')
  })
  it('does not freeze a resumed execution at its earlier escalation',()=>{
    const steps=[step('detected'),step('escalated'),step('preflight',{eligible:true}),step('remediation_committed'),step('bakein_passed'),step('resolved',{samples:2,outcome:'passed'})]
    expect(currentRound(steps).at(-1)?.kind).toBe('resolved')
    expect(executionProgress({steps,commands:[]}).status).toBe('verified')
  })
  it('shows execution errors as failure without ticking pending verification stages',()=>{
    const steps=[step('detected'),step('preflight',{eligible:true}),step('execution_error',{stage:'execution',error:'SSH timeout'})]
    expect(executionProgress({steps,commands:[]}).status).toBe('failed')
    const rail=theaterRailPresentation({railIds:['detector','confirm','preflight','act','watch','verify'],timeline:steps,sentinel:true})
    expect(rail.stages.find(row=>row.id==='act')?.status).toBe('failed')
    expect(rail.stages.find(row=>row.id==='verify')?.status).toBe('pending')
  })
  it('allows a later authorized action to replace an earlier safety handoff',()=>{
    const steps=[step('detected'),step('no_safe_action'),step('preflight',{eligible:true}),step('remediation_committed')]
    const ids=railFor('sentinel',steps).map(row=>row.id)
    expect(ids).toContain('act')
    const rail=theaterRailPresentation({railIds:ids,timeline:steps,sentinel:true})
    expect(rail.terminalKind).toBeNull()
    expect(rail.stages.find(row=>row.id==='watch')?.status).toBe('current')
  })
  it('distinguishes a historical operation receipt from a running command and empty output',()=>{
    const entry=command({phase:'receipt',rc:null,stdout:null,stderr:null,receipt:{command:'systemctl restart demo.service',immediate_ok:true}})
    expect(executionCommands([entry])[0].state).toBe('receipt')
    const html=renderToStaticMarkup(<ExecutionProgress lang="en" chain={{steps:[step('resolved',{samples:7})],commands:[entry]}}/>)
    expect(html).toContain('Execution receipt (output not captured)')
    expect(html).toContain('were not captured')
    expect(html).not.toContain('Standard output is empty')
    expect(html).not.toContain('Command running')
  })
  it('exposes actual per-check readback results without converting missing observations to passes',()=>{
    const html=renderToStaticMarkup(<ExecutionProgress lang="en" chain={{commands:[],steps:[step('bakein_sampled',{reading:{management_ok:true,guard_ok:false}})]}}/>)
    expect(html).toContain('Individual readback results')
    expect(html).toContain('✓ Passed')
    expect(html).toContain('! Failed')
    expect(html).toContain('Not recorded')
    expect(html).not.toContain('✓ Recovery verified')
  })
  it('renders a running command and purpose before any result is available',()=>{
    const html=renderToStaticMarkup(<ExecutionProgress lang="en" chain={{steps:[step('preflight')],commands:[command({purpose:'Read the service active state'})]}}/>)
    expect(html).toContain('Command running')
    expect(html).toContain('Read the service active state')
    expect(html).toContain('awaiting tool output')
    expect(html).not.toContain('✓ Recovery verified')
  })
})
