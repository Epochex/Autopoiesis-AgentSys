import type { InvestigationCaseRecord } from './investigation-case'
import type { CaseSession } from './case-investigation-data'
import type { Chain } from './use-sentinel-chain'

export type NarrativeStageId = 'detect' | 'confirm' | 'investigate' | 'memory' | 'action' | 'verify'

export interface NarrativeStage {
  id: NarrativeStageId
  title: string
  state: 'waiting' | 'active' | 'complete' | 'failed'
  happened: string
  evidence: string
  reason: string
  result: string
}

export interface MemoryImpact {
  applied: boolean
  memoryId?: string
  summary?: string
  sourceSessionId?: string
  matchedOn: string[]
  baselineChecks: number
  executedChecks: number
  savedChecks: number
  baselineSeconds?: number
  executedSeconds?: number
}

type Args = {
  caseRecord: InvestigationCaseRecord
  session: CaseSession | null
  chain: Chain | null
  lang: 'zh' | 'en'
}

const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const strings = (value: unknown): string[] => Array.isArray(value) ? value.map(String) : []
const lastEvent = (record: InvestigationCaseRecord, kind: string) => [...record.timeline].reverse().find(event => event.kind === kind)

export function buildIncidentNarrative({ caseRecord, session, chain, lang }: Args): {
  stages: NarrativeStage[]
  memory: MemoryImpact | null
  audit: { caseRecord: InvestigationCaseRecord; session: CaseSession | null; commands: Chain['commands'] }
} {
  const zh = lang === 'zh'
  const t = (cn: string, en: string) => zh ? cn : en
  const source = object(caseRecord.sourcePayload)
  const memoryDemo = source.demoScenario === 'memory_trajectory'
  const comparison = object(lastEvent(caseRecord, 'memory_comparison_recorded')?.payload ?? lastEvent(caseRecord, 'memory_comparison_recorded'))
  const intervention = object(session?.memory_intervention ?? session?.memory_value)
  const retrieval = (Array.isArray(session?.retrieval_results) ? session?.retrieval_results : [])
    .map(object).find(item => item.kind === 'indexed_memory' && item.selected_for_context !== false)
  const memoryId = String(retrieval?.item_id ?? strings(intervention.memory_ids)[0] ?? comparison.memoryId ?? '') || undefined
  const rawMemorySummary = retrieval?.summary ? String(retrieval.summary) : undefined
  const matchedLabels: Record<string, [string, string]> = {
    query_terms: ['故障描述', 'fault description'], subject: ['同一对象', 'same subject'], family: ['同一故障族', 'same fault family'],
  }
  const baselineOrder = strings(intervention.no_memory_shadow_order)
  const executedOrder = strings(intervention.executed_order)
  const baselineChecks = Number(comparison.baselineChecks ?? baselineOrder.length)
  const executedChecks = Number(comparison.executedChecks ?? executedOrder.length)
  const savedChecks = Number(comparison.savedChecks ?? intervention.saved_probe_count ?? Math.max(0, baselineChecks - executedChecks))
  const memoryApplied = Boolean(intervention.memory_applied ?? comparison.memoryApplied ?? retrieval)
  const memory: MemoryImpact | null = memoryId || baselineChecks ? {
    applied: memoryApplied,
    memoryId,
    summary: rawMemorySummary && zh && /^verified service_failed/i.test(rawMemorySummary) ? '已验证的服务故障检查方法' : rawMemorySummary,
    sourceSessionId: strings(retrieval?.source_trace_ids)[0] ?? (comparison.sourceSessionId ? String(comparison.sourceSessionId) : undefined),
    matchedOn: strings(retrieval?.matched_on).map(item => matchedLabels[item]?.[zh ? 0 : 1] ?? item),
    baselineChecks,
    executedChecks,
    savedChecks,
    baselineSeconds: comparison.baselineRequestSeconds == null ? undefined : Number(comparison.baselineRequestSeconds),
    executedSeconds: comparison.memoryRequestSeconds == null ? undefined : Number(comparison.memoryRequestSeconds),
  } : null

  const steps = chain?.steps ?? []
  const has = (kind: string) => steps.some(step => step.kind === kind) || caseRecord.timeline.some(event => event.kind === kind)
  const caseEvidence = caseRecord.timeline.filter(event => [
    'detection_confirmed', 'evidence_collected', 'remediation_committed',
    'bakein_opened', 'bakein_sampled', 'memory_comparison_recorded',
  ].includes(String(event.kind))).length
  const evidenceCount = session?.evidence?.length || caseRecord.businessDecision?.evidence.length || caseEvidence
  const confirmation = lastEvent(caseRecord, 'detection_confirmed')
  const caseSample = lastEvent(caseRecord, 'bakein_sampled') ?? lastEvent(caseRecord, 'resolved')
  const sample = [...steps].reverse().find(step => step.kind === 'bakein_sampled' || step.kind === 'resolved')
    ?? caseSample
  const resolved = caseRecord.status === 'resolved' || has('resolved')
  const failed = caseRecord.status === 'escalated' || has('escalated') || has('bakein_regressed')
  const detectedComplete = has('detected') || Boolean(source.incidentFacts) || caseRecord.occurrenceCount > 0
  const confirmed = has('detection_confirmed') || has('preflight') || Boolean(session)
  const investigated = Boolean(session) || evidenceCount > 0 || has('preflight')
  const acted = has('remediated') || has('remediation_committed') || Boolean(caseRecord.businessDecision?.action)
  const memoryState: NarrativeStage['state'] = memory ? 'complete' : investigated ? 'complete' : 'waiting'

  const stages: NarrativeStage[] = [
    {
      id: 'detect', title: t('发现异常', 'Detect anomaly'), state: detectedComplete ? 'complete' : 'active',
      happened: caseRecord.summary,
      evidence: t(`监测规则 ${caseRecord.ruleId} 命中 ${caseRecord.subject}`, `Rule ${caseRecord.ruleId} matched ${caseRecord.subject}`),
      reason: t('异常达到建案条件，需要进入连续确认。', 'The observation met the case-opening threshold and required confirmation.'),
      result: t('事件已建案并进入同一条处理链。', 'A case was opened on the shared incident path.'),
    },
    {
      id: 'confirm', title: t('连续确认', 'Confirm continuously'), state: confirmed ? 'complete' : detectedComplete ? 'active' : 'waiting',
      happened: t('连续采样，排除一次性抖动。', 'Repeated samples ruled out a one-off fluctuation.'),
      evidence: t(`${Math.max(1, Number(confirmation?.samples ?? sample?.samples ?? caseRecord.occurrenceCount))} 次观测指向同一对象。`, `${Math.max(1, Number(confirmation?.samples ?? sample?.samples ?? caseRecord.occurrenceCount))} observations pointed to the same subject.`),
      reason: t('只有持续成立的故障才推进调查和处置。', 'Only a persistent condition advances to investigation and action.'),
      result: confirmed ? t('异常连续成立，确认进入调查。', 'Persistence confirmed; investigation started.') : t('正在等待下一次样本。', 'Waiting for the next sample.'),
    },
    {
      id: 'investigate', title: t('调查与证据', 'Investigate and collect evidence'), state: investigated ? 'complete' : confirmed ? 'active' : 'waiting',
      happened: t('围绕当前对象收集最小必要证据。', 'Collected the minimum evidence needed for this subject.'),
      evidence: t(`本案保存 ${evidenceCount} 项判定证据。`, `${evidenceCount} decision evidence items were saved for this case.`),
      reason: t('证据只服务于本案结论和下一步动作。', 'Evidence is limited to the decision and next action for this case.'),
      result: investigated ? t('形成可执行判断。', 'An actionable finding was produced.') : t('调查尚未形成结论。', 'The investigation has not yet reached a finding.'),
    },
    {
      id: 'memory', title: t('记忆介入', 'Apply memory'), state: memoryState,
      happened: memory?.applied ? t(`召回历史处理记录：${memory.summary ?? memory.memoryId}`, `Recalled a prior handling record: ${memory.summary ?? memory.memoryId}`) : t('检索与本案对象、故障族相符的历史记录。', 'Searched for history matching the subject and fault family.'),
      evidence: memory ? t(`来源于历史调查 ${memory.sourceSessionId ?? '待记录'}，匹配 ${memory.matchedOn.join('、') || '对象与故障族'}。`, `Source investigation ${memory.sourceSessionId ?? 'pending'}; matched ${memory.matchedOn.join(', ') || 'subject and fault family'}.`) : t('本案没有采用可复用记忆。', 'No reusable memory was adopted for this case.'),
      reason: memory?.applied ? t('历史结论已验证且适用条件与本案一致。', 'The prior result was verified and its applicability matched this case.') : t('保持完整检查路径。', 'The full inspection path remains in use.'),
      result: memory ? t(`已采用，检查 ${memory.baselineChecks}→${memory.executedChecks}，减少 ${memory.savedChecks} 项。`, `Adopted; checks ${memory.baselineChecks}→${memory.executedChecks}, saving ${memory.savedChecks}.`) : t('继续按标准路径调查。', 'Continued on the standard investigation path.'),
    },
    {
      id: 'action', title: t('执行处置', 'Execute action'), state: failed ? 'failed' : acted ? 'complete' : investigated ? 'active' : 'waiting',
      happened: caseRecord.businessDecision?.disposition || (memoryDemo ? t('形成记忆引导的调查结论。', 'Produced a memory-guided investigation finding.') : t('根据已确认结论执行最小处置。', 'Executed the smallest action supported by the finding.')),
      evidence: caseRecord.businessDecision?.headline || t('处置前检查与调查证据支持该动作。', 'Pre-action checks and investigation evidence supported the action.'),
      reason: memoryDemo ? t('本用例验证调查提速，结论通过同一案件链保存。', 'This use case validates investigation acceleration and saves the finding on the same case chain.') : t('动作受审批、安全门和回滚条件约束。', 'The action is constrained by approval, safety gates, and rollback conditions.'),
      result: failed ? t('处置未通过验证，已转人工处理。', 'The action failed verification and was escalated.') : acted ? (memoryDemo ? t('调查结论已保存，进入结果核对。', 'The investigation finding was saved for result verification.') : t('动作已执行，进入持续回读。', 'The action ran and continuous readback started.')) : t('等待处置条件满足。', 'Waiting for action conditions.'),
    },
    {
      id: 'verify', title: t('持续回读', 'Verify continuously'), state: failed ? 'failed' : resolved ? 'complete' : acted ? 'active' : 'waiting',
      happened: memoryDemo ? t('核对召回来源、工作集检查点、检查顺序和持久化结果。', 'Verified recall provenance, working-set checkpoints, probe order, and persistence.') : t('持续检查目标状态、保护性业务检查和管理连接。', 'Continuously checked target state, protected business checks, and management connectivity.'),
      evidence: memoryDemo && memory ? t(`两轮实际检查 ${memory.baselineChecks}→${memory.executedChecks}，来源调查已绑定。`, `Executed checks ${memory.baselineChecks}→${memory.executedChecks}; source investigation is bound.`) : sample ? t(`最新回读：目标恢复 ${sample.target_recovered === false ? '未通过' : '通过'}。`, `Latest readback: target recovery ${sample.target_recovered === false ? 'failed' : 'passed'}.`) : t('等待保存回读样本。', 'Waiting for saved readback samples.'),
      reason: memoryDemo ? t('来源、顺序和磁盘记录全部一致后才确认记忆收益。', 'Memory impact is accepted only when provenance, ordering, and persisted records agree.') : t('只有连续回读通过才能关闭事件。', 'The case closes only after continuous readback passes.'),
      result: resolved ? (memoryDemo ? t('两轮对比通过，记忆收益已保存。', 'The comparison passed and memory impact was saved.') : t('恢复已验证，事件闭环。', 'Recovery was verified and the case closed.')) : failed ? t('回读失败，保留全部证据并升级。', 'Readback failed; evidence was retained and the case escalated.') : t('仍在观察窗口。', 'The observation window is still active.'),
    },
  ]

  return { stages, memory, audit: { caseRecord, session, commands: chain?.commands ?? [] } }
}
