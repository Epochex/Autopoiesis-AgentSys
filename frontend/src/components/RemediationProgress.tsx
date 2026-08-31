import { useMemo } from 'react'
import type { Lang } from '../i18n'
import { currentRound } from './autopoiesis-pipeline'
import { type ChainStep as Step, useSentinelChain } from './use-sentinel-chain'

/* Current response state for the selected device.
 *
 * Lives inside the theater so the blast radius and the response are read in one
 * place: the topology says which nodes are involved, this says what is being
 * done to them and how far along it is.
 *
 * Observation-window samples expose progress and the condition required for a
 * verified close or rollback. */

/** The chain's shape, in the order the system walks it. */
const ACTION_PHASES = ['detected', 'confirmed', 'preflight', 'acting', 'watching', 'closed'] as const
const REPORT_PHASES = ['detected', 'confirmed', 'gate', 'handoff'] as const
type Phase = (typeof ACTION_PHASES)[number] | (typeof REPORT_PHASES)[number]

const PHASE_LABEL: Record<Phase, [string, string]> = {
  detected: ['检测事实', 'DETECTION FACT'],
  confirmed: ['二次确认', 'SECOND CONFIRMATION'],
  preflight: ['安全门条件', 'SAFETY CONDITIONS'],
  acting: ['动作执行', 'ACTION'],
  watching: ['回读观察', 'READBACK'],
  closed: ['决策结果', 'DECISION'],
  gate: ['安全门条件', 'SAFETY CONDITIONS'],
  handoff: ['人工交接', 'OPERATOR HANDOFF'],
}

function phaseOf(steps: Step[]): { reached: Set<Phase>; current: Phase; terminal: string | null } {
  const reached = new Set<Phase>()
  let terminal: string | null = null
  for (const step of steps) {
    switch (step.kind) {
      case 'detected':
        reached.add('detected')
        if ((step.streak ?? 0) >= (step.need ?? 2)) reached.add('confirmed')
        break
      case 'preflight':
        reached.add('confirmed')
        reached.add('preflight')
        break
      case 'remediated':
        reached.add('acting')
        reached.add('watching')
        reached.add('closed')
        terminal = step.needs_human ? 'needs_human' : step.outcome ?? null
        break
      case 'resolved':
        reached.add('closed')
        terminal = 'resolved'
        break
      case 'no_safe_action':
        reached.add('confirmed')
        reached.add('gate')
        reached.add('handoff')
        terminal = 'no_safe_action'
        break
      case 'escalated':
        // Recurrence escalation closes this round at the confirmation decision.
        reached.add('confirmed')
        reached.add('closed')
        terminal = 'escalated'
        break
      case 'declined':
      case 'cooldown':
        reached.add('closed')
        terminal = step.kind
        break
      default:
        break
    }
  }
  // A preflight that passed with no verdict yet means the action is in flight,
  // including the long silent stretch of the watch window.
  if (reached.has('preflight') && !reached.has('closed')) {
    reached.add('acting')
    reached.add('watching')
  }
  const order: readonly Phase[] = terminal === 'no_safe_action' ? REPORT_PHASES : ACTION_PHASES
  const current = [...order].reverse().find((p) => reached.has(p)) ?? 'detected'
  return { reached, current, terminal }
}

const TERMINAL_LABEL: Record<string, [string, string]> = {
  resolved: ['恢复已验证', 'RECOVERY VERIFIED'],
  passed: ['恢复已验证', 'RECOVERY VERIFIED'],
  escalated: ['复发阈值触发 · 已升级人工', 'RECURRENCE THRESHOLD · ESCALATED'],
  needs_human: ['待人工复核', 'OPERATOR REVIEW'],
  no_safe_action: ['写操作未授权 · 已转人工', 'WRITE NOT AUTHORIZED · HANDED OFF'],
  declined: ['安全门未放行', 'SAFETY GATE BLOCKED'],
  cooldown: ['冷却中', 'COOLING DOWN'],
  reverted: ['已回滚', 'REVERTED'],
  revert_unverified: ['回滚未能验证', 'REVERT UNVERIFIED'],
}

