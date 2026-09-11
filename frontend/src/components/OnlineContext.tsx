import { useEffect, useRef } from 'react'
import type { Lang } from '../i18n'
import { useDisplayTranslation } from './useDisplayTranslation'
import type { ContextRow } from './live-context'
import './context-packet.css'
import './context-full.css'
import { display } from './display-lexicon'

export type OnlineContextData = {
  lang?: Lang
  rows: ContextRow[]
  previous: ContextRow[]
  phase: string
  at?: string
  payload?: Record<string, unknown>
  workingSet?: { at: string; value: unknown }
  text: Record<string, string>
  onSelect: (id: string) => void
}
/* Selection reasons arrive as ranker tokens ("bm25_rank:1"); the audience
 * reads what each ranker is, not its identifier. */
const RANKERS: Record<string, [string, string]> = {
  upstream_rank: ['上游排名', 'upstream rank'], bm25_rank: ['关键词排名', 'keyword rank'], vector_rank: ['向量排名', 'vector rank'],
  relation_rank: ['关联排名', 'relation rank'], recency_rank: ['时效排名', 'recency rank'], scope_rank: ['范围排名', 'scope rank'],
  asset_match: ['资产匹配', 'asset match'], root_match: ['故障族匹配', 'fault-family match'], historical_only: ['仅历史参考', 'historical only'],
  reference_only: ['仅供参考', 'reference only'], temporal_filter: ['时间过滤', 'temporal filter'], quarantined: ['已隔离', 'quarantined'],
}
const reasonText = (token: string, zh: boolean) => {
  const [key, value] = token.split(':')
  const known = RANKERS[key]
  if (!known) return token
  return value !== undefined ? `${known[zh ? 0 : 1]} ${value}` : known[zh ? 0 : 1]
}
const reason = (row: ContextRow, zh = true) => {
  const reasons = row.selection_reasons ?? row.drop_reason ?? row.reason
  const list = Array.isArray(reasons) ? reasons.map(String) : typeof reasons === 'string' ? [reasons] : []
  return list.map(token => reasonText(token, zh)).join(' · ')
}
/* What kind of thing a row is, in the words the memory tiers already use. */
const kindOf = (row: ContextRow, zh: boolean) => {
  const kind = String(row.kind ?? '')
  const tier = String(row.tier ?? '')
  if (kind === 'knowledge_document') return zh ? `${String(row.product ?? 'FortiGate')} 知识文档` : `${String(row.product ?? 'FortiGate')} knowledge document`
  if (kind === 'indexed_memory' || tier) {
    const tiers: Record<string, [string, string]> = { procedural: ['处理方法', 'procedure'], episodic: ['历史案例', 'past incident'], semantic: ['归纳知识', 'learned pattern'], asset_profile: ['设备资料', 'device record'] }
    return (tiers[tier] ?? ['记忆记录', 'memory record'])[zh ? 0 : 1]
  }
  return ''
}
/* A memory row is named by its saved text; a document row by its title, with
 * the passage itself kept as a clamped excerpt underneath. */
const rowTitle = (row: ContextRow, text: Record<string, string>) =>
  text[row.item_id] ?? (row.title ? String(row.title) : row.summary ? String(row.summary).slice(0, 140) : row.item_id)
const rowExcerpt = (row: ContextRow, text: Record<string, string>) =>
  !text[row.item_id] && row.title && row.summary && row.summary !== row.title ? String(row.summary) : ''

/* The working set is the investigation's own state; its JSON is kept behind a
 * toggle and the state itself is told in sentences. */
