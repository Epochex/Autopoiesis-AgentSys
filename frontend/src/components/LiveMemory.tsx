import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
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
import { prefersReducedMotion } from '../reduced-motion'
import './live-memory.css'

const TICK_MS = 180
const EXPANDED_STORAGE_KEY = 'live-memory-expanded'
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
type EdgeSpec = {
  key: string
  source: string
  target: string
  kind: 'link' | 'relation' | 'superseded'
  label: string
}
type DrawnEdge = EdgeSpec & { d: string; lx: number; ly: number }

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

async function readMemoryEvents(signal: AbortSignal): Promise<LiveMemoryEvent[] | null> {
  const events: LiveMemoryEvent[] = []
  let after = 0
  while (!signal.aborted) {
    const response = await fetch(`/api/rca/memory/events?after=${after}&limit=2000`, {
      headers: { Accept: 'application/json' },
      signal,
    })
    if (!response.ok) return null
    const payload = await response.json() as LiveMemoryEventsResponse
    if (!payload.ok || !payload.durable) return null
    events.push(...payload.events)
    if (payload.next_offset == null) return events
    if (payload.next_offset <= after) return null
    after = payload.next_offset
  }
  return null
}

const squareBar = (value: number, ceiling: number) => {
  const filled = Math.round(Math.max(0, Math.min(1, value / ceiling)) * 4)
  return `${'▇'.repeat(filled)}${'▁'.repeat(4 - filled)}`
}

