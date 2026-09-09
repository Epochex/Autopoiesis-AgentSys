import { useEffect, useState } from 'react'
import type { Lang } from '../i18n'

interface NextAction {
  activity?: string
  probability?: number
  count?: number
}

interface TrajectoryAnalysis {
  current_state?: { last_activity?: string | null }
  top_next_actions?: NextAction[]
  unseen_transition?: { unseen?: boolean }
  cycle_rate?: number
  failure_risk?: {
    failure_signal?: string
    risk_score?: number | null
    calibrated?: boolean
    state_session_samples?: number
  }
}

interface TrajectoryResponse {
  analysis?: TrajectoryAnalysis
  sessionCount?: number
  eventCount?: number
}

const ACTIVITY_ZH: Record<string, string> = {
  working_set_updated: '更新调查工作集',
  session_opened: '建立调查任务',
  probe_selected: '选定核验项',
  discriminating_probe_selected: '选定区分核验',
  tool_called: '执行只读采集',
  probe_round_completed: '核验结果入账',
  hypothesis_confirmed: '确认根因候选',
  hypothesis_rejected: '排除根因候选',
  verifier_result: '回读处置结果',
  business_decision_recorded: '记录处置决定',
  memory_intervention_evaluated: '评估记忆介入',
  verified_procedure_indexed: '登记已验证处置步骤',
  memory_candidates_ranked: '排序历史记录候选',
  retrieval_candidates_filtered: '筛选可用历史记录',
  operational_memory_recalled: '读取相关处置记录',
  knowledge_retrieved: '读取设备参考资料',
  disposition_recorded: '归档调查结论',
}

const activityLabel = (value: string | null | undefined, zh: boolean) => {
  if (!value) return zh ? '等待本案轨迹' : 'AWAITING CASE TRAJECTORY'
  return zh ? ACTIVITY_ZH[value] ?? value.replaceAll('_', ' ') : value.replaceAll('_', ' ').toUpperCase()
}

export function InvestigationTrajectorySignal({ caseId, lang }: { caseId: string | null; lang: Lang }) {
  const [report, setReport] = useState<TrajectoryResponse | null>(null)
  const zh = lang === 'zh'

  useEffect(() => {
    if (!caseId) return
    let active = true
    const load = async () => {
      setReport(null)
      try {
        const response = await fetch(`/api/rca/investigate/trajectory-fsm?case_id=${encodeURIComponent(caseId)}`)
        if (!response.ok) {
          if (active) setReport(null)
          return
        }
        const payload = await response.json() as TrajectoryResponse
        if (active) setReport(payload)
      } catch {
        if (active) setReport(null)
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 15_000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [caseId])

  const analysis = caseId ? report?.analysis : null
  if (!analysis) {
    return <div className="ifb-fsm is-waiting">{zh ? '等待本案轨迹' : 'AWAITING CASE TRAJECTORY'}</div>
  }

  const next = analysis.top_next_actions?.[0]
  const cycleRate = typeof analysis.cycle_rate === 'number' ? analysis.cycle_rate : null
  const samples = analysis.failure_risk?.state_session_samples
  return (
    <div className="ifb-fsm" aria-label={zh ? '本案调查轨迹' : 'Case investigation trajectory'}>
      <span>{zh ? '轨迹位置' : 'CURRENT'} <b>{activityLabel(analysis.current_state?.last_activity, zh)}</b></span>
      <span>{zh ? '历史同类下一步' : 'HISTORICAL NEXT'} <b>{activityLabel(next?.activity, zh)}</b>{typeof next?.probability === 'number' ? ` · ${Math.round(next.probability * 100)}%` : ''}</span>
      {cycleRate != null ? <span>{zh ? '重复状态占比' : 'REPEATED-STATE RATE'} <b>{Math.round(cycleRate * 100)}%</b></span> : null}
      {analysis.unseen_transition?.unseen ? <em>{zh ? '本次转移在历史记录中未出现' : 'TRANSITION UNSEEN IN HISTORY'}</em> : null}
      {analysis.failure_risk?.calibrated === false ? (
        <em>{zh ? `轨迹偏离提示只参与人工排序${typeof samples === 'number' ? ` · ${samples} 个历史样本` : ''}` : `TRAJECTORY DEVIATION SIGNAL FOR HUMAN RANKING${typeof samples === 'number' ? ` · ${samples} SAMPLES` : ''}`}</em>
      ) : null}
    </div>
  )
}
