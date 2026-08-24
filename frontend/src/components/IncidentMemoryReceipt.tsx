import { useEffect, useMemo, useState } from 'react'
import './incident-memory-receipt.css'

type Influence = {
  kind: string
  at: string
  subject: string
  what_changed: string
}

type MemoryRow = {
  memory_id: string
  tier: string
  quarantined: boolean
  source_trace_ids: number
  influence_count: number
  subsequent_influence_count: number
  latest_influence?: Influence | null
}

type OperationalRow = {
  id: string
  title: string
  status: string
  evidence_count?: number
}

type Receipt = {
  ok: boolean
  subject: string
  durable: boolean
  lifecycle: 'awaiting_terminal' | 'dossier_recorded' | 'memory_committed' | 'safety_gated' | 'reused'
  latest_incident: {
    completed: boolean
    terminal_kind: string | null
    dossier_id: string | null
    source_trace_id: string | null
  }
  dossiers: OperationalRow[]
  risks: OperationalRow[]
  features: OperationalRow[]
  current_memories: MemoryRow[]
  related_memories: MemoryRow[]
  influence_count: number
  subsequent_influence_count: number
}

const lifecycleLabel = (value: Receipt['lifecycle'], zh: boolean): string => ({
  awaiting_terminal: zh ? '等待本轮形成终态' : 'AWAITING TERMINAL STATE',
  dossier_recorded: zh ? '事件档案已持久化' : 'DOSSIER PERSISTED',
  memory_committed: zh ? '在线记忆已写入' : 'ONLINE MEMORY COMMITTED',
  safety_gated: zh ? '安全门决策已记忆' : 'SAFETY DECISION RETAINED',
  reused: zh ? '记忆已被后续调查引用' : 'MEMORY USED BY A LATER INVESTIGATION',
})[value]

const tierLabel = (tier: string, zh: boolean): string => ({
  episodic: zh ? '事件记忆' : 'EPISODIC',
  semantic: zh ? '模式记忆' : 'SEMANTIC',
  procedural: zh ? '处置程序' : 'PROCEDURAL',
  asset_profile: zh ? '资产画像' : 'ASSET PROFILE',
})[tier] ?? tier

export function IncidentMemoryReceipt({
  subject,
  incidentRef,
  zh,
}: {
  subject: string
  incidentRef?: string | null
  zh: boolean
}) {
  const [data, setData] = useState<Receipt | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    if (!subject) return
    let gone = false
    let timer: number | undefined
    const load = async () => {
      const query = new URLSearchParams({ subject })
      if (incidentRef) query.set('incident_ref', incidentRef)
      try {
        const response = await fetch(`/api/rca/event-memory-receipt?${query}`, {
          headers: { Accept: 'application/json' },
        })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const payload = await response.json() as Receipt
        if (gone) return
        setData(payload)
        setError(false)
      } catch {
        if (!gone) setError(true)
      }
      if (!gone) timer = window.setTimeout(load, 5000)
    }
    void load()
    return () => {
      gone = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [incidentRef, subject])

  const recalled = useMemo(
    () => data?.current_memories.filter((row) => row.subsequent_influence_count > 0) ?? [],
    [data],
  )
  const dossier = data?.dossiers[0] ?? null
  const safetyOnly = data?.latest_incident.terminal_kind === 'no_safe_action'

  return (
    <section className="imr" aria-label={zh ? '本次事件记忆回执' : 'Incident memory receipt'}>
      <header className="imr-head">
        <div>
          <span>{zh ? '真实事件 → 在线记忆' : 'LIVE INCIDENT → ONLINE MEMORY'}</span>
          <b>{zh ? '本次事件记忆回执' : 'INCIDENT MEMORY RECEIPT'}</b>
        </div>
        <span className={`imr-state ${data?.lifecycle ?? 'loading'}`}>
          {data ? lifecycleLabel(data.lifecycle, zh) : error ? (zh ? '回执暂不可读' : 'RECEIPT UNAVAILABLE') : (zh ? '正在核对' : 'CHECKING')}
        </span>
      </header>

      <div className="imr-steps">
        <article className="done">
          <span>01</span><b>{zh ? '事件证据' : 'INCIDENT EVIDENCE'}</b>
          <p>{subject}</p>
          <code>{incidentRef ?? (zh ? '旧记录按对象关联' : 'LEGACY SUBJECT MATCH')}</code>
        </article>
        <article className={dossier ? 'done' : ''}>
          <span>02</span><b>IncidentDossier</b>
          <p>{dossier ? `${dossier.status} · ${dossier.evidence_count ?? 0} ${zh ? '条证据' : 'evidence'}` : (zh ? '等待链路终态' : 'WAITING FOR TERMINAL STATE')}</p>
          <code>{dossier?.id ?? '∅'}</code>
        </article>
        <article className={data?.current_memories.length ? 'done' : ''}>
          <span>03</span><b>{zh ? '在线记忆写入' : 'ONLINE MEMORY WRITE'}</b>
          {data?.current_memories.length ? data.current_memories.map((row) => (
            <p key={row.memory_id}>
              {tierLabel(row.tier, zh)} · <code>{row.memory_id}</code>
              <small>{zh ? '来源轨迹' : 'SOURCE TRACES'} {row.source_trace_ids} · {data.durable ? 'PostgreSQL' : (zh ? '进程内' : 'IN-PROCESS')}</small>
            </p>
          )) : <p>{safetyOnly
            ? (zh ? '安全门决定仅形成事件记忆，不生成成功动作经验' : 'SAFETY DECISION RETAINED; NO SUCCESSFUL ACTION KNOWLEDGE')
            : (zh ? '恢复与回读通过后写入' : 'WRITTEN AFTER RECOVERY READBACK PASSES')}</p>}
        </article>
        <article className={recalled.length ? 'done' : ''}>
          <span>04</span><b>{zh ? '后续引用' : 'LATER USE'}</b>
          <p>{recalled.length
            ? `${recalled.length} ${zh ? '条本轮记忆已被后续调查引用' : 'MEMORIES FROM THIS ROUND WERE USED LATER'}`
            : (zh ? '等待相似事件调查形成 influence' : 'WAITING FOR A SIMILAR INVESTIGATION')}</p>
          {recalled.slice(0, 2).map((row) => <code key={row.memory_id}>{row.memory_id} · {row.subsequent_influence_count}</code>)}
        </article>
      </div>

      <footer className="imr-foot">
        <span>{data?.durable ? (zh ? 'PostgreSQL 持久化已连接' : 'POSTGRESQL DURABILITY CONNECTED') : (zh ? '持久化连接待确认' : 'DURABILITY PENDING')}</span>
        <span>{data?.risks.length ? `${zh ? '关联长期风险' : 'RELATED RISKS'} ${data.risks.length}` : (zh ? '本轮无长期风险晋升' : 'NO LONG-RUN RISK PROMOTION IN THIS ROUND')}</span>
        <a href="#live-memory">{zh ? '查看本机在线记忆与版本 ▾' : 'OPEN ONLINE MEMORY AND VERSIONS ▾'}</a>
      </footer>
    </section>
  )
}
