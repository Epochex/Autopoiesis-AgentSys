/* The two chains this system can run, each named ONCE for every surface that
 * draws it: the live-situation panel's stage strip and the topology theater's
 * rail share this vocabulary (it mirrors the backend's stage ids).
 *
 * They are separate rails because they are separate subsystems. A sentinel
 * chain never enters the correlator or the suggestions topic, so drawing it on
 * the NetOps rail would light stages that did not run. */
export const PIPELINE: { id: string; zh: string; en: string }[] = [
  { id: 'correlator', zh: '关联器', en: 'CORRELATOR' },
  { id: 'alerts-topic', zh: '告警流', en: 'ALERTS' },
  { id: 'cluster-window', zh: '簇窗口', en: 'CLUSTER' },
  { id: 'aiops-agent', zh: 'AIOps 推理', en: 'AIOPS' },
  { id: 'suggestions-topic', zh: '建议流', en: 'SUGGEST' },
  { id: 'remediation', zh: '处置预案', en: 'REMEDIATE' },
]

/* The sentinel's own loop, running in the gateway process on R450. Six steps to
 * match what the timeline actually records — the same six the live progress rail
 * inside the theater walks. */
export const SENTINEL_LOOP: { id: string; zh: string; en: string }[] = [
  { id: 'detector', zh: '巡检发现', en: 'DETECT' },
  { id: 'confirm', zh: '二次确认', en: 'CONFIRM' },
  { id: 'preflight', zh: '前置校验', en: 'PREFLIGHT' },
  { id: 'act', zh: '执行动作', en: 'ACT' },
  { id: 'watch', zh: '观察期', en: 'WATCH' },
  { id: 'verify', zh: '回读验证', en: 'VERIFY' },
]

export const railFor = (scope?: string) => (scope === 'sentinel' ? SENTINEL_LOOP : PIPELINE)

/* Which rail steps a sentinel chain actually reached, read off its timeline. */
const SENTINEL_REACHED: Record<string, string[]> = {
  detected: ['detector'],
  preflight: ['confirm', 'preflight'],
  remediated: ['act', 'watch'],
  resolved: ['verify'],
  no_safe_action: ['confirm'],
  declined: ['confirm', 'preflight'],
  cooldown: ['confirm'],
}

const SENTINEL_TERMINAL = new Set(['remediated', 'resolved', 'no_safe_action', 'declined', 'cooldown'])

export function sentinelStageIds(timeline: { kind: string }[]): string[] {
  const hit = new Set<string>()
  for (const step of timeline) for (const id of SENTINEL_REACHED[step.kind] ?? []) hit.add(id)
  // The action runs immediately after preflight, but `remediated` is only written
  // once the watch window closes ~90s later. Without this, the rail claims the
  // change has not happened during the exact stretch when it has.
  const closed = timeline.some((s) => SENTINEL_TERMINAL.has(s.kind))
  if (hit.has('preflight') && !closed) { hit.add('act'); hit.add('watch') }
  return SENTINEL_LOOP.map((s) => s.id).filter((id) => hit.has(id))
}
