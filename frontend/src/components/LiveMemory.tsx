import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { Lang } from '../i18n'
import type {
  LiveMemoryDetailRecord,
  LiveMemoryDetailResponse,
  LiveMemoryEvent,
  LiveMemoryEventsResponse,
  LiveMemoryListRecord,
  LiveMemoryListResponse,
  MemTier,
} from '../types'
import './live-memory.css'

const TICK_MS = 180
const EXPANDED_STORAGE_KEY = 'live-memory-expanded-v2'
const TIER_ORDER: MemTier[] = ['semantic', 'procedural', 'episodic', 'asset_profile']

const readInitialExpanded = (): boolean => {
  if (typeof window === 'undefined') return false
  if (window.location.hash === '#live-memory') return true
  try {
    return window.sessionStorage.getItem(EXPANDED_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

const TIER_LABEL: Record<MemTier, [string, string]> = {
  semantic: ['归纳的规律 · PATTERN', 'PATTERN'],
  procedural: ['处理办法 · HOW-TO', 'HOW-TO'],
  episodic: ['遇到过的事 · SEEN BEFORE', 'SEEN BEFORE'],
  asset_profile: ['设备资料 · ASSET INFO', 'ASSET INFO'],
}

type StepKind = 'upsert' | 'quarantine' | 'mixed' | 'observed' | 'reobserved' | 'valid_from' | 'invalidated' | 'undated'
type LiveStep = {
  key: string
  memoryId: string
  memoryIds: string[]
  kind: StepKind
  at: string | null
  offset: number | null
  offsetEnd: number | null
  changedFields: string[]
  changesByMemory: Record<string, string[]>
  events: LiveMemoryEvent[]
}
type MemoryView = 'recent' | 'active' | 'shelved' | 'all'

const TIER_SHORT: Record<MemTier, [string, string]> = {
  semantic: ['规律', 'PATTERN'],
  procedural: ['方法', 'HOW-TO'],
  episodic: ['案例', 'CASE'],
  asset_profile: ['资产', 'ASSET'],
}

const recordTime = (record: LiveMemoryListRecord): number => {
  const value = record.last_observed_at ?? record.first_observed_at ?? record.valid_from
  const parsed = value ? Date.parse(value) : Number.NaN
  return Number.isNaN(parsed) ? 0 : parsed
}

const shortTime = (value: string | null, zh: boolean): string => {
  if (!value) return '∅'
  const parsed = Date.parse(value)
  if (Number.isNaN(parsed)) return value
  return new Intl.DateTimeFormat(zh ? 'zh-CN' : 'en-GB', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(parsed))
}

const STEP_LABEL: Record<StepKind, [string, string]> = {
  upsert: ['写入一个版本', 'VERSION WRITTEN'],
  quarantine: ['打入冷宫', 'SHELVED'],
  mixed: ['批量写入', 'BATCH WRITTEN'],
  observed: ['第一次见到', 'FIRST SEEN'],
  reobserved: ['最近一次见到', 'LAST SEEN'],
  valid_from: ['有效期开始', 'VALIDITY OPENED'],
  invalidated: ['有效期结束', 'VALIDITY CLOSED'],
  undated: ['无时间记录', 'TIME UNRECORDED'],
}

const fieldsAtStep = (step: LiveStep | null, memoryId: string): string[] =>
  step?.changesByMemory[memoryId] ?? []

const quarantineTag = (record: Pick<LiveMemoryListRecord, 'tags'>): string | null => {
  const reasons = record.tags.filter((tag) => tag.startsWith('quarantine:'))
  return reasons.length ? reasons[reasons.length - 1] : null
}

const timeValue = (value: string | null): number => {
  if (!value) return Number.POSITIVE_INFINITY
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? Number.POSITIVE_INFINITY : parsed
}

function buildSteps(records: LiveMemoryListRecord[]): LiveStep[] {
  const steps: LiveStep[] = []
  for (const record of records) {
    const first = record.first_observed_at ?? record.valid_from
    if (first) {
      const changedFields = ['record']
      if (record.first_observed_at) changedFields.push('first_observed_at')
      if (record.valid_from && record.valid_from === first) changedFields.push('valid_from')
      steps.push({
        key: `${record.memory_id}:observed:${first}`,
        memoryId: record.memory_id,
        memoryIds: [record.memory_id],
        kind: 'observed',
        at: first,
        offset: null,
        offsetEnd: null,
        changedFields,
        changesByMemory: { [record.memory_id]: changedFields },
        events: [],
      })
    } else {
      steps.push({
        key: `${record.memory_id}:undated`,
        memoryId: record.memory_id,
        memoryIds: [record.memory_id],
        kind: 'undated',
        at: null,
        offset: null,
        offsetEnd: null,
        changedFields: ['record'],
        changesByMemory: { [record.memory_id]: ['record'] },
        events: [],
      })
    }
    if (record.last_observed_at && record.last_observed_at !== first) {
      steps.push({
        key: `${record.memory_id}:reobserved:${record.last_observed_at}`,
        memoryId: record.memory_id,
        memoryIds: [record.memory_id],
        kind: 'reobserved',
        at: record.last_observed_at,
        offset: null,
        offsetEnd: null,
        changedFields: ['last_observed_at'],
        changesByMemory: { [record.memory_id]: ['last_observed_at'] },
        events: [],
      })
    }
    if (record.valid_from && record.valid_from !== first) {
      steps.push({
        key: `${record.memory_id}:valid_from:${record.valid_from}`,
        memoryId: record.memory_id,
        memoryIds: [record.memory_id],
        kind: 'valid_from',
        at: record.valid_from,
        offset: null,
        offsetEnd: null,
        changedFields: ['valid_from'],
        changesByMemory: { [record.memory_id]: ['valid_from'] },
        events: [],
      })
    }
    if (record.valid_to) {
      steps.push({
        key: `${record.memory_id}:invalidated:${record.valid_to}`,
        memoryId: record.memory_id,
        memoryIds: [record.memory_id],
        kind: 'invalidated',
        at: record.valid_to,
        offset: null,
        offsetEnd: null,
        changedFields: ['valid_to'],
        changesByMemory: { [record.memory_id]: ['valid_to'] },
        events: [],
      })
    }
  }
  const kindRank: Record<StepKind, number> = { upsert: 0, quarantine: 0, mixed: 0, observed: 0, valid_from: 1, reobserved: 2, invalidated: 3, undated: 4 }
  return steps.sort((left, right) => {
    const byTime = timeValue(left.at) - timeValue(right.at)
    if (Number.isFinite(byTime) && byTime !== 0) return byTime
    const byKind = kindRank[left.kind] - kindRank[right.kind]
    return byKind || left.memoryId.localeCompare(right.memoryId)
  })
}

const eventChangedFields = (event: LiveMemoryEvent, previous?: LiveMemoryEvent): string[] => {
  if (!previous) return ['record']
  const changed: string[] = []
  if (event.tier !== previous.tier) changed.push('tier')
  if (event.text_head !== previous.text_head) changed.push('text')
  if (event.confidence !== previous.confidence) changed.push('confidence')
  if (event.importance !== previous.importance) changed.push('importance')
  if (event.strength !== previous.strength) changed.push('strength')
  if (event.quarantine_reason !== previous.quarantine_reason) changed.push('quarantine_reason')
  if (event.event_type === 'QUARANTINE') changed.push('quarantined')
  return changed
}

function buildEventSteps(input: LiveMemoryEvent[]): LiveStep[] {
  const events = [...input].sort((left, right) => left.offset - right.offset)
  const previous = new Map<string, LiveMemoryEvent>()
  const steps: LiveStep[] = []
  for (const event of events) {
    const changedFields = eventChangedFields(event, previous.get(event.memory_id))
    previous.set(event.memory_id, event)
    const last = steps[steps.length - 1]
    // PostgreSQL transaction timestamps are identical for a batch. Grouping
    // them makes one quarantine transaction one replay point while offsets
    // still define the ledger order before and after that point.
    if (last && last.at === event.occurred_at) {
      last.events.push(event)
      if (!last.memoryIds.includes(event.memory_id)) last.memoryIds.push(event.memory_id)
      last.changesByMemory[event.memory_id] = changedFields
      last.offsetEnd = event.offset
      last.kind = last.events.every((item) => item.event_type === 'QUARANTINE')
        ? 'quarantine'
        : last.events.every((item) => item.event_type === 'UPSERT') ? 'upsert' : 'mixed'
      last.key = `ledger:${last.offset}-${last.offsetEnd}`
      continue
    }
    steps.push({
      key: `ledger:${event.offset}`,
      memoryId: event.memory_id,
      memoryIds: [event.memory_id],
      kind: event.event_type === 'QUARANTINE' ? 'quarantine' : 'upsert',
      at: event.occurred_at,
      offset: event.offset,
      offsetEnd: event.offset,
      changedFields,
      changesByMemory: { [event.memory_id]: changedFields },
      events: [event],
    })
  }
  return steps
}

type MemoryEventPage = { events: LiveMemoryEvent[]; highWater: number }

async function readMemoryEvents(signal: AbortSignal, startAfter = 0): Promise<MemoryEventPage | null> {
  const events: LiveMemoryEvent[] = []
  let after = startAfter
  let highWater = startAfter
  while (!signal.aborted) {
    const response = await fetch(`/api/rca/memory/events?after=${after}&limit=2000`, {
      headers: { Accept: 'application/json' },
      signal,
    })
    if (!response.ok) return null
    const payload = await response.json() as LiveMemoryEventsResponse
    if (!payload.ok || !payload.durable) return null
    events.push(...payload.events)
    highWater = Math.max(highWater, payload.high_water ?? after, ...payload.events.map((event) => event.offset))
    if (payload.next_offset == null) return { events, highWater }
    if (payload.next_offset <= after) return null
    after = payload.next_offset
  }
  return null
}

function ChipList({ items }: { items: string[] }) {
  if (!items.length) return <span className="lm-none">∅</span>
  return <span className="lm-chips">{items.map((item) => <i key={item}>{item}</i>)}</span>
}

function AuditRow({ label, changed = false, children }: { label: string; changed?: boolean; children: ReactNode }) {
  return (
    <div className={changed ? 'lm-audit-row changed' : 'lm-audit-row'}>
      <span className="lm-audit-key">{label}</span>
      <span className="lm-audit-value">{children}</span>
    </div>
  )
}

function LiveMemoryInspector({
  summary,
  detail,
  step,
  loading,
  zh,
}: {
  summary: LiveMemoryListRecord | null
  detail: LiveMemoryDetailRecord | null
  step: LiveStep | null
  loading: boolean
  zh: boolean
}) {
  if (!summary) {
    return (
      <aside className="lm-inspector empty">
        <div className="lm-inspector-mast">{zh ? '本机记忆 · 每一步改了什么' : "THIS HOST'S MEMORY · WHAT CHANGED"}</div>
        <div className="lm-inspector-empty">
          <b>{zh ? '未选中条目' : 'NO RECORD SELECTED'}</b>
          <span>{zh ? '点击任一记录卡片可查看完整来源。' : 'Select any record card to view its full source.'}</span>
        </div>
      </aside>
    )
  }

  const changed = new Set(fieldsAtStep(step, summary.memory_id))
  const reason = quarantineTag(summary)
  const record = detail ? {
    ...detail,
    tier: summary.tier,
    text: summary.text,
    confidence: summary.confidence,
    importance: summary.importance,
    strength: summary.strength,
    quarantined: summary.quarantined,
  } : summary

  return (
    <aside className="lm-inspector" aria-live="polite">
      <header className="lm-inspector-mast">
        <span>{zh ? '本机记忆 · 每一步改了什么' : "THIS HOST'S MEMORY · WHAT CHANGED"}</span>
        <span>{zh ? '只读' : 'READ ONLY'}</span>
      </header>
      <div className="lm-inspector-mode">
        <span>{zh ? '已锁定' : 'PINNED'}</span>
        <b title={summary.memory_id}>{summary.memory_id}</b>
      </div>
      {loading ? <div className="lm-inspector-status">{zh ? '正在读取完整来源…' : 'LOADING FULL SOURCE…'}</div> : null}
      <div className="lm-inspector-scroll">
        <div className={changed.has('record') ? 'lm-detail-id changed' : 'lm-detail-id'}>
          <span>{TIER_LABEL[record.tier][zh ? 0 : 1]}</span>
          <b>{record.memory_id}</b>
          {record.quarantined ? <i>{zh ? '已打入冷宫' : 'SHELVED'}</i> : null}
        </div>

        <section className="lm-audit-section">
          <h4>{zh ? '全文' : 'FULL TEXT'}</h4>
          <p className="lm-full-text">{record.text}</p>
        </section>

        <section className="lm-audit-section">
          <h4>{zh ? '游标处动作' : 'ACTION AT CURSOR'}</h4>
          {step?.memoryIds.includes(summary.memory_id) ? (
            <div className="lm-step-readout">
              <b>{STEP_LABEL[step.kind][zh ? 0 : 1]}</b>
              <span>{step.at ?? (zh ? '时间未记录' : 'TIME UNRECORDED')}</span>
              <i>{fieldsAtStep(step, summary.memory_id).join(' · ') || (zh ? '该版本未暴露可比较字段变化' : 'NO EXPOSED FIELD DELTA IN THIS VERSION')}</i>
            </div>
          ) : (
            <p className="lm-audit-note">{zh ? '本步没有改变这条记录。' : 'THIS RECORD IS UNTOUCHED AT THIS STEP.'}</p>
          )}
        </section>

        <section className="lm-audit-section">
          <h4>{zh ? '当前记录的值' : 'CURRENT VALUES'}</h4>
          <div className="lm-state-grid">
            <div><b>{record.confidence.toFixed(2)}</b><span>{zh ? '可信度' : 'CONFIDENCE'}</span></div>
            <div><b>{record.importance.toFixed(2)}</b><span>{zh ? '重要度' : 'IMPORTANCE'}</span></div>
            <div><b>{record.strength.toFixed(2)}</b><span>{zh ? '保留强度' : 'RETENTION'}</span></div>
          </div>
          <AuditRow label={zh ? '访问次数' : 'ACCESS COUNT'}>{record.access_count}</AuditRow>
          <AuditRow label="FIRST_OBSERVED" changed={changed.has('first_observed_at')}>{record.first_observed_at ?? '∅'}</AuditRow>
          <AuditRow label="LAST_OBSERVED" changed={changed.has('last_observed_at')}>{record.last_observed_at ?? '∅'}</AuditRow>
          <AuditRow label="VALID_FROM" changed={changed.has('valid_from')}>{record.valid_from ?? '∅'}</AuditRow>
          <AuditRow label="VALID_TO" changed={changed.has('valid_to')}>{record.valid_to ?? '∅'}</AuditRow>
        </section>

        <section className="lm-audit-section">
          <h4>{zh ? '来源与关系' : 'SOURCE AND LINKS'}</h4>
          <AuditRow label="TAGS"><ChipList items={record.tags} /></AuditRow>
          <AuditRow label="ASSET_IDS"><ChipList items={record.asset_ids} /></AuditRow>
          <AuditRow label="SOURCE_TRACE_IDS">
            {detail ? <><b className="lm-trace-count">{detail.source_trace_ids.length} {zh ? '条' : 'TRACES'}</b><ChipList items={detail.source_trace_ids} /></> : summary.source_trace_ids}
          </AuditRow>
          {detail ? (
            <>
              <AuditRow label="EVIDENCE_IDS"><ChipList items={detail.evidence_ids} /></AuditRow>
              <AuditRow label="LINKS"><ChipList items={detail.links} /></AuditRow>
              <AuditRow label="RELATIONS">
                {detail.relations.length ? (
                  <span className="lm-relations">
                    {detail.relations.map((relation, index) => (
                      <i key={`${relation.target_id}-${relation.relation_type}-${index}`}>
                        <b>{relation.relation_type}</b> → {relation.target_id} · {relation.confidence.toFixed(2)} · evidence {relation.evidence_ids.length}
                      </i>
                    ))}
                  </span>
                ) : <span className="lm-none">∅</span>}
              </AuditRow>
              <AuditRow label="EVIDENCE_SNAPSHOT">{detail.evidence_snapshot.count} · <ChipList items={detail.evidence_snapshot.evidence_ids} /></AuditRow>
              <AuditRow label="SUPERSEDED_BY" changed={changed.has('superseded_by')}>{detail.superseded_by ?? '∅'}</AuditRow>
            </>
          ) : null}
          {record.quarantined ? (
            <AuditRow label={zh ? '打入冷宫的原因原文' : 'RAW REASON IT WAS SHELVED'}>
              <span className="lm-quarantine-reason">{reason ?? '∅'}</span>
            </AuditRow>
          ) : null}
        </section>
      </div>
    </aside>
  )
}

function Timeline({
  steps,
  cursor,
  playing,
  onCursor,
  onToggle,
  eventLedger,
  zh,
}: {
  steps: LiveStep[]
  cursor: number
  playing: boolean
  onCursor: (index: number) => void
  onToggle: () => void
  eventLedger: boolean
  zh: boolean
}) {
  const current = steps[cursor] ?? null
  const last = Math.max(0, steps.length - 1)
  const pct = (index: number) => `${steps.length <= 1 ? 0 : (index / last) * 100}%`
  const counts = useMemo(() => {
    const value: Record<StepKind, number> = { upsert: 0, quarantine: 0, mixed: 0, observed: 0, reobserved: 0, valid_from: 0, invalidated: 0, undated: 0 }
    for (const step of steps) value[step.kind] += 1
    return value
  }, [steps])
  const tickClass = (kind: StepKind) => kind === 'quarantine' || kind === 'mixed' ? 'invalidated' : kind === 'upsert' ? 'observed' : kind
  const offsetLabel = (step: LiveStep) => step.offset === step.offsetEnd
    ? String(step.offset)
    : `${step.offset}-${step.offsetEnd}`
  const subject = (step: LiveStep) => step.memoryIds.length === 1
    ? step.memoryId
    : (zh ? `${step.memoryIds.length} 条记录` : `${step.memoryIds.length} RECORDS`)

  return (
    <section className="lm-timeline" aria-label={zh ? '本机记忆变化时间线' : "This host's memory change timeline"}>
      <div className="lm-timeline-side">
        <div className="lm-transport">
          <button type="button" onClick={() => onCursor(0)} disabled={!steps.length || cursor === 0} aria-label={zh ? '回到开头' : 'Reset'}>|◀</button>
          <button type="button" onClick={() => onCursor(cursor - 1)} disabled={cursor <= 0} aria-label={zh ? '上一步' : 'Step back'}>◀</button>
          <button type="button" className="lm-play" onClick={onToggle} disabled={!steps.length} aria-pressed={playing} aria-label={playing ? (zh ? '暂停' : 'Pause') : (zh ? '播放' : 'Play')}>{playing ? '❚❚' : '▶'}</button>
          <button type="button" onClick={() => onCursor(cursor + 1)} disabled={cursor >= last} aria-label={zh ? '下一步' : 'Step forward'}>▶</button>
        </div>
        <div className="lm-timeline-legend">
          {(Object.keys(counts) as StepKind[]).filter((kind) => counts[kind] > 0).map((kind) => (
            <span key={kind} className={tickClass(kind)}><i /><b>{counts[kind]}</b>{STEP_LABEL[kind][zh ? 0 : 1]}</span>
          ))}
        </div>
      </div>
      <div className="lm-timeline-plot">
        <div className="lm-timeline-ticks" aria-hidden="true">
          {steps.map((step, index) => (
            <i key={step.key} className={`${tickClass(step.kind)}${index === cursor ? ' current' : ''}${index < cursor ? ' past' : ''}`} style={{ left: pct(index) }} />
          ))}
          <span className="lm-timeline-fill" style={{ width: pct(cursor) }} />
          <span className="lm-timeline-head" style={{ left: pct(cursor) }} />
        </div>
        <input
          type="range"
          min={0}
          max={last}
          value={Math.min(cursor, last)}
          onChange={(event) => onCursor(Number(event.currentTarget.value))}
          aria-label={eventLedger ? (zh ? '沿事件 offset 拖动游标' : 'Scrub event offsets') : (zh ? '沿观测时间拖动游标' : 'Scrub observation time')}
          aria-valuetext={current ? `${STEP_LABEL[current.kind][zh ? 0 : 1]} ${subject(current)} ${current.at ?? ''}` : ''}
        />
        <div className="lm-timeline-bounds"><span>{steps[0]?.at ?? '∅'}</span><span>{steps[last]?.at ?? '∅'}</span></div>
      </div>
      <div className="lm-timeline-readout">
        <span>{eventLedger ? 'OFFSET' : (zh ? '步骤' : 'STEP')} <b>{current && eventLedger ? offsetLabel(current) : String(cursor + 1).padStart(2, '0')}</b>{eventLedger ? null : `/${steps.length}`}</span>
        {current ? <><i>{STEP_LABEL[current.kind][zh ? 0 : 1]}</i><strong>{subject(current)}</strong><time>{current.at ?? (zh ? '时间未记录' : 'TIME UNRECORDED')}</time></> : null}
      </div>
    </section>
  )
}

export function LiveMemory({ lang }: { lang: Lang }) {
  const zh = lang === 'zh'
  const rootRef = useRef<HTMLElement | null>(null)
  const ledgerOffset = useRef(0)
  const [data, setData] = useState<LiveMemoryListResponse | null>(null)
  const [ledgerEvents, setLedgerEvents] = useState<LiveMemoryEvent[] | null>(null)
  const [selectedDetail, setSelectedDetail] = useState<LiveMemoryDetailRecord | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cursor, setCursor] = useState(0)
  const cursorInitialized = useRef(false)
  const [playing, setPlaying] = useState(false)
  const [onScreen, setOnScreen] = useState(() => typeof IntersectionObserver !== 'function')
  const [pinnedId, setPinnedId] = useState<string | null>(null)
  const [viewMode, setViewMode] = useState<MemoryView>('recent')
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState(readInitialExpanded)

  useEffect(() => {
    try {
      window.sessionStorage.setItem(EXPANDED_STORAGE_KEY, String(expanded))
    } catch {
      // Storage can be unavailable in privacy-restricted browser contexts. The
      // control remains usable for the current mount.
    }
  }, [expanded])

  useEffect(() => {
    const expandForHash = () => {
      if (window.location.hash === '#live-memory') setExpanded(true)
    }
    const expandForLink = (event: MouseEvent) => {
      const target = event.target
      if (!(target instanceof Element)) return
      const link = target.closest<HTMLAnchorElement>('a[href="#live-memory"]')
      if (link) setExpanded(true)
    }
    expandForHash()
    window.addEventListener('hashchange', expandForHash)
    document.addEventListener('click', expandForLink)
    return () => {
      window.removeEventListener('hashchange', expandForHash)
      document.removeEventListener('click', expandForLink)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined
    const keepRefreshing = expanded && onScreen
    const load = () => {
      fetch('/api/rca/memory?limit=1000&include_quarantined=true', {
        headers: { Accept: 'application/json' }, signal: controller.signal,
      })
        .then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          return response.json() as Promise<LiveMemoryListResponse>
        })
        .then((payload) => { setData(payload); setError(null) })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return
          setError(zh ? `线上记忆读取失败：${String(reason)}` : `Failed to read live memory: ${String(reason)}`)
        })
        .finally(() => {
          if (!controller.signal.aborted && keepRefreshing) timer = window.setTimeout(load, 30000)
        })
    }
    load()
    return () => { controller.abort(); if (timer !== undefined) window.clearTimeout(timer) }
  }, [expanded, onScreen, zh])

  useEffect(() => {
    if (!expanded || !onScreen) return
    const controller = new AbortController()
    let timer: number | undefined
    const load = () => {
      readMemoryEvents(controller.signal, ledgerOffset.current)
        .then((page) => {
          if (controller.signal.aborted || page === null) return
          ledgerOffset.current = page.highWater
          setLedgerEvents((current) => {
            if (!page.events.length) return current ?? []
            const merged = new Map((current ?? []).map((event) => [event.offset, event]))
            for (const event of page.events) merged.set(event.offset, event)
            return [...merged.values()].sort((left, right) => left.offset - right.offset)
          })
        })
        .catch(() => {
          if (!controller.signal.aborted) setLedgerEvents((current) => current ?? null)
        })
        .finally(() => {
          if (!controller.signal.aborted) timer = window.setTimeout(load, 5000)
        })
    }
    load()
    return () => { controller.abort(); if (timer !== undefined) window.clearTimeout(timer) }
  }, [expanded, onScreen])

  useEffect(() => {
    const root = rootRef.current
    if (!root || typeof IntersectionObserver !== 'function') return
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry) return
      const viewport = window.innerHeight || 1
      setOnScreen(entry.intersectionRatio >= 0.25 || entry.intersectionRect.height >= viewport * 0.35)
    }, { threshold: [0, 0.25, 0.5, 1] })
    observer.observe(root)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const release = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setPinnedId(null)
    }
    window.addEventListener('keydown', release)
    return () => window.removeEventListener('keydown', release)
  }, [])

  const eventLedger = ledgerEvents !== null && ledgerEvents.length > 0
  const steps = useMemo(
    () => eventLedger ? buildEventSteps(ledgerEvents) : buildSteps(data?.records ?? []),
    [data, eventLedger, ledgerEvents],
  )
  const last = Math.max(0, steps.length - 1)
  const atEnd = cursor >= last
  const running = expanded && playing && onScreen && !atEnd && steps.length > 0

  useEffect(() => {
    if (!steps.length) return
    let active = true
    queueMicrotask(() => {
      if (!active) return
      if (!cursorInitialized.current) {
        cursorInitialized.current = true
        setCursor(Math.max(0, steps.length - 1))
        return
      }
      setCursor((value) => Math.min(value, Math.max(0, steps.length - 1)))
    })
    return () => { active = false }
  }, [steps])

  useEffect(() => {
    if (!running) return
    const timer = window.setTimeout(() => setCursor((value) => Math.min(last, value + 1)), TICK_MS)
    return () => window.clearTimeout(timer)
  }, [cursor, last, running])

  const currentStep = steps[cursor] ?? null
  const firstStep = useMemo(() => {
    const values = new Map<string, number>()
    steps.forEach((step, index) => {
      for (const memoryId of step.memoryIds) {
        if (!values.has(memoryId)) values.set(memoryId, index)
      }
    })
    return values
  }, [steps])
  const visibleIds = useMemo(() => new Set(
    eventLedger
      ? steps.slice(0, cursor + 1).flatMap((step) => step.memoryIds)
      : (data?.records ?? []).filter((record) => (firstStep.get(record.memory_id) ?? Number.POSITIVE_INFINITY) <= cursor).map((record) => record.memory_id),
  ), [cursor, data, eventLedger, firstStep, steps])
  const eventsAtCursor = useMemo(() => {
    const values = new Map<string, LiveMemoryEvent>()
    if (!eventLedger) return values
    for (const step of steps.slice(0, cursor + 1)) {
      for (const event of step.events) values.set(event.memory_id, event)
    }
    return values
  }, [cursor, eventLedger, steps])
  const displayRecords = useMemo(() => (data?.records ?? []).map((record) => {
    const event = eventsAtCursor.get(record.memory_id)
    if (!event) return record
    return {
      ...record,
      tier: event.tier,
      text: event.text_head,
      confidence: event.confidence,
      importance: event.importance,
      strength: event.strength,
      quarantined: event.event_type === 'QUARANTINE' || event.quarantine_reason !== null,
    }
  }), [data, eventsAtCursor])
  const selectedId = pinnedId
  const selectedSummary = displayRecords.find((record) => record.memory_id === selectedId) ?? null
  useEffect(() => {
    if (!selectedId) {
      let active = true
      queueMicrotask(() => {
        if (!active) return
        setSelectedDetail(null)
        setDetailLoading(false)
      })
      return () => { active = false }
    }
    const controller = new AbortController()
    queueMicrotask(() => {
      if (controller.signal.aborted) return
      setDetailLoading(true)
      setSelectedDetail(null)
    })
    fetch(`/api/rca/memory/${encodeURIComponent(selectedId)}`, {
      headers: { Accept: 'application/json' }, signal: controller.signal,
    })
      .then((response) => response.ok ? response.json() as Promise<LiveMemoryDetailResponse> : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((payload) => { if (!controller.signal.aborted) setSelectedDetail(payload.record) })
      .catch(() => {})
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false) })
    return () => controller.abort()
  }, [selectedId])

  const tierCounts = useMemo(() => Object.fromEntries(TIER_ORDER.map((tier) => [
    tier,
    displayRecords.filter((record) => record.tier === tier && !record.quarantined).length,
  ])) as Record<MemTier, number>, [displayRecords])

  const tableRecords = useMemo(() => {
    const term = query.trim().toLowerCase()
    let records = displayRecords.filter((record) => visibleIds.has(record.memory_id))
    if (viewMode === 'active') records = records.filter((record) => !record.quarantined)
    if (viewMode === 'shelved') records = records.filter((record) => record.quarantined)
    if (term) records = records.filter((record) => `${record.memory_id} ${record.text} ${record.tags.join(' ')} ${record.asset_ids.join(' ')}`.toLowerCase().includes(term))
    records = [...records].sort((left, right) => recordTime(right) - recordTime(left) || left.memory_id.localeCompare(right.memory_id))
    return viewMode === 'recent' ? records.slice(0, 18) : records
  }, [displayRecords, query, viewMode, visibleIds])

  const scrub = (index: number) => {
    setPlaying(false)
    setCursor(Math.max(0, Math.min(last, index)))
  }
  const toggle = () => {
    if (atEnd) {
      setCursor(0)
      setPlaying(true)
    } else {
      setPlaying((value) => !value)
    }
  }

  const activeCount = data?.records.filter((record) => !record.quarantined).length ?? 0
  const quarantineCount = data?.records.filter((record) => record.quarantined).length ?? 0
  const total = activeCount + quarantineCount
  const visibleActive = displayRecords.filter((record) => visibleIds.has(record.memory_id) && !record.quarantined).length
  const visibleQuarantined = displayRecords.filter((record) => visibleIds.has(record.memory_id) && record.quarantined).length
  const latestWrite = useMemo(() => {
    const eventTimes = ledgerEvents?.map((event) => event.occurred_at) ?? []
    const snapshotTimes = (data?.records ?? []).flatMap((record) => [
      record.last_observed_at,
      record.first_observed_at,
      record.valid_from,
      record.valid_to,
    ]).filter((value): value is string => value !== null)
    const writeTimes = eventTimes.length ? eventTimes : snapshotTimes
    const latest = writeTimes.reduce<string | null>((current, value) => {
      const parsed = Date.parse(value)
      if (Number.isNaN(parsed)) return current
      if (current === null || parsed > Date.parse(current)) return value
      return current
    }, null)
    if (!latest) return data ? '∅' : '…'
    return new Intl.DateTimeFormat(zh ? 'zh-CN' : 'en-GB', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date(latest))
  }, [data, ledgerEvents, zh])

  return (
    <section id="live-memory" className="lm-root" ref={rootRef} aria-label={zh ? '本机记忆' : "This host's memory"}>
      <header className="lm-dashboard-head">
        <div className="lm-dashboard-title">
          <span>03 · {zh ? '在线记忆' : 'ONLINE MEMORY'}</span>
          <h2>{zh ? '本机记忆演化' : 'LIVE MEMORY EVOLUTION'}</h2>
          <p>{zh ? '近期变化优先，完整审计按需展开' : 'RECENT CHANGES FIRST, FULL AUDIT ON DEMAND'}</p>
        </div>
        <div className="lm-health" aria-live="polite">
          <div className="lm-health-ratio" aria-label={`${activeCount} active, ${quarantineCount} shelved`}>
            <i style={{ width: `${total ? (activeCount / total) * 100 : 0}%` }} />
          </div>
          <div className="lm-health-number active"><b>{data ? activeCount : '…'}</b><span>{zh ? '可检索' : 'ACTIVE'}</span></div>
          <div className="lm-health-number shelved"><b>{data ? quarantineCount : '…'}</b><span>{zh ? '已隔离' : 'SHELVED'}</span></div>
          <div className="lm-health-write"><span>{zh ? '最近写入' : 'LAST WRITE'}</span><b>{latestWrite}</b></div>
        </div>
        <button
          type="button"
          className="lm-expand"
          aria-expanded={expanded}
          aria-controls="live-memory-content"
          onClick={() => setExpanded((value) => !value)}
        >{expanded ? (zh ? '收起审计' : 'CLOSE AUDIT') : (zh ? '查看变化与审计' : 'OPEN CHANGES & AUDIT')}</button>
      </header>
      <div id="live-memory-content" hidden={!expanded}>
      {!data && !error ? <div className="lm-message">{zh ? '正在读取本机记忆…' : "READING THIS HOST'S MEMORY…"}</div> : null}
      {error ? <div className="lm-message error">{error}</div> : null}
      {data && !data.durable ? (
        <div className="lm-message nondurable">
          <b>{zh ? '当前没有重启后仍保留的本机记忆' : 'NO MEMORY THAT SURVIVES A RESTART IS AVAILABLE'}</b>
          <span>{zh ? '接口返回 durable=false，当前进程没有连接 PostgreSQL 记忆库。' : 'The API returned durable=false; this process is not connected to the PostgreSQL memory store.'}</span>
        </div>
      ) : null}

      {data?.durable ? (
        <>
          <div className="lm-tier-strip">
            {TIER_ORDER.map((tier) => (
              <div key={tier}><span>{TIER_SHORT[tier][zh ? 0 : 1]}</span><b>{tierCounts[tier]}</b></div>
            ))}
            <div className="lm-visible-count"><span>{zh ? '游标可见' : 'AT CURSOR'}</span><b>{visibleActive + visibleQuarantined}/{total}</b></div>
            <details className="lm-method-note">
              <summary>{zh ? '数据口径' : 'DATA SCOPE'}</summary>
              <p>{eventLedger
                ? (zh ? '按 PostgreSQL 写入 offset 回放；同一事务合并为一步。完整来源只在点开单条记忆时读取。' : 'Replayed by PostgreSQL write offset; one transaction is one step. Full provenance loads only when a row is opened.')
                : (zh ? '当前使用记录中的首次、最近和有效期时间构造变化顺序。' : 'The sequence uses first, latest, and validity timestamps from current records.')}</p>
            </details>
          </div>

          <Timeline steps={steps} cursor={cursor} playing={running} onCursor={scrub} onToggle={toggle} eventLedger={eventLedger} zh={zh} />

          <section className="lm-ledger" aria-label={zh ? '在线记忆列表' : 'Online memory ledger'}>
            <header className="lm-ledger-tools">
              <div className="lm-view-tabs" role="group" aria-label={zh ? '记忆筛选' : 'Memory filter'}>
                {(['recent', 'active', 'shelved', 'all'] as MemoryView[]).map((mode) => {
                  const labels: Record<MemoryView, [string, string]> = {
                    recent: ['近期变化', 'RECENT'], active: ['可检索', 'ACTIVE'], shelved: ['已隔离', 'SHELVED'], all: ['全部', 'ALL'],
                  }
                  return <button type="button" className={viewMode === mode ? 'on' : ''} key={mode} onClick={() => setViewMode(mode)}>{labels[mode][zh ? 0 : 1]}</button>
                })}
              </div>
              <label className="lm-search">
                <span>{zh ? '过滤' : 'FILTER'}</span>
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={zh ? 'ID / 资产 / 标签' : 'ID / ASSET / TAG'} />
              </label>
              <span className="lm-result-count">{tableRecords.length} {zh ? '条' : 'ROWS'}</span>
            </header>
            <div className="lm-table-head" aria-hidden="true">
              <span>{zh ? '状态' : 'STATE'}</span><span>{zh ? '类型' : 'TYPE'}</span><span>ID</span><span>{zh ? '内容摘要' : 'SUMMARY'}</span><span>{zh ? '最近观察' : 'LAST SEEN'}</span><span>{zh ? '采用' : 'USES'}</span>
            </div>
            <div className="lm-table-body">
              {tableRecords.map((record) => {
                const touched = currentStep?.memoryIds.includes(record.memory_id) ?? false
                return (
                  <button
                    type="button"
                    key={record.memory_id}
                    className={`lm-table-row${record.quarantined ? ' shelved' : ''}${selectedId === record.memory_id ? ' selected' : ''}${touched ? ' touched' : ''}`}
                    onClick={() => setPinnedId((value) => value === record.memory_id ? null : record.memory_id)}
                  >
                    <span className="lm-row-state"><i />{record.quarantined ? (zh ? '隔离' : 'SHELVED') : (zh ? '可用' : 'ACTIVE')}</span>
                    <span className={`lm-row-tier tier-${record.tier}`}>{TIER_SHORT[record.tier][zh ? 0 : 1]}</span>
                    <b className="lm-row-id" title={record.memory_id}>{record.memory_id}</b>
                    <span className="lm-row-text" title={record.text}>{record.text}</span>
                    <time>{shortTime(record.last_observed_at ?? record.first_observed_at, zh)}</time>
                    <strong>×{record.access_count}</strong>
                  </button>
                )
              })}
              {!tableRecords.length ? <div className="lm-table-empty">{zh ? '当前筛选没有记录' : 'NO RECORDS MATCH THIS VIEW'}</div> : null}
            </div>
          </section>

          {selectedSummary ? (
            <div className="lm-detail-sheet">
              <button type="button" className="lm-detail-close" onClick={() => setPinnedId(null)}>{zh ? '关闭详情' : 'CLOSE DETAIL'} ×</button>
            <LiveMemoryInspector
              summary={selectedSummary}
              detail={selectedDetail}
              step={currentStep}
              loading={detailLoading}
              zh={zh}
            />
            </div>
          ) : null}
        </>
      ) : null}
      </div>
    </section>
  )
}
