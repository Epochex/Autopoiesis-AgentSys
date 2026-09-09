import { useMemo, useState } from 'react'
import { ExecutionProgress } from './ExecutionProgress'
import type { Lang } from '../i18n'
import { stageIdForEvent } from './autopoiesis-pipeline'
import type { InvestigationCaseRecord } from './investigation-case'
import type { Chain, CommandLine } from './use-sentinel-chain'

export interface DrillStage {
  id: string
  zh: string
  en: string
  status: 'completed' | 'current' | 'pending' | 'failed' | 'paused'
}

interface DrillRecord {
  id: string
  stageId: string
  kind: string
  ts: string
  firstTs: string
  count: number
  source: 'case' | 'sentinel' | 'command' | 'source'
  raw: Record<string, unknown>
}

interface DrillField {
  key: string
  value: unknown
}

const clock = (value: string) => value.replace('T', ' ').replace(/\.\d+(?:Z|[+-]\d\d:\d\d)?$/, '').replace('Z', '')

const KIND_LABEL: Record<string, [string, string]> = {
  source_observation: ['当前源事实', 'CURRENT SOURCE FACT'],
  detected: ['检测命中', 'DETECTION'],
  awaiting_confirmation: ['等待连续确认', 'AWAITING CONFIRMATION'],
  preflight: ['前置校验', 'PREFLIGHT'],
  no_safe_action: ['写操作未授权', 'WRITE NOT AUTHORIZED'],
  escalated: ['转人工处置', 'OPERATOR HANDOFF'],
  business_decision_recorded: ['案件决定', 'CASE DECISION'],
  investigation_session_started: ['调查会话', 'INVESTIGATION SESSION'],
  evidence_collected: ['调查证据', 'EVIDENCE'],
  retrieval_completed: ['历史检索', 'RETRIEVAL'],
  remediation_started: ['动作开始', 'ACTION STARTED'],
  remediation_committed: ['动作提交', 'ACTION COMMITTED'],
  remediated: ['动作结果', 'ACTION RESULT'],
  bakein_opened: ['观察窗开启', 'WATCH OPENED'],
  bakein_sampled: ['观察采样', 'WATCH SAMPLE'],
  bakein_passed: ['观察通过', 'WATCH PASSED'],
  resolved: ['恢复验证', 'RECOVERY VERIFIED'],
  command: ['命令记录', 'COMMAND'],
}

const labelOf = (kind: string, zh: boolean) => KIND_LABEL[kind]?.[zh ? 0 : 1] ?? kind

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

function signature(kind: string, raw: Record<string, unknown>): string {
  if (kind === 'business_decision_recorded' && isObject(raw.decision)) {
    const decision = raw.decision
    const evidence = Array.isArray(decision.evidence)
      ? decision.evidence.map((item) => isObject(item) ? item.value : item)
      : []
    return JSON.stringify([
      kind,
      decision.state,
      decision.classification,
      decision.action,
      decision.disposition,
      evidence,
    ])
  }
  if (kind === 'bakein_sampled') return JSON.stringify([kind, raw.outcome, raw.summary])
  return JSON.stringify([kind, raw.action, raw.reason, raw.outcome, raw.summary, raw.note, raw.argv, raw.rc, raw.out])
}

