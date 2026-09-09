import { useEffect, useRef } from 'react'
import type { Lang } from '../i18n'
import { useDisplayTranslation } from './useDisplayTranslation'
import type { ContextRow } from './live-context'
import './context-packet.css'
import './context-full.css'

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
const reason = (row: ContextRow) => {
  const reasons = row.selection_reasons ?? row.drop_reason ?? row.reason
  return Array.isArray(reasons) ? reasons.join(' · ') : typeof reasons === 'string' ? reasons : ''
}
function ContextRows({ data, complete = false }: { data: OnlineContextData; complete?: boolean }) {
  const zh = data.lang !== 'en'
  const { translate } = useDisplayTranslation([...Object.values(data.text), ...data.rows.flatMap(r => [r.summary ?? '', r.title ?? '', reason(r)])], data.lang ?? 'zh')
  const previous = new Set(data.previous.filter(r => r.selected_for_context).map(r => r.item_id))
  const scores = data.rows.map(r => typeof r.context_score === 'number' ? r.context_score : typeof r.final_score === 'number' ? r.final_score : null)
  const maximum = Math.max(0, ...scores.filter((score): score is number => score !== null))
  return <div className="cp-rows">{data.rows.map((r, i) => <div className="cp-row" key={r.item_id}>
    <span className="cp-rank">{String(i + 1).padStart(2, '0')}</span>
    <span className="cp-tier">{r.candidate ? (zh ? '候选' : 'CANDIDATE') : r.selected_for_context ? (zh ? '已纳入' : 'SELECTED') : (zh ? '已过滤' : 'FILTERED')}</span>
    <div className="cp-body"><button className="online-context-link" onClick={() => data.onSelect(r.item_id)}>{translate(data.text[r.item_id] ?? r.summary ?? r.title ?? r.item_id)}</button>
      <div className="cp-signals">{typeof r.context_score === 'number' && <span>{zh ? '选材评分' : 'Selection score'} {r.context_score.toFixed(4)}</span>}{typeof r.final_score === 'number' && <span>{zh ? '最终评分' : 'Final score'} {r.final_score.toFixed(4)}</span>}<span>{translate(reason(r))}</span><span>{r.selected_for_context && !previous.has(r.item_id) ? (zh ? '本次新增' : 'New this step') : ''}</span></div>
      {complete && <><code>{r.item_id}</code><details><summary>{zh ? '选材字段' : 'Selection fields'}</summary><pre>{JSON.stringify(r, null, 2)}</pre></details></>}
    </div><div className="cp-score-col">{scores[i] !== null && <><b>{scores[i]?.toFixed(4)}</b><span className="cp-bar"><i style={{width: maximum > 0 ? `${Math.max(0,scores[i]!) / maximum * 100}%` : '0%'}}/></span></>}</div>
  </div>)}</div>
}
export function OnlineContextPacket({ data, onFull }: { data: OnlineContextData; onFull: () => void }) {
  const zh = data.lang !== 'en'
  return <section className="cp" aria-label={zh ? "上下文包" : "Context packet"}><header className="cp-head"><div className="cp-head-l"><b className="cp-title">{zh ? '上下文包' : 'Context packet'}</b><span className="cp-sub">{data.phase} · {zh ? '与图上时间轴同步' : 'Synchronized with the graph timeline'}</span></div><div className="cp-head-r"><span>{data.rows.filter(r => r.selected_for_context).length} {zh ? '项纳入' : 'selected'} · {data.rows.filter(r => r.candidate).length} {zh ? '项候选' : 'candidates'}</span><button className="cp-full" onClick={onFull}>{zh ? '完整上下文 ▸' : 'Full context ▸'}</button></div></header>{data.rows.length ? <ContextRows data={data}/> : <p className="cp-msg">{zh ? '此时间点尚未保存检索或选材记录。' : 'No retrieval or selection receipt was saved at this point.'}</p>}</section>
}
export function OnlineContextFull({ data, onClose }: { data: OnlineContextData; onClose: () => void }) {
  const zh = data.lang !== 'en'
  const { translate, error, retry } = useDisplayTranslation([...Object.values(data.text), ...data.rows.flatMap(r => [r.summary ?? '', r.title ?? '', reason(r)])], data.lang ?? 'zh')
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
      <div className="cf-title"><h2>{zh ? '完整上下文' : 'Full context'}</h2><span>{data.phase} · {data.at}</span></div>
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
        {[...previous].filter(id => !current.has(id)).map(id => <p key={id}>{zh ? '移除' : 'Removed'} · {translate(data.text[id] ?? id)}</p>)}
        <h3 className="cf-sec">{zh ? '过滤依据' : 'Filtering reasons'}</h3>
        {data.rows.some(r => !r.selected_for_context && !r.candidate) ? data.rows.filter(r => !r.selected_for_context && !r.candidate).map(r => <p key={r.item_id}>{translate(data.text[r.item_id] ?? r.title ?? r.item_id)}<br/>{translate(reason(r)) || (zh ? '该记录未保存逐项过滤原因' : 'No per-item filtering reason was saved')}</p>) : <p>{data.rows.some(r => r.candidate) ? (zh ? '当前处于候选检索阶段，尚未保存上下文选择结果。' : 'Candidate retrieval is in progress; context selection has not been saved.') : (zh ? '当前选材记录没有过滤项。' : 'No items were filtered in this selection receipt.')}</p>}
        {data.workingSet?.value ? <><h3 className="cf-sec">{zh ? '工作集快照' : 'Working set snapshot'}</h3><small>{data.workingSet.at}</small><pre>{JSON.stringify(data.workingSet.value, null, 2)}</pre></> : null}
        <details><summary>{zh ? '完整原始记录' : 'Complete original record'}</summary><pre>{JSON.stringify(data.payload ?? {}, null, 2)}</pre></details>
      </aside>
    </div>
  </div>
}
