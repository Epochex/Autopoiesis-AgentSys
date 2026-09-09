import { useEffect, useMemo, useState } from 'react'
import type { Lang } from '../i18n'
import type { LiveMemoryDetailRecord, LiveMemoryEvent, LiveMemoryEventsResponse, LiveMemoryListResponse } from '../types'
import { humanizeMemoryRecord } from './incident-copy'
import { memoryIdsForEvent } from './event-memory-links'
import { contextAt } from './live-context'
import { MemoryGraph } from './MemoryGraph'
import { ContextPacket } from './ContextPacket'
import type { MemRecord } from '../types'
import type { OnlineContextData } from './OnlineContext'
import './live-memory-graph.css'
import { useDisplayTranslation } from './useDisplayTranslation'

type Step = { at: string; kind: string; payload: unknown }
export type GraphSession = { session_id: string; subject: string; case_id?: string | null; trace_events: Step[]; working_set?: Record<string, unknown>; retrieval_results?: Record<string, unknown>[]; decision?: Record<string, unknown> }
export type GraphReceipt = { subject: string; current_memories: { memory_id: string }[]; investigation: null | { session_id: string; memory_ids: string[]; memory_enabled?: boolean; executed_probe_count: number; candidate_probe_count: number } }
type Tick = { id: string; at: string; memory?: LiveMemoryEvent; step?: Step }
const labels: Record<string, string> = { knowledge_retrieval_attempted: '检索设备资料', hypothesis_proposed: '建立原因假设', discriminating_probe_selected: '选择核验项', business_decision_recorded: '保存处置决定', memory_shortcut: '记忆引导检查', memory_intervention_evaluated: '核验记忆影响', memory_candidates_ranked: '检索记忆', verified_procedure_indexed: '更新验证步骤', knowledge_retrieved: '读取设备资料', retrieval_candidates_filtered: '筛选历史记录', hypothesis_confirmed: '确认原因', probe_round_completed: '检查结果', tool_called: '采集证据', UPSERT: '写入记忆版本', QUARANTINE: '隔离记忆' }
const tiers: Record<string, string> = { episodic: '历史案例', procedural: '处理方法', semantic: '归纳记录', asset_profile: '设备资料' }
labels.context_compiled = '提交模型上下文'
labels.working_set_updated = '更新工作集'
const EN: Record<string, string> = {"检索设备资料": "Search device records", "建立原因假设": "Propose cause hypotheses", "选择核验项": "Select verification", "保存处置决定": "Save disposition", "记忆引导检查": "Memory-guided check", "核验记忆影响": "Verify memory influence", "检索记忆": "Retrieve memories", "更新验证步骤": "Update verified procedure", "读取设备资料": "Read device records", "筛选历史记录": "Filter historical records", "确认原因": "Confirm cause", "检查结果": "Check results", "采集证据": "Collect evidence", "写入记忆版本": "Write memory version", "隔离记忆": "Quarantine memory", "历史案例": "Past incidents", "处理方法": "Procedures", "归纳记录": "Learned patterns", "设备资料": "Device records", "提交模型上下文": "Submit model context", "更新工作集": "Update working set", "记忆目录读取失败": "Memory catalog request failed", "持久化记忆未连接": "Persistent memory is disconnected", "记忆更新记录读取失败": "Memory updates request failed", "记忆更新记录未持久化": "Memory updates are not persisted", "更新游标未推进": "Update cursor did not advance", "服务故障核验步骤": "Service failure verification procedure", "检索候选": "Retrieval candidates", "模型输入": "Model input", "已完成选材": "Selection completed", "等待检索": "Waiting for retrieval", "写回 · ": "Write-back · ", "当前会话快照": "Current session snapshot", "实时记忆观测舱": "Live memory observatory", "记忆观测舱": "Memory observatory", "全部在线记忆": "All online memories", "隐藏隔离记录": "Hide quarantined records", "显示隔离记录": "Show quarantined records", "正在读取记忆与版本记录…": "Loading memories and versions…", "当前快照": "Current snapshot", " · 已保存": " · saved", "高亮：本步更新": "Highlight: updated this step", "连线：读取 / 写回": "Links: read / write-back", "三格：置信评分（上限 3）": "Three cells: confidence score (maximum 3)", "右下数值：重要程度": "Bottom-right value: importance", "点击节点锁定条目": "Click a node to pin the record", "记忆条目审查": "Memory record review", "条目审查": "Record review", "返回记忆图": "Back to memory graph", "展开完整记录": "Expand full record", "已锁定": "Pinned", "跟随游标": "Following cursor", "等待更新记录": "Waiting for updates", "恢复跟随游标": "Follow cursor again", "该版本内容摘要": "Version summary", "记忆内容": "Memory content", "当前保存全文": "Full saved content", "版本变化": "Version changes", " · 版本 ": " · version ", "置信评分": "Confidence score", "重要程度": "Importance", "保留强度": "Retention strength", "首次记录": "First record", "内容差异": "Content changes", "上下文选择": "Context selection", "已检索，等待选材结果": "Retrieved; awaiting selection", "已纳入本次上下文": "Selected for this context", "已过滤": "Filtered", "本步动作": "Action at this step", "版本更新记录": "Version history", "来源调查": "Source investigations", "当前调查": "Current investigation", "打开关联调查": "Open linked investigation", "原始字段与来源": "Original fields and provenance", "暂停": "Pause", "播放": "Play", "跟随最新": "Follow latest", "暂无时间记录": "No recorded timestamp", "动态图时间轴": "Memory graph timeline", "调查动作回放": "Investigation replay"}
const clock = (value: string, lang: Lang) => new Date(value).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', { hour12: false })
let memoryCache: { records: LiveMemoryDetailRecord[]; events: LiveMemoryEvent[]; after: number } | null = null

