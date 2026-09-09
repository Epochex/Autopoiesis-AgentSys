import { useEffect, useMemo, useRef, useState } from 'react'
import type { Lang } from '../i18n'
import { filterGlobalMemory, loadGlobalMemory, type GlobalMemorySnapshot } from './global-memory'
import { useDisplayTranslation } from './useDisplayTranslation'
import { MemorySourceError, resolveMemorySource, type MemorySourceCase } from './memory-source'
import './global-memory.css'

export function GlobalMemoryPage({ lang, onOpenSource }: { lang: Lang; onOpenSource?: (source: MemorySourceCase) => void }) {
  const zh = lang === 'zh'
  const t = (cn: string, en: string) => zh ? cn : en
  const [data, setData] = useState<GlobalMemorySnapshot | null>(null)
  const previous = useRef<GlobalMemorySnapshot | null>(null)
  const [error, setError] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [query, setQuery] = useState('')
  const [tier, setTier] = useState('')
  const [state, setState] = useState('all')
  const [selected, setSelected] = useState<string | null>(null)
  const [versionOffset, setVersionOffset] = useState<number | null>(null)
  const [tab, setTab] = useState<'inventory' | 'history'>('inventory')
  const [page, setPage] = useState(0)
  const [sourceState, setSourceState] = useState<{ id: string; loading: boolean; reason?: string } | null>(null)
  const sourceRequest = useRef<AbortController | null>(null)
  useEffect(() => () => sourceRequest.current?.abort(), [])
  const openSource = async (id: string) => {
    sourceRequest.current?.abort()
    const controller = new AbortController()
    sourceRequest.current = controller
    setSourceState({ id, loading: true })
    try {
      const resolved = await resolveMemorySource(id, controller.signal)
      if (!controller.signal.aborted) { setSourceState(null); onOpenSource?.(resolved) }
    } catch (error) {
      if (!controller.signal.aborted) setSourceState({ id, loading: false, reason: error instanceof MemorySourceError ? error.reason : 'unavailable' })
    }
  }
  useEffect(() => {
    const abort = new AbortController(); let timer: number | undefined
    const poll = async () => {
      try {
        const snapshot = await loadGlobalMemory(abort.signal, previous.current)
        if (!abort.signal.aborted) { previous.current = snapshot; setData(snapshot); setError(false) }
      } catch { if (!abort.signal.aborted) setError(true) }
      finally { if (!abort.signal.aborted) timer = window.setTimeout(poll, 5000) }
    }
    void poll()
    return () => { abort.abort(); window.clearTimeout(timer) }
  }, [refresh])
  const rows = useMemo(() => filterGlobalMemory(data?.records ?? [], query, tier, state), [data, query, tier, state])
  const ids = useMemo(() => new Set(rows.map(row => row.memory_id)), [rows])
  const history = useMemo(() => [...(data?.events ?? [])].reverse().filter(event => !query && !tier && state === 'all' || ids.has(event.memory_id)), [data, ids, query, tier, state])
  const record = data?.records.find(row => row.memory_id === selected)
  const versions = (data?.events ?? []).filter(event => event.memory_id === selected)
  const version = versions.find(event => event.offset === versionOffset) ?? versions.at(-1)
  const prior = version ? versions.filter(event => event.offset < version.offset).at(-1) : undefined
  const tierName = (value: string) => ({ asset_profile: t('设备资料', 'Asset profiles'), semantic: t('归纳知识', 'Knowledge'), procedural: t('处理方法', 'Procedures'), episodic: t('历史案例', 'Cases') }[value] ?? value)
  const actionName = (type: string, v: number) => type === 'QUARANTINE' ? t('隔离', 'Quarantined') : v === 1 ? t('写入', 'Created') : t('更新', 'Updated')
  const time = (value?: string | null) => value ? new Date(value).toLocaleString(zh ? 'zh-CN' : 'en-GB', { hour12: false }) : t('未记录', 'Not recorded')
  const open = (id: string, offset: number | null = null) => { sourceRequest.current?.abort(); setSourceState(null); setSelected(id); setVersionOffset(offset) }
  const count = tab === 'inventory' ? rows.length : history.length
  const activePage = Math.min(page, Math.max(0, Math.ceil(count / 40) - 1))
  const displayTexts = [...rows.slice(activePage * 40, (activePage + 1) * 40).map(row => row.text), ...history.slice(activePage * 40, (activePage + 1) * 40).map(event => event.text_head), record?.text, record?.quarantine_reason, version?.text_head, version?.quarantine_reason, prior?.text_head].filter((value): value is string => Boolean(value))
  const display = useDisplayTranslation(displayTexts, lang)
  return <section className="global-memory" aria-label={t('长期记忆', 'Long-term memory')}>
    <header className="gm-heading"><div><small>{t('常态观测 · 跨事件持久化记录', 'CONTINUOUS OBSERVATION · PERSISTENT RECORDS ACROSS CASES')}</small><h1>{t('长期记忆与演化', 'Long-term memory and evolution')}</h1><p>{t('持续查看全网设备知识、处理方法和历史案例的积累，以及每次保存的版本与来源。', 'Observe accumulated asset knowledge, procedures and cases across the network, with saved versions and provenance.')}</p></div><button onClick={() => setRefresh(v => v + 1)}>{t('刷新', 'Refresh')}</button></header>
    {display.loading && <p role="status">Translating saved descriptions…</p>}
    {display.error && <p role="alert">{display.error} <button onClick={display.retry}>Retry translation</button></p>}
    {error && <p role="alert">{t('持久化记忆读取失败。保留上次成功结果，将自动重试。', 'Persistent memory could not be loaded. The last successful result is retained; retrying automatically.')}</p>}
    {!data && !error && <p role="status">{t('正在读取全局记忆与变更历史…', 'Loading global memory and change history…')}</p>}
    {data && <>
      <div className="gm-summary">{(['asset_profile', 'semantic', 'procedural', 'episodic'] as const).map(key => <div key={key}><span>{tierName(key)}</span><strong>{data.inventory.counts[key]}</strong></div>)}<div><span>{t('隔离记录', 'Quarantined')}</span><strong>{data.inventory.counts.quarantined}</strong></div><div><span>{t('已记录变更', 'Recorded changes')}</span><strong>{data.events.length}</strong></div></div>
      <p className="gm-sync">{t('每 5 秒同步', 'Syncs every 5 seconds')} · {time(data.syncedAt)} · {t('已加载库存', 'Inventory loaded')} {data.records.length}/{data.total} · {t('自动衰减', 'Automatic decay')}: {data.inventory.retention.decay_wired ? t('已接入', 'Connected') : t('未接入', 'Not connected')} · {t('最近衰减记录', 'Latest decay record')}: {time(data.inventory.retention.last_decay_at)}</p>
      <div className="gm-controls"><label>{t('搜索记忆、资产或标签', 'Search memory, assets or tags')}<input value={query} onChange={e => { setQuery(e.target.value); setPage(0) }} /></label><label>{t('记忆层级', 'Memory tier')}<select value={tier} onChange={e => { setTier(e.target.value); setPage(0) }}><option value="">{t('全部层级', 'All tiers')}</option>{['asset_profile', 'semantic', 'procedural', 'episodic'].map(key => <option key={key} value={key}>{tierName(key)}</option>)}</select></label><label>{t('保存状态', 'Record state')}<select value={state} onChange={e => { setState(e.target.value); setPage(0) }}><option value="all">{t('全部', 'All')}</option><option value="active">{t('有效', 'Active')}</option><option value="quarantined">{t('隔离', 'Quarantined')}</option></select></label></div>
      <nav className="gm-tabs"><button aria-pressed={tab === 'inventory'} onClick={() => { setTab('inventory'); setPage(0) }}>{t('当前库存', 'Current inventory')} ({rows.length})</button><button aria-pressed={tab === 'history'} onClick={() => { setTab('history'); setPage(0) }}>{t('长期变更流', 'Change history')} ({history.length})</button></nav>
      <div className="gm-body"><div className="gm-list">
        {tab === 'inventory' ? <table><thead><tr><th>{t('记忆 / 关联资产', 'Memory / linked assets')}</th><th>{t('层级', 'Tier')}</th><th>{t('状态 / 最近观测', 'State / last observed')}</th></tr></thead><tbody>{rows.slice(activePage * 40, (activePage + 1) * 40).map(row => <tr key={row.memory_id} aria-selected={selected === row.memory_id}><td><button className="gm-record" onClick={() => open(row.memory_id)}>{display.translate(row.text)}</button><small>{row.asset_ids.join(' · ') || t('未关联资产', 'No linked assets')}</small></td><td>{tierName(row.tier)}</td><td>{row.quarantined ? t('隔离', 'Quarantined') : t('有效', 'Active')}<small>{time(row.last_observed_at)}</small></td></tr>)}</tbody></table> : <ol className="gm-history">{history.slice(activePage * 40, (activePage + 1) * 40).map(event => <li key={event.offset}><button onClick={() => open(event.memory_id, event.offset)}><span>{time(event.occurred_at)} · {actionName(event.event_type, event.version)} · v{event.version}</span><p>{display.translate(event.text_head)}</p><small>{event.memory_id}</small></button></li>)}</ol>}
        {!count && <p>{t('当前筛选范围内没有记录。', 'No records match these filters.')}</p>}
        <footer className="gm-pagination"><button disabled={activePage === 0} onClick={() => setPage(activePage - 1)}>{t('上一页', 'Previous')}</button><span>{activePage + 1} / {Math.max(1, Math.ceil(count / 40))}</span><button disabled={(activePage + 1) * 40 >= count} onClick={() => setPage(activePage + 1)}>{t('下一页', 'Next')}</button></footer>
      </div><aside className="gm-inspector"><h2>{t('条目与版本检查', 'Record and version inspection')}</h2>{!selected ? <p>{t('选择一条记忆或一次变更，查看全文、版本和来源。', 'Select a record or change to inspect its content, versions and provenance.')}</p> : <><code>{selected}</code>{record ? <><h3>{t('当前保存全文', 'Current saved text')}</h3><p>{display.translate(record.text)}</p><dl><dt>{t('首次观测', 'First observed')}</dt><dd>{time(record.first_observed_at)}</dd><dt>{t('最近观测', 'Last observed')}</dt><dd>{time(record.last_observed_at)}</dd><dt>{t('关联资产', 'Linked assets')}</dt><dd>{record.asset_ids.join(', ') || t('未记录', 'Not recorded')}</dd><dt>{t('访问次数', 'Access count')}</dt><dd>{record.access_count}</dd></dl>{record.quarantine_reason && <p>{t('隔离原因', 'Quarantine reason')}: {display.translate(record.quarantine_reason)}</p>}<h3>{t('来源调查', 'Source investigations')}</h3>{sourceState?.loading ? <p role="status">{t('正在查找来源对应的案件…', 'Resolving the source case…')}</p> : sourceState?.reason ? <p role="alert">{sourceState.reason === 'unlinked' ? t('此来源尚未关联可打开的调查案件，原始来源标识保留在下方。', 'This source has no linked investigation case. Its original provenance ID remains below.') : sourceState.reason === 'mismatch' ? t('来源返回的案件身份不一致，请核对原始来源记录。', 'The returned case identity does not match this source. Check the original provenance record.') : t('来源案件读取失败，请重试。', 'The source case could not be loaded. Please retry.')} <code>{sourceState.id}</code></p> : null}{record.source_trace_ids.length ? record.source_trace_ids.map(id => <div key={id}>{onOpenSource ? <button disabled={sourceState?.id === id && sourceState.loading} onClick={() => void openSource(id)}>{t('打开调查', 'Open investigation')} · {id}</button> : <code>{id}</code>}</div>) : <p>{t('未记录来源调查', 'No source investigation recorded')}</p>}<details><summary>{t('当前原始字段与关联关系', 'Current raw fields and relations')}</summary><pre>{JSON.stringify(record, null, 2)}</pre></details></> : <p>{t('该变更保留在历史中，当前库存未返回对应条目。', 'This change is retained in history; its record is absent from the loaded inventory.')}</p>}
      <h3>{t('已保存版本', 'Saved versions')}</h3><div className="gm-versions">{versions.map(event => <button key={event.offset} aria-pressed={version?.offset === event.offset} onClick={() => setVersionOffset(event.offset)}>v{event.version} · {actionName(event.event_type, event.version)} · {time(event.occurred_at)}</button>)}</div>{!versions.length && <p>{t('当前条目没有已记录的版本事件。', 'No version events are recorded for this entry.')}</p>}{version && <><h3>{t('所选版本摘要', 'Selected version excerpt')}</h3><p>{display.translate(version.text_head)}</p><small>{t('历史事件保存内容摘要；上方全文为当前库存版本。', 'Historical events store excerpts; the full text above is the current inventory version.')}</small>{prior && prior.text_head !== version.text_head && <><h4>{t('上一版本摘要', 'Previous version excerpt')}</h4><p>{display.translate(prior.text_head)}</p></>}<dl>{(['confidence', 'importance', 'strength'] as const).map((key, i) => <div key={key}><dt>{[t('置信评分', 'Confidence'), t('重要程度', 'Importance'), t('保留强度', 'Retention strength')][i]}</dt><dd>{prior ? `${prior[key]} → ` : ''}{version[key]}</dd></div>)}</dl>{version.quarantine_reason && <p>{t('隔离原因', 'Quarantine reason')}: {display.translate(version.quarantine_reason)}</p>}</>}</>}</aside></div>
    </>}
  </section>
}