function Metric({ label, value, ceiling }: { label: string; value: number; ceiling: number }) {
  return (
    <span className="lm-metric">
      <i aria-hidden="true">{squareBar(value, ceiling)}</i>
      <span>{label}</span>
      <b>{value.toFixed(2)}</b>
    </span>
  )
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

function MemoryCard({
  record,
  visible,
  selected,
  changedFields,
  invalidated,
  onSelect,
  cardRef,
  zh,
}: {
  record: LiveMemoryListRecord
  visible: boolean
  selected: boolean
  changedFields: Set<string>
  invalidated: boolean
  onSelect: () => void
  cardRef: (node: HTMLButtonElement | null) => void
  zh: boolean
}) {
  const reason = quarantineTag(record)
  const classes = [
    'lm-card',
    visible ? 'visible' : 'pending',
    selected ? 'selected' : '',
    record.quarantined ? 'quarantined' : '',
    invalidated ? 'invalidated' : '',
  ].filter(Boolean).join(' ')
  return (
    <button
      ref={cardRef}
      type="button"
      className={classes}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className={changedFields.has('record') ? 'lm-card-id changed' : 'lm-card-id'} title={record.memory_id}>
        {record.memory_id}
        {visible && record.access_count > 0 ? <b title={zh ? '被找到或再次采用的次数' : 'TIMES FOUND OR REUSED'}>×{record.access_count}</b> : null}
      </span>
      {!visible ? <span className="lm-card-pending">{zh ? '未写入 · NOT YET WRITTEN' : 'NOT YET WRITTEN'}</span> : null}
      <span className="lm-card-content" aria-hidden={!visible}>
          <span className="lm-card-text">{record.text}</span>
          <span className="lm-card-metrics">
            <Metric label={zh ? '可信' : 'CONF'} value={record.confidence} ceiling={3} />
            <Metric label={zh ? '重要' : 'IMP'} value={record.importance} ceiling={52} />
            <Metric label={zh ? '保留' : 'KEEP'} value={record.strength} ceiling={1} />
          </span>
          <span className="lm-card-times">
            <i className={changedFields.has('first_observed_at') ? 'changed' : ''}>FIRST {record.first_observed_at ?? '∅'}</i>
            <i className={changedFields.has('last_observed_at') ? 'changed' : ''}>LAST {record.last_observed_at ?? '∅'}</i>
            {record.valid_to ? <i className={changedFields.has('valid_to') ? 'changed' : ''}>VALID_TO {record.valid_to}</i> : null}
          </span>
          {record.quarantined ? (
            <span className="lm-card-quarantine">
              <b>{zh ? '当前已打入冷宫' : 'SHELVED NOW'}</b>
              <i>{reason ?? (zh ? '没有记录原因' : 'NO REASON RECORDED')}</i>
            </span>
          ) : null}
      </span>
    </button>
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
  const graphRef = useRef<HTMLDivElement | null>(null)
  const cardRefs = useRef(new Map<string, HTMLButtonElement>())
  const [data, setData] = useState<LiveMemoryListResponse | null>(null)
  const [ledgerEvents, setLedgerEvents] = useState<LiveMemoryEvent[] | null>(null)
  const [details, setDetails] = useState<Map<string, LiveMemoryDetailRecord>>(new Map())
  const [error, setError] = useState<string | null>(null)
  const [cursor, setCursor] = useState(0)
  const [playing, setPlaying] = useState(() => !prefersReducedMotion())
  const [onScreen, setOnScreen] = useState(() => typeof IntersectionObserver !== 'function')
  const [pinnedId, setPinnedId] = useState<string | null>(null)
  const [drawnEdges, setDrawnEdges] = useState<DrawnEdge[]>([])
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
    fetch('/api/rca/memory?limit=1000&include_quarantined=true', {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json() as Promise<LiveMemoryListResponse>
      })
      .then((payload) => {
        setData(payload)
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setError(zh ? `线上记忆读取失败：${String(reason)}` : `Failed to read live memory: ${String(reason)}`)
      })
    return () => controller.abort()
  }, [zh])

  useEffect(() => {
    const controller = new AbortController()
    readMemoryEvents(controller.signal)
      .then((events) => {
        if (!controller.signal.aborted) setLedgerEvents(events)
      })
      .catch(() => {
        // Event replay is additive. A missing or older gateway must leave the
        // existing snapshot-derived timeline fully usable.
        if (!controller.signal.aborted) setLedgerEvents(null)
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!data?.durable || !data.records.length) return
    const controller = new AbortController()
    let next = 0
    const loaded = new Map<string, LiveMemoryDetailRecord>()
    const worker = async () => {
      while (!controller.signal.aborted) {
        const record = data.records[next++]
        if (!record) return
        try {
          const response = await fetch(`/api/rca/memory/${encodeURIComponent(record.memory_id)}`, {
            headers: { Accept: 'application/json' },
            signal: controller.signal,
          })
          if (!response.ok) continue
          const payload = await response.json() as LiveMemoryDetailResponse
          loaded.set(record.memory_id, payload.record)
          setDetails(new Map(loaded))
        } catch {
          if (controller.signal.aborted) return
        }
      }
    }
    void Promise.all(Array.from({ length: Math.min(6, data.records.length) }, () => worker()))
    return () => controller.abort()
  }, [data])

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
    setCursor(prefersReducedMotion() ? Math.max(0, steps.length - 1) : 0)
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
  const invalidatedAt = useMemo(() => {
    const values = new Map<string, number>()
    steps.forEach((step, index) => {
      if (step.kind === 'invalidated') values.set(step.memoryId, index)
    })
    return values
  }, [steps])
  const visibleIds = useMemo(() => new Set(
    eventLedger
      ? steps.slice(0, cursor + 1).flatMap((step) => step.memoryIds)
      : (data?.records ?? []).filter((record) => (firstStep.get(record.memory_id) ?? Number.POSITIVE_INFINITY) <= cursor).map((record) => record.memory_id),
  ), [cursor, data, eventLedger, firstStep, steps])
  const visibleKey = useMemo(() => Array.from(visibleIds).sort().join('|'), [visibleIds])
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
  const selectedId = pinnedId ?? currentStep?.memoryId ?? data?.records[0]?.memory_id ?? null
  const selectedSummary = displayRecords.find((record) => record.memory_id === selectedId) ?? null
  const selectedDetail = selectedId ? details.get(selectedId) ?? null : null

  const byTier = useMemo(() => {
    const grouped = new Map<MemTier, LiveMemoryListRecord[]>(TIER_ORDER.map((tier) => [tier, []]))
    for (const record of displayRecords) grouped.get(record.tier)?.push(record)
    return grouped
  }, [displayRecords])

  const edgeSpecs = useMemo(() => {
    const ids = new Set(data?.records.map((record) => record.memory_id) ?? [])
    const values: EdgeSpec[] = []
    const seen = new Set<string>()
    const push = (edge: Omit<EdgeSpec, 'key'>) => {
      if (!ids.has(edge.target) || edge.source === edge.target) return
      const pair = edge.kind === 'link' && edge.source > edge.target
        ? `${edge.target}|${edge.source}|${edge.kind}|${edge.label}`
        : `${edge.source}|${edge.target}|${edge.kind}|${edge.label}`
      if (seen.has(pair)) return
      seen.add(pair)
      values.push({ ...edge, key: pair })
    }
    for (const detail of details.values()) {
      for (const target of detail.links) push({ source: detail.memory_id, target, kind: 'link', label: 'link' })
      for (const relation of detail.relations) push({ source: detail.memory_id, target: relation.target_id, kind: 'relation', label: relation.relation_type })
      if (detail.superseded_by) push({ source: detail.memory_id, target: detail.superseded_by, kind: 'superseded', label: 'superseded_by' })
    }
    return values
  }, [data, details])

  const measureEdges = useCallback(() => {
    const graph = graphRef.current
    if (!graph) return
    const bounds = graph.getBoundingClientRect()
    const next: DrawnEdge[] = []
    for (const edge of edgeSpecs) {
      if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) continue
      const source = cardRefs.current.get(edge.source)
      const target = cardRefs.current.get(edge.target)
      if (!source || !target) continue
      const a = source.getBoundingClientRect()
      const b = target.getBoundingClientRect()
      const x1 = a.left - bounds.left + a.width / 2
      const y1 = a.top - bounds.top + a.height / 2
      const x2 = b.left - bounds.left + b.width / 2
      const y2 = b.top - bounds.top + b.height / 2
      const bend = y1 + (y2 - y1) / 2
      next.push({ ...edge, d: `M${x1} ${y1} L${x1} ${bend} L${x2} ${bend} L${x2} ${y2}`, lx: (x1 + x2) / 2, ly: bend })
    }
    setDrawnEdges(next)
  }, [edgeSpecs, visibleIds])

  useLayoutEffect(() => {
    measureEdges()
    const graph = graphRef.current
    if (!graph || typeof ResizeObserver !== 'function') return
    const observer = new ResizeObserver(measureEdges)
    observer.observe(graph)
    return () => observer.disconnect()
  }, [expanded, measureEdges, visibleKey])

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
      <div className="lm-collapse-summary">
        <span>
          {zh ? '本机记忆' : "THIS HOST'S MEMORY"}
          {' · '}{zh ? '活跃' : 'ACTIVE'} {data ? activeCount : '…'}
          {' · '}{zh ? '冷宫' : 'SHELVED'} {data ? quarantineCount : '…'}
          {' · '}{zh ? '最近写入' : 'LAST WRITE'} {latestWrite}
        </span>
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls="live-memory-content"
          onClick={() => setExpanded((value) => !value)}
        >[{expanded ? (zh ? '收起' : 'COLLAPSE') : (zh ? '展开' : 'EXPAND')}]</button>
      </div>
      <div id="live-memory-content" hidden={!expanded}>
      <header className="lm-head">
        <div>
          <h2>{zh ? '本机记忆 · 现在真的记得什么' : "THIS HOST'S MEMORY · WHAT IT ACTUALLY REMEMBERS"}</h2>
          <p>{zh ? '哨兵在生产中处置留下的。存在 PostgreSQL 里，重启还在。' : 'Left by Sentinel while handling production incidents. Stored in PostgreSQL and retained across restarts.'}</p>
        </div>
        <div className="lm-stats" aria-live="polite">
          <span>{zh ? '游标可见' : 'VISIBLE'} <b>{visibleActive + visibleQuarantined}</b>/{total}</span>
          <span>{zh ? '活跃' : 'ACTIVE'} <b>{activeCount}</b></span>
          <span>{zh ? '冷宫' : 'SHELVED'} <b>{quarantineCount}</b></span>
        </div>
      </header>

      {data?.durable ? (
        <div className="lm-source-note">
          <b>{eventLedger ? (zh ? '按每次写入回放' : 'REPLAY EACH WRITE') : (zh ? '只看现有时间记录' : 'AVAILABLE TIMES ONLY')}</b>
          <span>{eventLedger
            ? (zh ? '时间线按 offset 推进，同一时刻写入的内容合并显示。可以回放版本写入、打入冷宫，以及接口保存的文本和数值。关系、完整来源和接口没保存的版本差异只能显示当前值。' : 'The timeline advances by offset and groups writes made at the same time. It replays version writes, SHELVED actions, and saved text and values. Links, full source data, and version changes the API did not save use current values.')
            : (zh ? '当前接口没有提供每次写入记录，时间线只能使用第一次见到、最近一次见到和有效期。打入冷宫状态与连接来自当前值，具体动作时间和各版本差异无法回放。' : 'The API does not provide every write. The timeline uses first seen, last seen, and validity times. SHELVED state and links come from current values; exact action time and per-version changes cannot be replayed.')}</span>
        </div>
      ) : null}

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
          <div className="lm-ratio" aria-label={`${activeCount} active, ${quarantineCount} quarantined`}>
            <div className="lm-ratio-label"><span>{zh ? '当前数量比例' : 'CURRENT COUNT RATIO'}</span><b>{activeCount}:{quarantineCount}</b></div>
            <div className="lm-ratio-cells">
              {data.records.filter((record) => !record.quarantined).map((record) => <i className="active" key={record.memory_id} title={record.memory_id} />)}
              {data.records.filter((record) => record.quarantined).map((record) => <i className="quarantined" key={record.memory_id} title={`${record.memory_id} · ${quarantineTag(record) ?? ''}`} />)}
            </div>
          </div>

          <Timeline steps={steps} cursor={cursor} playing={running} onCursor={scrub} onToggle={toggle} eventLedger={eventLedger} zh={zh} />

          <div className="lm-body">
            <div className="lm-space" ref={graphRef}>
              <svg className="lm-edges" width="100%" height="100%" aria-label={zh ? '记忆关系连线' : 'Memory relation links'}>
                <defs>
                  <marker id="lm-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                    <path d="M0 0 L8 4 L0 8" />
                  </marker>
                </defs>
                {drawnEdges.map((edge) => (
                  <g key={edge.key} className={`lm-edge ${edge.kind}`}>
                    <path d={edge.d} markerEnd={edge.kind === 'link' ? undefined : 'url(#lm-arrow)'} />
                    {edge.kind !== 'link' ? <text x={edge.lx + 4} y={edge.ly - 4}>{edge.label}</text> : null}
                  </g>
                ))}
              </svg>
              {TIER_ORDER.map((tier) => {
                const records = byTier.get(tier) ?? []
                return (
                  <section className={`lm-tier tier-${tier}`} key={tier}>
                    <header>
                      <span>{TIER_LABEL[tier][zh ? 0 : 1]}</span>
                      <b>{records.filter((record) => visibleIds.has(record.memory_id)).length}/{records.length}</b>
                    </header>
                    <div className="lm-card-grid">
                      {records.map((record) => {
                        const touched = new Set(fieldsAtStep(currentStep, record.memory_id))
                        return (
                          <MemoryCard
                            key={record.memory_id}
                            record={record}
                            visible={visibleIds.has(record.memory_id)}
                            selected={selectedId === record.memory_id}
                            changedFields={touched}
                            invalidated={eventLedger ? record.quarantined : (invalidatedAt.get(record.memory_id) ?? Number.POSITIVE_INFINITY) <= cursor}
                            onSelect={() => setPinnedId((value) => value === record.memory_id ? null : record.memory_id)}
                            cardRef={(node) => {
                              if (node) cardRefs.current.set(record.memory_id, node)
                              else cardRefs.current.delete(record.memory_id)
                            }}
                            zh={zh}
                          />
                        )
                      })}
                      {!records.length ? <div className="lm-tier-empty">{zh ? '这类记录当前为 0 条' : '0 RECORDS OF THIS TYPE'}</div> : null}
                    </div>
                  </section>
                )
              })}
            </div>

            <LiveMemoryInspector
              summary={selectedSummary}
              detail={selectedDetail}
              step={currentStep}
              loading={Boolean(selectedId && !selectedDetail)}
              zh={zh}
            />
          </div>

          <footer className="lm-legend">
            <span><i className="sample-card" />{zh ? '实线框 = 游标处已出现' : 'SOLID FRAME = PRESENT AT CURSOR'}</span>
            <span><i className="sample-pending" />{zh ? '虚线框 = 此刻尚未写入' : 'DASHED FRAME = NOT YET WRITTEN'}</span>
            <span><i className="sample-acid" />{zh ? '荧光格 = 本步真实时间字段变化' : 'ACID CELL = REAL TIME FIELD CHANGED THIS STEP'}</span>
            <span><i className="sample-quarantine" />{zh ? '双线 = 当前已打入冷宫，原因保留原文' : 'DOUBLE RULE = SHELVED NOW, RAW REASON SHOWN'}</span>
            <span>×N {zh ? '= 被找到或再次采用的次数' : '= TIMES FOUND OR REUSED'}</span>
            <span>─ {zh ? '关联' : 'LINK'} · → {zh ? '有向关系或取代' : 'DIRECTED RELATION OR SUPERSESSION'}</span>
          </footer>
        </>
      ) : null}
      </div>
    </section>
  )
}