function WorkingSet({ value, zh }: { value: unknown; zh: boolean }) {
  if (!value || typeof value !== 'object') return null
  const ws = value as Record<string, unknown>
  const list = (key: string) => Array.isArray(ws[key]) ? ws[key] as Record<string, unknown>[] : []
  const hyp = (item: Record<string, unknown>) => `${String(item.statement ?? item.hypothesis_id ?? '')}${Array.isArray(item.supporting_evidence_ids) && item.supporting_evidence_ids.length ? `（${zh ? '证据' : 'evidence'} ${(item.supporting_evidence_ids as unknown[]).join(', ')}）` : ''}`
  const receipts = list('tool_receipts')
  return <dl className="cp-ws">
    {ws.goal ? <div><dt>{zh ? '调查目标' : 'Goal'}</dt><dd>{String(ws.goal)}</dd></div> : null}
    {ws.subgoal ? <div><dt>{zh ? '当前子目标' : 'Current subgoal'}</dt><dd>{String(ws.subgoal)}</dd></div> : null}
    {list('confirmed_hypotheses').length ? <div><dt>{zh ? '已确认假设' : 'Confirmed hypotheses'}</dt><dd>{list('confirmed_hypotheses').map(hyp).join('；')}</dd></div> : null}
    {list('open_hypotheses').length ? <div><dt>{zh ? '待验证假设' : 'Open hypotheses'}</dt><dd>{list('open_hypotheses').map(hyp).join('；')}</dd></div> : null}
    {list('rejected_hypotheses').length ? <div><dt>{zh ? '已排除假设' : 'Rejected hypotheses'}</dt><dd>{list('rejected_hypotheses').map(hyp).join('；')}</dd></div> : null}
    {Array.isArray(ws.decisive_evidence_ids) && ws.decisive_evidence_ids.length ? <div><dt>{zh ? '决定性证据' : 'Decisive evidence'}</dt><dd>{(ws.decisive_evidence_ids as unknown[]).join(', ')}</dd></div> : null}
    {receipts.length ? <div><dt>{zh ? '已执行检查' : 'Checks run'} · {receipts.length}</dt><dd>{receipts.map((r, i) => <span key={i} className="cp-ws-receipt"><code>{String(r.evidence_id ?? '')}</code> {String(r.command ?? '')} · {String(r.source ?? '')} · {r.ok ? (zh ? '成功' : 'ok') : (zh ? '失败' : 'failed')}</span>)}</dd></div> : null}
    {list('next_candidates').length ? <div><dt>{zh ? '下一步候选' : 'Next candidates'}</dt><dd>{list('next_candidates').map(c => String(c.probe ?? c.hypothesis_id ?? JSON.stringify(c))).join('；')}</dd></div> : null}
    {list('pending_actions').length ? <div><dt>{zh ? '待执行动作' : 'Pending actions'}</dt><dd>{list('pending_actions').map(c => String(c.action ?? c.kind ?? JSON.stringify(c))).join('；')}</dd></div> : null}
    {ws.transition ? <div><dt>{zh ? '本步转移' : 'Transition'}</dt><dd>{typeof ws.transition === 'string' ? ws.transition : JSON.stringify(ws.transition)}</dd></div> : null}
  </dl>
}
function ContextRows({ data, complete = false }: { data: OnlineContextData; complete?: boolean }) {
  const zh = data.lang !== 'en'
  const { translate } = useDisplayTranslation([...Object.values(data.text), ...data.rows.flatMap(r => [r.summary ?? '', r.title ?? ''])], data.lang ?? 'zh')
  const previous = new Set(data.previous.filter(r => r.selected_for_context).map(r => r.item_id))
  const scores = data.rows.map(r => typeof r.context_score === 'number' ? r.context_score : typeof r.final_score === 'number' ? r.final_score : null)
  const maximum = Math.max(0, ...scores.filter((score): score is number => score !== null))
  return <div className="cp-rows">{data.rows.map((r, i) => <div className="cp-row" key={r.item_id}>
    <span className="cp-rank">{String(i + 1).padStart(2, '0')}</span>
    <span className="cp-tier">{r.candidate ? (zh ? '候选' : 'CANDIDATE') : r.selected_for_context ? (zh ? '已纳入' : 'SELECTED') : (zh ? '已过滤' : 'FILTERED')}</span>
    <div className="cp-body"><button className="online-context-link" onClick={() => data.onSelect(r.item_id)}>{translate(rowTitle(r, data.text))}</button>{kindOf(r, zh) ? <span className="cp-kind">{kindOf(r, zh)}</span> : null}{rowExcerpt(r, data.text) ? <p className="cp-excerpt">{translate(rowExcerpt(r, data.text))}</p> : null}{reason(r, zh) ? <small className="cp-why">{reason(r, zh)}</small> : null}
      <div className="cp-signals">{typeof r.context_score === 'number' && <span>{zh ? '选材评分' : 'Selection score'} {r.context_score.toFixed(4)}</span>}{typeof r.final_score === 'number' && <span>{zh ? '最终评分' : 'Final score'} {r.final_score.toFixed(4)}</span>}<span>{translate(reason(r))}</span><span>{r.selected_for_context && !previous.has(r.item_id) ? (zh ? '本次新增' : 'New this step') : ''}</span></div>
      {complete && <><code>{r.item_id}</code><details><summary>{zh ? '选材字段' : 'Selection fields'}</summary><pre>{JSON.stringify(r, null, 2)}</pre></details></>}
    </div><div className="cp-score-col">{scores[i] !== null && <><b>{scores[i]?.toFixed(4)}</b><span className="cp-bar"><i style={{width: maximum > 0 ? `${Math.max(0,scores[i]!) / maximum * 100}%` : '0%'}}/></span></>}</div>
  </div>)}</div>
}
export function OnlineContextPacket({ data, onFull }: { data: OnlineContextData; onFull: () => void }) {
  const zh = data.lang !== 'en'
  return <section className="cp" aria-label={zh ? "上下文包" : "Context packet"}><header className="cp-head"><div className="cp-head-l"><b className="cp-title">{zh ? '上下文包' : 'Context packet'}</b><span className="cp-sub">{display(data.phase, zh ? 'zh' : 'en')} · {zh ? '与图上时间轴同步' : 'Synchronized with the graph timeline'}</span></div><div className="cp-head-r"><span>{data.rows.filter(r => r.selected_for_context).length} {zh ? '项纳入' : 'selected'} · {data.rows.filter(r => r.candidate).length} {zh ? '项候选' : 'candidates'}</span><button className="cp-full" onClick={onFull}>{zh ? '完整上下文 ▸' : 'Full context ▸'}</button></div></header>{data.rows.length ? <ContextRows data={data}/> : <p className="cp-msg">{zh ? '此时间点尚未保存检索或选材记录。' : 'No retrieval or selection receipt was saved at this point.'}</p>}</section>
}
export function OnlineContextFull({ data, onClose }: { data: OnlineContextData; onClose: () => void }) {
  const zh = data.lang !== 'en'
  const { translate, error, retry } = useDisplayTranslation([...Object.values(data.text), ...data.rows.flatMap(r => [r.summary ?? '', r.title ?? ''])], data.lang ?? 'zh')
  const ref = useRef<HTMLButtonElement>(null)
  const dialog = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    ref.current?.focus()
    const scroll = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      if (e.key === 'Tab') {
        const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), summary, [tabindex="0"]') ?? [])].filter(el => el.getClientRects().length)
        const first = focusable[0], last = focusable.at(-1)
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
      }
    }
    window.addEventListener('keydown', key)
    return () => { document.body.style.overflow = scroll; window.removeEventListener('keydown', key); previous?.focus() }
  }, [onClose])
  const messages = data.payload?.messages
  const current = new Set(data.rows.filter(r => r.selected_for_context).map(r => r.item_id))
  const previous = new Set(data.previous.filter(r => r.selected_for_context).map(r => r.item_id))
  return <div ref={dialog} className="cf-overlay online-context-full" role="dialog" aria-modal="true" aria-label={zh ? "完整上下文" : "Full context"}>
    <header className="cf-head">
      <div className="cf-title"><h2>{zh ? '完整上下文' : 'Full context'}</h2><span>{display(data.phase, zh ? 'zh' : 'en')} · {data.at}</span></div>
      <div className="cf-stats"><span><b>{current.size}</b> {zh ? '项纳入' : 'selected'}</span><span><b>{data.rows.filter(r => r.candidate).length}</b> {zh ? '项候选' : 'candidates'}</span></div>
      <button ref={ref} className="cf-x" onClick={onClose}>{zh ? '返回记忆图 · Esc' : 'Back to memory graph · Esc'}</button>
    </header>
    <div className="cf-body">
      <main className="cf-main online-context-content">
        <h3 className="cf-sec">{zh ? '记忆与资料选择' : 'Memory and source selection'}</h3>
        <ContextRows data={data} complete/>
        {error && <p role="alert">{error} <button onClick={retry}>Retry translation</button></p>}
        {data.payload?.investigation_context ? <><h3 className="cf-sec">{zh ? '调查上下文快照' : 'Investigation context snapshot'}</h3><p>{zh ? '包含该步骤的调查目标、检索内容、工作集与现场证据。' : 'Investigation objectives, retrieval, working set and observed evidence at this step.'}</p><pre>{JSON.stringify(data.payload.investigation_context, null, 2)}</pre></> : null}
        <h3 className="cf-sec">{zh ? '模型输入原文' : 'Original model input'}</h3>
        {Array.isArray(messages) ? messages.map((message, i) => <section key={i}><b>{String(message.role)}</b><pre>{typeof message.content === 'string' ? message.content : JSON.stringify(message.content, null, 2)}</pre></section>) : <p>{zh ? '当前步骤未保存模型输入原文。上方展示该步骤已记录的检索与选择结果。' : 'The original model input was not saved for this step. Recorded retrieval and selection results appear above.'}</p>}
      </main>
      <aside className="cf-side online-context-content">
        <h3 className="cf-sec">{zh ? '本次选材变化' : 'Selection changes this step'}</h3>
        <p>{zh ? '新增' : 'Added'} {[...current].filter(id => !previous.has(id)).length} {zh ? '项 · 移除' : '· Removed'} {[...previous].filter(id => !current.has(id)).length} {zh ? '项 · 保留' : '· Retained'} {[...current].filter(id => previous.has(id)).length} {zh ? '项' : 'items'}</p>
        {[...previous].filter(id => !current.has(id)).map(id => { const row = data.previous.find(r => r.item_id === id); return <p key={id}>{zh ? '移除' : 'Removed'} · {translate(row ? rowTitle(row, data.text) : data.text[id] ?? id)}</p> })}
        <h3 className="cf-sec">{zh ? '过滤依据' : 'Filtering reasons'}</h3>
        {data.rows.some(r => !r.selected_for_context && !r.candidate) ? data.rows.filter(r => !r.selected_for_context && !r.candidate).map(r => <p key={r.item_id}>{translate(data.text[r.item_id] ?? r.title ?? r.item_id)}<br/>{reason(r, zh) || (zh ? '该记录未保存逐项过滤原因' : 'No per-item filtering reason was saved')}</p>) : <p>{data.rows.some(r => r.candidate) ? (zh ? '当前处于候选检索阶段，尚未保存上下文选择结果。' : 'Candidate retrieval is in progress; context selection has not been saved.') : (zh ? '当前选材记录没有过滤项。' : 'No items were filtered in this selection receipt.')}</p>}
        {data.workingSet?.value ? <><h3 className="cf-sec">{zh ? '工作集快照' : 'Working set snapshot'}</h3><small>{data.workingSet.at}</small><WorkingSet value={data.workingSet.value} zh={zh} /><details><summary>{zh ? '工作集原始 JSON' : 'Working set raw JSON'}</summary><pre>{JSON.stringify(data.workingSet.value, null, 2)}</pre></details></> : null}
        <details><summary>{zh ? '完整原始记录' : 'Complete original record'}</summary><pre>{JSON.stringify(data.payload ?? {}, null, 2)}</pre></details>
      </aside>
    </div>
  </div>
}