function withheldWrite(steps: Step[], zh: boolean): {
  fact: string; action: string; gate: string; result: string; owner: string
} | null {
  const refusal = [...steps].reverse().find((step) => step.kind === 'no_safe_action')
  if (!refusal) return null
  const detection = [...steps].reverse().find((step) => step.kind === 'detected')
  const subject = detection?.subject ?? refusal.subject ?? ''
  const recorded = refusal.reason?.trim()
  const sourceRequiresValidation = /^(192\.0\.2|198\.51\.100|203\.0\.113)\./.test(subject)
  const gate = sourceRequiresValidation
    ? (zh
        ? '安全门条件 · 来源归属确认；管理地址豁免；封禁 TTL；提交后回读；超时自动回滚。当前条件未齐。'
        : 'SAFETY CONDITIONS · Confirmed source ownership; management-address exemption; block TTL; post-commit readback; timed rollback. Current conditions are incomplete.')
    : (recorded || refusal.note || (zh
        ? '安全门条件 · 封禁 TTL、管理地址豁免、提交后回读和超时自动回滚均需满足。'
        : 'SAFETY CONDITIONS · Block TTL, management-address exemption, post-commit readback, and timed rollback are required.'))
  return {
    fact: sourceRequiresValidation
      ? (zh
          ? `检测事实 · ${subject} 产生重复失败登录记录；来源归属和活动会话待核验。`
          : `DETECTION FACT · ${subject} produced repeated failed-login records; source ownership and active sessions require validation.`)
      : (zh ? `检测事实 · ${subject} 触发安全事件规则。` : `DETECTION FACT · ${subject} triggered the security-event rule.`),
    action: zh ? '候选动作 · 临时防火墙封禁' : 'CANDIDATE ACTION · TEMPORARY FIREWALL BLOCK',
    gate,
    result: zh ? '决策结果 · 写操作未授权，防火墙配置保持原版本。' : 'DECISION · WRITE NOT AUTHORIZED; FIREWALL CONFIGURATION REMAINS AT THE PRIOR VERSION.',
    owner: zh ? '后续责任 · 安全运营核验来源、活动会话和影响范围后处置。' : 'FOLLOW-UP OWNER · SECURITY OPERATIONS VALIDATES THE SOURCE, ACTIVE SESSIONS, AND IMPACT SCOPE.',
  }
}

function recurrenceContext(steps: Step[], zh: boolean): {
  current: string
  evidence: string
  history: string
  decision: string
  next: string
} | null {
  const escalation = [...steps].reverse().find((step) => step.kind === 'escalated')
  if (!escalation) return null
  const detection = [...steps].reverse().find((step) => step.kind === 'detected')
  const cycles = escalation.prior_cycles ?? []
  const recurrences = escalation.recurrences ?? cycles.length
  const windowHours = escalation.window_hours ?? 24
  const samples = cycles.reduce((total, cycle) => total + (cycle.samples ?? 0), 0)
  const evidenceLine = String(detection?.evidence?.line ?? '').trim()
  const current = String(detection?.summary ?? '').trim()
  const passed = cycles.filter((cycle) => cycle.outcome === 'passed').length
  return zh ? {
    current: `现场状态 · ${current || `${detection?.subject ?? escalation.subject ?? '目标'} 的故障检测仍成立。`}`,
    evidence: evidenceLine
      ? `检测证据 · ${evidenceLine}`
      : '检测证据 · 当前轮已通过连续检测确认，未执行新的重启动作。',
    history: `已完成排查 · ${windowHours} 小时内记录 ${recurrences} 次复发；此前 ${passed} 轮处置通过回读，共采集 ${samples} 次健康样本。`,
    decision: `处置结论 · 同一动作在 ${windowHours} 小时内已 ${recurrences} 次通过回读后复发，达到复发预算；本轮未再次重启，已升级人工排查持续性原因。`,
    next: `人工检查项 · 核对 ${detection?.subject ?? escalation.subject ?? '该服务'} 的退出码与 journal，检查依赖服务、配置和启动参数在历次恢复后的变化；修复持续性原因并通过回读后解除升级状态。`,
  } : {
    current: `CURRENT STATE · ${current || `The fault remains present on ${detection?.subject ?? escalation.subject ?? 'the target'}.`}`,
    evidence: evidenceLine
      ? `DETECTION EVIDENCE · ${evidenceLine}`
      : 'DETECTION EVIDENCE · The current round passed consecutive detection; no new restart was executed.',
    history: `COMPLETED CHECKS · ${recurrences} recurrences were recorded within ${windowHours} hours; ${passed} earlier actions passed readback across ${samples} health samples.`,
    decision: `RESPONSE DECISION · The same action passed readback and later recurred ${recurrences} times within ${windowHours} hours, exhausting the recurrence budget. This round did not restart the service and is escalated for persistent-cause investigation.`,
    next: `OPERATOR CHECKS · Inspect the exit code and journal for ${detection?.subject ?? escalation.subject ?? 'the service'}, then compare dependency, configuration, and launch-parameter changes after each recovery. Clear escalation after the persistent cause is fixed and readback passes.`,
  }
}