function recordsFrom(record: InvestigationCaseRecord | null, chain: Chain | null): DrillRecord[] {
  const rows: DrillRecord[] = []
  if (record?.sourcePayload) {
    rows.push({
      id: `${record.caseId}:source`,
      stageId: 'detector',
      kind: 'source_observation',
      ts: record.lastSeenAt,
      firstTs: record.firstSeenAt,
      count: Math.max(1, record.occurrenceCount),
      source: 'source',
      raw: record.sourcePayload,
    })
  }
  for (const [index, item] of (record?.timeline ?? []).entries()) {
    const kind = String(item.kind ?? '')
    const stageId = stageIdForEvent(kind, item)
    if (!stageId) continue
    const ts = String(item.ts ?? record?.updatedAt ?? '')
    rows.push({
      id: `case:${String(item.eventId ?? index)}`,
      stageId,
      kind,
      ts,
      firstTs: String(item.firstRecordedAt ?? ts),
      count: Math.max(1, Number(item.equivalentRecordCount ?? 1)),
      source: 'case',
      raw: item,
    })
  }
  const steps = [...(chain?.steps ?? [])].sort((a, b) => a.at.localeCompare(b.at))
  for (const [index, item] of steps.entries()) {
    const raw = item as unknown as Record<string, unknown>
    const stageId = stageIdForEvent(item.kind, raw)
    if (!stageId) continue
    rows.push({ id: `sentinel:${item.at}:${index}`, stageId, kind: item.kind, ts: item.at, firstTs: item.at, count: 1, source: 'sentinel', raw })
  }
  let stepIndex = 0
  let stageId = steps.length ? stageIdForEvent(steps[0].kind, steps[0] as unknown as Record<string, unknown>) : 'detector'
  for (const [index, command] of [...(chain?.commands ?? [])].sort((a, b) => a.at.localeCompare(b.at)).entries()) {
    while (stepIndex + 1 < steps.length && steps[stepIndex + 1].at <= command.at) {
      stepIndex += 1
      stageId = stageIdForEvent(steps[stepIndex].kind, steps[stepIndex] as unknown as Record<string, unknown>) ?? stageId
    }
    rows.push({
      id: `command:${command.at}:${index}`,
      stageId: stageId ?? 'detector',
      kind: 'command',
      ts: command.at,
      firstTs: command.at,
      count: 1,
      source: 'command',
      raw: command as unknown as Record<string, unknown>,
    })
  }

  rows.sort((a, b) => a.ts.localeCompare(b.ts))
  const compact: DrillRecord[] = []
  const byMeaning = new Map<string, DrillRecord>()
  for (const row of rows) {
    const key = `${row.stageId}:${row.source}:${signature(row.kind, row.raw)}`
    const prior = byMeaning.get(key)
    if (!prior) {
      const next = { ...row }
      compact.push(next)
      byMeaning.set(key, next)
      continue
    }
    prior.count += row.count
    prior.ts = row.ts
    prior.raw = row.raw
  }
  return compact.sort((a, b) => b.ts.localeCompare(a.ts))
}

function fieldsOf(raw: Record<string, unknown>): DrillField[] {
  const out: DrillField[] = []
  for (const [key, value] of Object.entries(raw)) {
    if (['kind', 'ts', 'eventId'].includes(key)) continue
    if (isObject(value)) {
      for (const [child, nested] of Object.entries(value)) out.push({ key: `${key}.${child}`, value: nested })
    } else {
      out.push({ key, value })
    }
  }
  return out
}

function valuePreview(value: unknown): string {
  if (value == null || value === '') return 'N/A'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return `${value.length} items`
  return `${Object.keys(value as Record<string, unknown>).length} fields`
}

function recordPreview(row: DrillRecord, zh: boolean): string {
  if (row.kind === 'command') {
    const command = row.raw as unknown as CommandLine
    return `$ ${(command.argv ?? []).join(' ')}${command.rc != null ? ` · exit ${command.rc}` : zh ? ' · 执行中' : ' · Running'}`
  }
  if (isObject(row.raw.decision)) {
    return String(row.raw.decision.headline ?? row.raw.decision.disposition ?? row.raw.decision.state ?? row.kind)
  }
  return String(row.raw.summary ?? row.raw.reason ?? row.raw.note ?? labelOf(row.kind, zh))
}

