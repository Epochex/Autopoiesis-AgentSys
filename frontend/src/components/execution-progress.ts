import type { Chain, CommandLine } from './use-sentinel-chain'

export type ExecutionCommand = CommandLine & { id: string; state: 'running' | 'returned' | 'failed' | 'receipt' }
export function executionCommands(commands: CommandLine[]): ExecutionCommand[] {
  const merged = new Map<string, CommandLine>()
  commands.forEach((command, index) => {
    const id = command.command_id ?? `legacy:${command.at}:${index}`
    merged.set(id, {...merged.get(id), ...command})
  })
  return [...merged.entries()].map(([id, row]) => ({...row,id,state: row.phase === 'receipt' ? 'receipt' : row.phase === 'started' ? 'running' : row.phase === 'error' || (row.rc != null && row.rc !== 0) ? 'failed' : row.rc != null || row.phase === 'completed' ? 'returned' : 'running'}))
}
export function executionProgress(chain: Chain | null) {
  const steps = chain?.steps ?? []
  const commands = executionCommands(chain?.commands ?? [])
  const last = steps.at(-1)
  const resolvedIndex = steps.findLastIndex((step,index) => step.kind === 'resolved' && (!step.outcome || step.outcome === 'passed') && step.target_recovered !== false && ((step.samples ?? 0) > 0 || steps.slice(0,index).some(prior => prior.kind === 'bakein_passed' || prior.kind === 'remediated' && prior.outcome === 'passed' && (prior.samples ?? 0) > 0 && prior.target_recovered !== false)))
  const failureIndex = steps.findLastIndex(step => ['bakein_regressed','remediation_reverted','revert_unverified','execution_error'].includes(step.kind) || step.kind === 'remediated' && (step.needs_human || step.outcome !== 'passed'))
  const pauseIndex = steps.findLastIndex(step => ['declined','cooldown','escalated','no_safe_action'].includes(step.kind))
  const resumedIndex = steps.findLastIndex(step => ['preflight','remediation_started','remediation_committed','bakein_opened','bakein_sampled','bakein_passed'].includes(step.kind))
  const cleared = last?.kind === 'condition_cleared'
  const verified = resolvedIndex >= 0 && resolvedIndex > failureIndex && resolvedIndex > pauseIndex
  const failed = failureIndex > resolvedIndex && failureIndex >= resumedIndex || !verified && commands.at(-1)?.phase === 'error'
  const paused = !cleared && pauseIndex > resumedIndex && pauseIndex > resolvedIndex
  const samples = steps.filter(step => step.kind === 'bakein_sampled')
  const aggregateSamples = steps.filter(step => ['resolved','remediated','bakein_passed'].includes(step.kind) && typeof step.samples === 'number').map(step => step.samples!)
  const sampleCount = samples.length || aggregateSamples.length ? Math.max(samples.length,...aggregateSamples) : undefined
  const hasPhaseDetails = samples.some(step => step.phase === 'fast' || step.phase === 'stability')
  const current = commands.findLast(command => command.state === 'running')
  const reason = (failed ? steps[failureIndex] : paused ? steps[pauseIndex] : last)?.error ?? (failed ? steps[failureIndex] : paused ? steps[pauseIndex] : last)?.reason ?? (failed ? steps[failureIndex] : paused ? steps[pauseIndex] : last)?.summary
  return {commands,current,verified,failed,paused,cleared,samples,sampleCount,fastSamples:hasPhaseDetails ? samples.filter(step=>step.phase==='fast').length : undefined,stableSamples:hasPhaseDetails ? samples.filter(step=>step.phase==='stability').length : undefined,
    status: verified ? 'verified' : failed ? 'failed' : paused ? 'paused' : cleared ? 'cleared' : current ? 'running' : samples.length ? 'verifying' : steps.length ? 'observing' : 'waiting', reason,
    lastStep:last, verificationPassed:steps.some(step=>step.kind==='bakein_passed'), lastReadAt:chain?.lastReadAt, readError:chain?.readError}
}
