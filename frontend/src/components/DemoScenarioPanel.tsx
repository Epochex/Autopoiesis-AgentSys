import { useEffect, useMemo, useState } from 'react'
import type { Lang } from '../i18n'
import type { InvestigationSourceEventInput } from './investigation-context'

type Scenario = 'self_healing' | 'memory_trajectory'

interface DemoRun {
  runId: string
  scenario: Scenario
  status: 'running' | 'passed' | 'failed'
  phase?: string
  changeId?: string
  startedAt: string
  completedAt: string
  subject: string
  headline: string
  caseId?: string | null
  sessionId?: string | null
  metrics?: Record<string, unknown>
  steps?: string[]
}

interface DemoResponse {
  latest?: Partial<Record<Scenario, DemoRun>>
}

const metricNumber = (run: DemoRun | undefined, key: string): number => {
  const value = run?.metrics?.[key]
  return typeof value === 'number' ? value : Number(value) || 0
}

const clock = (value?: string) => {
  if (!value) return 'N/A'
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString()
}

export function DemoScenarioPanel({
  lang,
  onOpenCase,
  onOpenTrace,
}: {
  lang: Lang
  onOpenCase: (subject: string, caseId: string, sourceEvent?: InvestigationSourceEventInput) => void
  onOpenTrace: (subject: string, caseId?: string) => void
}) {
  const zh = lang === 'zh'
  const [latest, setLatest] = useState<Partial<Record<Scenario, DemoRun>>>({})

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const response = await fetch('/api/rca/demo/runs?limit=20', { headers: { Accept: 'application/json' } })
        if (!response.ok) return
        const payload = await response.json() as DemoResponse
        if (active) setLatest(payload.latest ?? {})
      } catch {
        // A missing receipt endpoint does not hide production events.
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 3_000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [])

  const rows = useMemo(() => {
    const selfHealing = latest.self_healing
    const memory = latest.memory_trajectory
    return [
      {
        key: 'self_healing' as const,
        number: '01',
        title: zh ? '服务自愈与结果回读' : 'SERVICE SELF-HEALING AND READBACK',
        command: './scripts/demo_self_healing.py',
        run: selfHealing,
        proof: selfHealing
          ? (zh
              ? `连续 ${metricNumber(selfHealing, 'confirmationPolls')} 次检测确认，执行 restart_unit，完成 ${metricNumber(selfHealing, 'readbackSamples')} 次实时回读。`
              : `${metricNumber(selfHealing, 'confirmationPolls')} consecutive detections, restart_unit, ${metricNumber(selfHealing, 'readbackSamples')} live readbacks.`)
          : (zh ? '展示连续检测、变更 Diff、一次性授权、重启、持续回读和恢复结论。' : 'Shows repeated detection, diff, scoped authorization, restart and sustained readback.'),
      },
      {
        key: 'memory_trajectory' as const,
        number: '02',
        title: zh ? '记忆提速与长轨迹' : 'MEMORY ACCELERATION AND LONG TRAJECTORY',
        command: './scripts/demo_memory_trajectory.py',
        run: memory,
        proof: memory
          ? (zh
              ? (memory.metrics?.baselineExecutedProbes !== undefined ? `无记忆实际检查 ${metricNumber(memory, 'baselineExecutedProbes')} 项 → 有记忆 ${metricNumber(memory, 'executedProbes')} 项，实际减少 ${metricNumber(memory, 'actualSavedProbes')} 项。轨迹 ${metricNumber(memory, 'trajectoryActivities')} 个活动。` : `候选检查 ${metricNumber(memory, 'candidateProbes')} 项，本次执行 ${metricNumber(memory, 'executedProbes')} 项；历史回执未记录两轮实际对比。`)
              : `${metricNumber(memory, 'candidateProbes')} candidates, ${metricNumber(memory, 'executedProbes')} executed, ${metricNumber(memory, 'savedProbes')} saved, ${metricNumber(memory, 'trajectoryActivities')} trajectory activities.`)
          : (zh ? '脚本先完成一次无记忆调查，再用已验证记录重跑同类案件并计算实际少做的检查。' : 'Runs once without memory, then repeats with the verified procedure and measures work avoided.'),
      },
    ]
  }, [latest, zh])

  return (
    <section className="sa-demo" aria-label={zh ? '答辩演示验收' : 'Defence demonstration acceptance'}>
      <div className="sa-demo-head">
        <div><span>{zh ? '答辩演示 · 可重复验收' : 'DEFENCE DEMOS · REPEATABLE ACCEPTANCE'}</span><small>{zh ? '受控演示记录不计入生产案件指标' : 'CONTROLLED RECORDS ARE EXCLUDED FROM PRODUCTION COUNTERS'}</small></div>
      </div>
      <div className="sa-demo-ledger">
        <div className="sa-demo-columns" aria-hidden="true">
          <span>{zh ? '状态' : 'STATE'}</span>
          <span>{zh ? '演示能力' : 'CAPABILITY'}</span>
          <span>{zh ? '本次实测' : 'MEASURED RECEIPT'}</span>
          <span>{zh ? '触发命令' : 'TRIGGER'}</span>
          <span>{zh ? '查看链路' : 'OPEN CHAIN'}</span>
        </div>
        {rows.map((row) => (
          <article key={row.key} className={`sa-demo-row ${row.run ? `is-${row.run.status}` : 'is-idle'}`}>
            <div className="sa-demo-state"><span>{row.number}</span><em>{row.run ? (row.run.status === 'running' ? '进行中' : row.run.status === 'passed' ? (zh ? '已通过' : 'PASSED') : (zh ? '未通过' : 'FAILED')) : (zh ? '待触发' : 'READY')}</em></div>
            <div className="sa-demo-name"><b>{row.title}</b><small>{row.run ? `${clock(row.run.completedAt)} · ${row.run.subject}` : (zh ? '在项目根目录执行' : 'RUN FROM THE REPOSITORY ROOT')}</small></div>
            <p>{row.run?.status === 'running' || row.run?.status === 'failed' ? row.run.phase ?? row.run.headline : row.proof}</p>
            <code>{row.command}</code>
            <footer>
              {row.run && row.key === 'self_healing' ? (
                <button type="button" disabled={row.run.status === 'running' && !row.run.changeId} onClick={() => onOpenTrace(row.run!.subject, row.run!.caseId ?? undefined)}>{zh ? '处置轨迹' : 'RESPONSE TRACE'} →</button>
              ) : null}
              {row.run?.caseId && row.key === 'memory_trajectory' ? (
                <button type="button" onClick={() => onOpenCase(row.run!.subject, row.run!.caseId!, {
                  id: row.run!.runId,
                  kind: 'controlled_demo',
                  observedAt: row.run!.completedAt,
                  summary: row.run!.headline,
                  severity: row.run!.status === 'passed' ? 'high' : 'critical',
                })}>{zh ? '记忆与轨迹' : 'MEMORY + TRACE'} →</button>
              ) : null}
              {!row.run || (row.key === 'memory_trajectory' && !row.run.caseId) ? <span>{zh ? '等待演示回执' : 'WAITING FOR RECEIPT'}</span> : null}
            </footer>
          </article>
        ))}
      </div>
    </section>
  )
}