export function CaseTrajectoryDrilldown({
  lang,
  caseId,
  record,
  chain,
  stages,
  selectedStage,
  onSelectStage,
  onOpenCase,
  error,
  subject,
  controlled = false,
}: {
  lang: Lang
  caseId?: string
  record: InvestigationCaseRecord | null
  chain: Chain | null
  stages: DrillStage[]
  selectedStage: string | null
  onSelectStage: (stageId: string) => void
  onOpenCase?: () => void
  error?: string | null
  subject?: string
  controlled?: boolean
}) {
  const [panel, setPanel] = useState<'execution' | 'records'>('execution')
  const executionEnabled = controlled || chain !== null
  const zh = lang === 'zh'
  const allRecords = useMemo(() => recordsFrom(record, chain), [record, chain])
  const visible = useMemo(() => allRecords.filter((item) => item.stageId === selectedStage), [allRecords, selectedStage])
  const [recordChoice, setRecordChoice] = useState<{ stageId: string; id: string } | null>(null)
  const [fieldChoice, setFieldChoice] = useState<{ recordId: string; key: string } | null>(null)
  const recordId = recordChoice?.stageId === selectedStage ? recordChoice.id : null
  const selectedRecord = visible.find((item) => item.id === recordId) ?? null
  const fields = useMemo(() => fieldsOf(selectedRecord?.raw ?? {}), [selectedRecord])
  const fieldKey = fieldChoice?.recordId === recordId ? fieldChoice.key : null
  const selectedField = fields.find((item) => item.key === fieldKey) ?? null

  return (
    <div className="th-drill">
      <header className="th-drill-head">
        <div><small>{executionEnabled ? (zh ? '执行过程 · 实时证据' : 'EXECUTION · LIVE EVIDENCE') : (zh ? '事件长轨迹 · 逐层下钻' : 'LONG TRAJECTORY · DRILL DOWN')}</small><b>{record?.subject ?? subject ?? caseId ?? (zh ? '实时事件' : 'LIVE EVENT')}</b></div>
        {caseId && onOpenCase ? <button type="button" onClick={onOpenCase}>{zh ? '进入案件调查' : 'OPEN CASE'}</button> : null}
      </header>
      <div className="th-drill-meta">
        <code>{caseId ?? (controlled ? (zh ? '受控演示 · 事件账本' : 'CONTROLLED DEMO · EVENT LEDGER') : (zh ? '实时事件账本' : 'LIVE EVENT LEDGER'))}</code>
        <span>{record ? `${clock(record.firstSeenAt)} → ${clock(record.lastSeenAt)}` : (error ?? (executionEnabled ? (zh ? '持续同步当前周期执行账本' : 'Continuously syncing the current execution cycle') : (zh ? '案件记录加载中' : 'LOADING CASE RECORD')))}</span>
      </div>

      {executionEnabled && <nav className="execution-tabs"><button aria-pressed={panel==='execution'} onClick={()=>setPanel('execution')}>{zh ? '执行进度与结果' : 'Execution progress and results'}</button><button aria-pressed={panel==='records'} onClick={()=>setPanel('records')}>{zh ? '逐层查看原始记录' : 'Inspect raw records'}</button></nav>}
      {executionEnabled && panel === 'execution' ? <ExecutionProgress chain={chain} lang={lang}/> : <>
      <nav className="th-drill-crumb" aria-label={zh ? '下钻层级' : 'Drilldown levels'}>
        <b>01 {zh ? '环节' : 'STAGE'}</b><span>›</span><b className={selectedRecord ? '' : 'is-muted'}>02 {zh ? '记录' : 'RECORD'}</b><span>›</span><b className={selectedField ? '' : 'is-muted'}>03 {zh ? '字段' : 'FIELD'}</b><span>›</span><b className={selectedField ? '' : 'is-muted'}>04 {zh ? '原始值' : 'RAW VALUE'}</b>
      </nav>

      <div className="th-drill-stages">
        {stages.map((stage) => {
          const count = allRecords.filter((item) => item.stageId === stage.id).length
          return <button key={stage.id} type="button" className={`is-${stage.status}${selectedStage === stage.id ? ' is-selected' : ''}`} onClick={() => onSelectStage(stage.id)}><span>{zh ? stage.zh : stage.en}</span><small>{count}</small></button>
        })}
      </div>

      <div className="th-drill-body">
        <section className="th-drill-col">
          <h3>02 · {zh ? '该环节的记录' : 'RECORDS IN THIS STAGE'} <small>{visible.length}</small></h3>
          <div className="th-drill-list">
            {visible.map((item) => <button key={item.id} type="button" className={recordId === item.id ? 'is-selected' : ''} onClick={() => setRecordChoice({ stageId: item.stageId, id: item.id })}><span><b>{labelOf(item.kind, zh)}</b><time>{clock(item.ts)}</time></span><p>{recordPreview(item, zh)}</p>{item.count > 1 ? <small>{item.source === 'source' ? (zh ? `关联源记录 ${item.count} 条` : `${item.count} LINKED SOURCE RECORDS`) : (zh ? `相同记录 ${item.count} 条 · ${clock(item.firstTs)} 起` : `${item.count} matching records · since ${clock(item.firstTs)}`)}</small> : null}</button>)}
            {!visible.length ? <p className="th-drill-empty">{zh ? '该环节没有已落库记录' : 'NO LANDED RECORDS FOR THIS STAGE'}</p> : null}
          </div>
        </section>

        <section className="th-drill-col">
          <h3>03 · {zh ? '记录字段' : 'RECORD FIELDS'} <small>{fields.length}</small></h3>
          <div className="th-drill-fields">
            {fields.map((item) => <button key={item.key} type="button" className={fieldKey === item.key ? 'is-selected' : ''} onClick={() => recordId && setFieldChoice({ recordId, key: item.key })}><code>{item.key}</code><span>{valuePreview(item.value)}</span></button>)}
            {!selectedRecord ? <p className="th-drill-empty">{zh ? '选择左侧记录' : 'SELECT A RECORD'}</p> : null}
          </div>
        </section>
      </div>

      <section className="th-drill-raw">
        <h3>04 · {selectedField?.key ?? (zh ? '原始值' : 'RAW VALUE')}</h3>
        {selectedField ? <pre>{typeof selectedField.value === 'string' ? selectedField.value : JSON.stringify(selectedField.value, null, 2)}</pre> : <p>{zh ? '选择字段后显示原始值' : 'SELECT A FIELD TO VIEW ITS RAW VALUE'}</p>}
      </section>
      </>}
    </div>
  )
}