export function LiveMemoryGraph({ lang, subject, receipt, session, onOpenSource }: { lang: Lang; subject?: string; receipt?: GraphReceipt | null; session?: GraphSession | null; onOpenSource?: (id: string) => void }) {
  const zh = lang === 'zh'
  const [records, setRecords] = useState<LiveMemoryDetailRecord[]>(memoryCache?.records ?? [])
  const [events, setEvents] = useState<LiveMemoryEvent[]>(memoryCache?.events ?? [])
  const [error, setError] = useState('')
  const [ready, setReady] = useState(Boolean(memoryCache))
  const [cursor, setCursor] = useState<string | null>(null)
  const [playing, setPlaying] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [showQuarantined, setShowQuarantined] = useState(false)
  const { translate, error: translationError, retry: retryTranslation } = useDisplayTranslation([...records.map(r => r.text), ...events.map(e => e.text_head ?? ''), ...(session?.retrieval_results ?? []).map(r => String(r.summary ?? r.title ?? ''))], lang)
  const t = (value: string) => zh ? value : EN[value] ?? translate(value)
  const label = (kind: string) => t(labels[kind] ?? kind.replaceAll('_', ' '))
  useEffect(() => {
    const t = (value: string) => lang === 'zh' ? value : EN[value] ?? value
    const abort = new AbortController(); let timer: number | undefined; let after = memoryCache?.after ?? 0
    const read = async () => {
      try {
        const listResponse = await fetch('/api/rca/memory?limit=1000&include_quarantined=true&details=true', { signal: abort.signal })
        if (!listResponse.ok) throw new Error(t('记忆目录读取失败'))
        const list = await listResponse.json() as LiveMemoryListResponse
        if (!list.durable) throw new Error(t('持久化记忆未连接'))
        const loaded = list.records as unknown as LiveMemoryDetailRecord[]
        const incoming: LiveMemoryEvent[] = []; let next = after
        for (;;) {
          const r = await fetch(`/api/rca/memory/events?after=${next}&limit=2000`, { signal: abort.signal })
          if (!r.ok) throw new Error(t('记忆更新记录读取失败'))
          const page = await r.json() as LiveMemoryEventsResponse
          if (!page.durable) throw new Error(t('记忆更新记录未持久化'))
          incoming.push(...page.events)
          const high = Math.max(next, page.high_water ?? next, ...page.events.map(e => e.offset))
          if (page.next_offset == null) { next = high; break }
          if (page.next_offset <= next) throw new Error(t('更新游标未推进'))
          next = page.next_offset
        }
        if (abort.signal.aborted) return
        after = next
        const merged = [...new Map([...(memoryCache?.events ?? []), ...incoming].map(e => [e.offset, e])).values()]
        memoryCache = { records: loaded, events: merged, after }
        setRecords(loaded); setEvents(merged); setReady(true); setError('')
      } catch (e) { if (!abort.signal.aborted) setError(String(e)) }
      finally { if (!abort.signal.aborted) timer = window.setTimeout(read, 5000) }
    }
    void read(); return () => { abort.abort(); if (timer !== undefined) window.clearTimeout(timer) }
  }, [lang])
  const ticks = useMemo(() => {
    const ids = new Set(records.map(r => r.memory_id))
    const related = new Set([...(receipt?.investigation?.memory_ids ?? []), ...(receipt?.current_memories.map(m => m.memory_id) ?? [])])
    if (selected) related.add(selected.replace(/^(context:|write:)/, ''))
    const rows: Tick[] = events.filter(e => ids.has(e.memory_id) && (!subject || related.has(e.memory_id))).map(e => ({ id: `m:${e.offset}`, at: e.occurred_at, memory: e }))
    session?.trace_events.forEach((step, i) => rows.push({ id: `s:${session.session_id}:${i}`, at: step.at, step }))
    return rows.sort((a, b) => Date.parse(a.at) - Date.parse(b.at) || a.id.localeCompare(b.id))
  }, [events, records, session, receipt, subject, selected])
  const index = cursor === null ? ticks.length - 1 : ticks.findIndex(t => t.id === cursor)
  const tick = ticks[index]
  useEffect(() => {
    if (!playing) return
    const timer = window.setTimeout(() => {
      if (index >= ticks.length - 1) { setPlaying(false); return }
      setCursor(ticks[index + 1].id)
    }, 900)
    return () => window.clearTimeout(timer)
  }, [playing, index, ticks])

  const visibleSteps = useMemo(() => ticks.slice(0, index + 1).flatMap(t => t.step ? [t.step] : []), [ticks, index])
  const context = useMemo(() => contextAt(visibleSteps), [visibleSteps])
  const versions = useMemo(() => {
    const map = new Map<string, LiveMemoryEvent>()
    events.filter(e => cursor === null || (tick && Date.parse(e.occurred_at) <= Date.parse(tick.at))).sort((a,b) => a.offset - b.offset).forEach(e => map.set(e.memory_id, e))
    return map
  }, [events, cursor, tick])
  const available = useMemo(() => {
    const history = new Set(events.map(e => e.memory_id))
    return records.filter(r => cursor === null || versions.has(r.memory_id) || !history.has(r.memory_id)).filter(r => {
      const v = versions.get(r.memory_id)
      return showQuarantined || !(v ? v.event_type === 'QUARANTINE' || v.quarantine_reason !== null : r.quarantined)
    })
  }, [records, events, versions, cursor, showQuarantined])
  const title = (r: LiveMemoryDetailRecord) => r.tags.includes('root:service_failed') ? t('服务故障核验步骤') : humanizeMemoryRecord(translate(versions.get(r.memory_id)?.text_head ?? r.text), r.tags, r.asset_ids, zh)
  const graphRecords: MemRecord[] = available.map(r => {
    const v = versions.get(r.memory_id)
    return { ...r, text: title(r), confidence: v?.confidence ?? r.confidence, importance: v?.importance ?? r.importance,
      strength: v?.strength ?? r.strength, evidence_snapshot: [],
      links: cursor === null ? [...new Set([...r.links, ...r.relations.map(link => link.target_id)])] : [],
      quarantined: v ? v.event_type === 'QUARANTINE' || v.quarantine_reason !== null : r.quarantined }
  })
  const rows = context.current?.rows ?? []
  const inRail = rows
  const lastWrite = [...visibleSteps].reverse().find(s => s.kind === 'verified_procedure_indexed')
  const writes = lastWrite ? memoryIdsForEvent(lastWrite.payload, records.map(r => r.memory_id)) : []
  const phase = context.current?.step.kind === 'retrieval_candidates_filtered' ? t('检索候选') : context.current?.step.kind === 'context_compiled' ? t('模型输入') : context.current ? t('已完成选材') : t('等待检索')
  const entries = inRail.map(r => ({ id: 'context:' + r.item_id, label: available.find(m => m.memory_id === r.item_id) ? title(available.find(m => m.memory_id === r.item_id)!) : translate(String(r.title ?? r.item_id)), memoryId: available.some(m => m.memory_id === r.item_id) ? r.item_id : undefined, write: false, dropped: !r.selected_for_context && !r.candidate, changed: context.current?.step === tick?.step && Boolean(r.candidate || context.added.has(r.item_id)) }))
  writes.forEach(id => entries.push({ id: 'write:' + id, label: t('写回 · ') + (available.find(r => r.memory_id === id) ? title(available.find(r => r.memory_id === id)!) : id), memoryId: id, write: true, dropped: false, changed: lastWrite === tick?.step }))
  const selectedId = selected ?? tick?.memory?.memory_id ?? entries[0]?.id ?? null
  const memoryId = selectedId?.replace(/^(context:|write:)/, '')
  const record = available.find(r => r.memory_id === memoryId)
  const version = record ? versions.get(record.memory_id) : undefined
  const previousVersion = version ? events.filter(e => e.memory_id === version.memory_id && e.offset < version.offset).sort((a,b) => b.offset-a.offset)[0] : undefined
  const row = rows.find(r => r.item_id === memoryId)
  const activeStep = selectedId?.startsWith('write:') ? lastWrite : tick?.step
  const openNode = (id: string | null) => { setSelected(id); setPlaying(false) }
  const checkpoint = [...visibleSteps].reverse().find(s => s.kind === 'working_set_updated' || s.kind === 'context_compiled')
  const packet: OnlineContextData = { lang, rows, previous: context.previous?.rows ?? [], phase, at: context.current?.step.at,
    payload: context.current?.payload, text: Object.fromEntries(available.map(r => [r.memory_id, versions.get(r.memory_id)?.text_head ?? r.text])),
    workingSet: checkpoint ? { at: checkpoint.at, value: (checkpoint.payload as Record<string, unknown>).working_set } : cursor === null && session?.working_set ? { at: t('当前会话快照'), value: session.working_set } : undefined,
    onSelect: id => { openNode('context:' + id) } }
  return <section className="lmg" aria-label={t('实时记忆观测舱')}>
    <header><div><h2>{t('记忆观测舱')}</h2><span className="lmg-subject">{subject ?? t('全部在线记忆')} · {zh ? '每 5 秒同步' : 'Syncs every 5 seconds'}</span></div><div className="lmg-tools"><span>{available.length} {zh ? '条记忆' : 'memories'} · {ticks.length} {zh ? '个更新与调查动作' : 'updates and investigation actions'}</span><button aria-pressed={showQuarantined} onClick={() => setShowQuarantined(v => !v)}>{showQuarantined ? t('隐藏隔离记录') : t('显示隔离记录')}</button></div></header>
    {subject && receipt?.investigation ? <p className="lmg-outcome">{zh ? '本次引用' : 'Referenced'} {receipt.investigation.memory_ids.length} {zh ? '条记忆，执行' : 'memories; executed'} {receipt.investigation.executed_probe_count} {zh ? '项检查，关联' : 'checks; linked'} {receipt.current_memories.length} {zh ? '条写入记录。' : 'memory writes.'}</p> : null}
    {error && <p role="alert">{error} {zh ? '保留上次成功读取的图。' : 'Keeping the last successfully loaded graph.'}</p>}
    {translationError && <p role="alert">{translationError} <button onClick={retryTranslation}>Retry translation</button></p>}
    {!ready && <p role="status">{t('正在读取记忆与版本记录…')}</p>}
    <div className={expanded ? 'lmg-body detail-expanded' : 'lmg-body'}>
      <div className="lmg-graph"><MemoryGraph records={graphRecords} events={[]} touchedIds={new Set(tick?.memory ? [tick.memory.memory_id] : [])} recall={null} selectedId={memoryId ?? null} pinnedId={selected ? memoryId ?? null : null} onSelect={openNode} zh={zh} online={{ versions: Object.fromEntries(available.map(r => [r.memory_id, versions.has(r.memory_id) ? 'v' + versions.get(r.memory_id)!.version + t(' · 已保存') : t('当前快照')])), entries, phase }}/><div className="lmg-graph-legend"><span>{t('高亮：本步更新')}</span><span>{t('连线：读取 / 写回')}</span><span>{t('三格：置信评分（上限 3）')}</span><span>{t('右下数值：重要程度')}</span><span>{t('点击节点锁定条目')}</span></div></div>
      <aside className="lmg-audit" aria-label={t('记忆条目审查')}>
        <header><b>{t('条目审查')}</b><button onClick={() => setExpanded(v => !v)}>{expanded ? t('返回记忆图') : t('展开完整记录')}</button></header>
        <div className="lmg-audit-body">
          <small>{record ? t(tiers[record.tier] ?? record.tier) : phase} · {selected ? t('已锁定') : t('跟随游标')}</small>
          <h3>{record ? title(record) : translate(String(row?.title ?? (activeStep ? label(activeStep.kind) : t('等待更新记录'))))}</h3>
          {selected && <button onClick={() => setSelected(null)}>{t('恢复跟随游标')}</button>}
          {record && <><h4>{version ? t('该版本内容摘要') : t('记忆内容')}</h4><p>{translate(version?.text_head ?? record.text)}</p><details><summary>{t('当前保存全文')}</summary><p>{translate(record.text)}</p></details><code>{record.memory_id}</code>
            <h4>{t('版本变化')}</h4><p>{version ? clock(version.occurred_at, lang) + t(' · 版本 ') + version.version : t('当前快照')}</p>
            {version && <div className="lmg-version-diff">{(['confidence','importance','strength'] as const).map((key,i) => <div key={key}><span>{[t('置信评分'),t('重要程度'),t('保留强度')][i]}</span><b>{previousVersion?.[key] ?? t('首次记录')} → {version[key]}</b></div>)}</div>}
            {previousVersion && previousVersion.text_head !== version?.text_head && <details open={expanded}><summary>{t('内容差异')}</summary><p>{zh ? '变更前：' : 'Before: '}{translate(previousVersion.text_head)}</p><p>{zh ? '变更后：' : 'After: '}{translate(version?.text_head ?? '')}</p></details>}
          </>}
          {row && <><h4>{t('上下文选择')}</h4><p>{row.candidate ? t('已检索，等待选材结果') : row.selected_for_context ? t('已纳入本次上下文') : t('已过滤')}</p>{typeof row.context_score === 'number' && <p>{zh ? '选材评分' : 'Selection score'} {row.context_score}</p>}<code>{row.item_id}</code></>}
          <h4>{t('本步动作')}</h4><p>{tick ? clock(tick.at, lang) : ''} · {tick ? label(tick.memory?.event_type ?? tick.step?.kind ?? '') : ''}</p>
          {record && <><h4>{t('版本更新记录')}</h4><nav className="lmg-version-list">{events.filter(e => e.memory_id === record.memory_id && (!version || e.offset <= version.offset)).map(e => <button key={e.offset} onClick={() => { setCursor('m:' + e.offset); setPlaying(false) }}>v{e.version} · {label(e.event_type)} · {clock(e.occurred_at, lang)}</button>)}</nav><h4>{t('来源调查')}</h4>{record.source_trace_ids.map(id => <button className="lmg-source-link" key={id} disabled={id === session?.session_id} onClick={() => onOpenSource?.(id)}>{id === session?.session_id ? t('当前调查') : t('打开关联调查')} · {id} →</button>)}</>}
          <details open={expanded}><summary>{t('原始字段与来源')}</summary><pre>{JSON.stringify(row ?? version ?? activeStep?.payload ?? {}, null, 2)}</pre></details>
        </div>
      </aside>
    </div>
    <footer><div className="lmg-controls"><button onClick={() => { setPlaying(p => !p); if (index >= ticks.length - 1) setCursor(ticks[0]?.id ?? null) }} disabled={!ticks.length}>{playing ? t('暂停') : t('播放')}</button><button aria-pressed={cursor === null} onClick={() => { setCursor(null); setPlaying(false) }}>{t('跟随最新')}</button><span>{tick ? clock(tick.at, lang) : t('暂无时间记录')}</span><b>{tick ? label(tick.memory?.event_type ?? tick.step?.kind ?? '') : ''}</b></div><input aria-label={t('动态图时间轴')} type="range" min="0" max={Math.max(0, ticks.length - 1)} value={Math.max(0, index)} disabled={!ticks.length} onChange={e => { setPlaying(false); setCursor(ticks[Number(e.target.value)].id) }}/></footer>
    <ContextPacket online={packet}/>
    {session && <nav className="lmg-trace-strip" aria-label={t('调查动作回放')}>{ticks.filter(t => t.step).map((t, i) => <button key={t.id} aria-pressed={tick?.id === t.id} onClick={() => { setPlaying(false); setCursor(t.id); setSelected(null) }}><small>{String(i + 1).padStart(2, '0')}</small>{label(t.step!.kind)}<time>{clock(t.at, lang).split(' ').at(-1)}</time></button>)}</nav>}
  </section>
}