export function RemediationProgress({ subject, lang }: { subject: string; lang: Lang }) {
  const zh = lang === 'zh'
  const steps = useSentinelChain(subject)?.steps ?? null

  // Read the round, not the whole chain: a subject that keeps coming back has
  // several finished rounds behind it, and this panel is about the one now.
  const round = useMemo(() => (steps ? currentRound(steps) : null), [steps])
  const view = useMemo(() => (round ? phaseOf(round) : null), [round])

  if (!round || !round.length || !view) return null

  const samples = [...round].reverse().find((s) => typeof s.samples === 'number')?.samples
  const running = !view.terminal
  const phases: readonly Phase[] = view.terminal === 'no_safe_action' ? REPORT_PHASES : ACTION_PHASES
  const withheld = view.terminal === 'no_safe_action' ? withheldWrite(round, zh) : null
  const recurrence = view.terminal === 'escalated' ? recurrenceContext(round, zh) : null

  return (
    <div className={`rp${running ? ' is-running' : ''}`}>
      <div className="rp-head">
        <span className="rp-k">
          {running
            ? (zh ? '当前处置状态' : 'CURRENT RESPONSE STATUS')
            : (zh ? '处置决策' : 'RESPONSE DECISION')}
        </span>
        <span className="rp-subject">{subject}</span>
        {view.terminal ? (
          <span className={`rp-terminal t-${view.terminal}`}>
            {(TERMINAL_LABEL[view.terminal] ?? [view.terminal, view.terminal])[zh ? 0 : 1]}
          </span>
        ) : (
          <span className="rp-terminal t-running">{zh ? '进行中' : 'IN FLIGHT'}</span>
        )}
      </div>

      <ol className="rp-rail">
        {phases.map((phase) => {
          const done = view.reached.has(phase)
          const now = phase === view.current && running
          return (
            <li key={phase} className={`rp-phase${done ? ' is-done' : ''}${now ? ' is-now' : ''}`}>
              <span className="rp-dot" />
              <span className="rp-lab">{PHASE_LABEL[phase][zh ? 0 : 1]}</span>
              {phase === 'watching' && typeof samples === 'number' && samples > 0 ? (
                <span className="rp-count">{samples} {zh ? '次采样' : 'samples'}</span>
              ) : null}
            </li>
          )
        })}
      </ol>

      {withheld ? (
        <div className="rp-decision">
          <strong>{withheld.action}</strong>
          <p>{withheld.fact}</p>
          <span>{withheld.gate}</span>
          <span>{withheld.result}</span>
          <span>{withheld.owner}</span>
        </div>
      ) : null}

      {recurrence ? (
        <div className="rp-recurrence">
          <strong>{zh ? '故障上下文与排查结论' : 'FAULT CONTEXT & INVESTIGATION FINDINGS'}</strong>
          <span>{recurrence.current}</span>
          <span>{recurrence.evidence}</span>
          <span>{recurrence.history}</span>
          <span>{recurrence.decision}</span>
          <span className="rp-next">{recurrence.next}</span>
        </div>
      ) : null}

      {/* Impact scope and current state remain on the incident marker. */}
      {running && view.current === 'watching' ? (
        <p className="rp-hint">
          {zh
            ? '观察窗持续回读目标与网关；保护指标恶化时触发回退，连续健康后提交闭环结果。'
            : 'The observation window re-reads the target and gateway; guardrail regression triggers rollback, and sustained health closes the incident.'}
        </p>
      ) : null}
    </div>
  )
}
