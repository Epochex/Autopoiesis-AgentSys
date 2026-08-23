import { useEffect, useMemo, useState } from 'react'
import type { Lang } from '../i18n'
import type {
  LiveMemoryDetailRecord,
  LiveMemoryDetailResponse,
  LiveMemoryListRecord,
  LiveMemoryListResponse,
  MemTier,
} from '../types'
import './live-memory.css'

const TIER_ORDER: MemTier[] = ['semantic', 'procedural', 'episodic', 'asset_profile']

const TIER_LABEL: Record<MemTier, [string, string]> = {
  semantic: ['语义 SEMANTIC · 抽象认知', 'SEMANTIC · ABSTRACTION'],
  procedural: ['程序 PROCEDURAL · 可复用模式', 'PROCEDURAL · REUSABLE PROBE'],
  episodic: ['情景 EPISODIC · 具体经历', 'EPISODIC · CONCRETE EPISODE'],
  asset_profile: ['资产 ASSET PROFILE · 实体画像', 'ASSET PROFILE · ENTITY'],
}

const same = (left: string[], right: string[]) =>
  left.length === right.length && left.every((item, index) => item === right[index])

const quarantineTag = (record: Pick<LiveMemoryListRecord, 'tags'>): string | null => {
  const reasons = record.tags.filter((tag) => tag.startsWith('quarantine:'))
  return reasons.length ? reasons[reasons.length - 1] : null
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

function MemoryCard({
  record,
  selected,
  onSelect,
  zh,
}: {
  record: LiveMemoryListRecord
  selected: boolean
  onSelect: () => void
  zh: boolean
}) {
  return (
    <button
      type="button"
      className={selected ? 'lm-card selected' : 'lm-card'}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="lm-card-id" title={record.memory_id}>{record.memory_id}</span>
      <span className="lm-card-text">{record.text}</span>
      <span className="lm-card-metrics">
        <Metric label={zh ? '置信' : 'CONF'} value={record.confidence} ceiling={3} />
        <Metric label={zh ? '重要' : 'IMP'} value={record.importance} ceiling={52} />
        <Metric label={zh ? '强度' : 'STR'} value={record.strength} ceiling={1} />
      </span>
    </button>
  )
}

function ChipList({ items }: { items: string[] }) {
  if (!items.length) return <span className="lm-none">∅</span>
  return <span className="lm-chips">{items.map((item) => <i key={item}>{item}</i>)}</span>
}

function AuditRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="lm-audit-row">
      <span className="lm-audit-key">{label}</span>
      <span className="lm-audit-value">{children}</span>
    </div>
  )
}

const listChangedFields = (summary: LiveMemoryListRecord, detail: LiveMemoryDetailRecord) => {
  const changed: string[] = []
  if (summary.confidence !== detail.confidence) changed.push('confidence')
  if (summary.importance !== detail.importance) changed.push('importance')
  if (summary.strength !== detail.strength) changed.push('strength')
  if (summary.access_count !== detail.access_count) changed.push('access_count')
  if (!same(summary.tags, detail.tags)) changed.push('tags')
  if (!same(summary.asset_ids, detail.asset_ids)) changed.push('asset_ids')
  if (summary.source_trace_ids !== detail.source_trace_ids.length) changed.push('source_trace_ids')
  if (summary.valid_from !== detail.valid_from) changed.push('valid_from')
  if (summary.valid_to !== detail.valid_to) changed.push('valid_to')
  if (summary.quarantined !== detail.quarantined) changed.push('quarantined')
  return changed
}

function LiveMemoryInspector({
  summary,
  detail,
  loading,
  error,
  zh,
}: {
  summary: LiveMemoryListRecord | null
  detail: LiveMemoryDetailRecord | null
  loading: boolean
  error: string | null
  zh: boolean
}) {
  if (!summary) {
    return (
      <aside className="lm-inspector empty">
        <div className="lm-inspector-mast">{zh ? '线上记忆 · 逐字段审计' : 'LIVE MEMORY · FIELD AUDIT'}</div>
        <div className="lm-inspector-empty">
          <b>{zh ? '未选中条目' : 'NO RECORD SELECTED'}</b>
          <span>{zh ? '点击左侧任一记录查看完整溯源。' : 'Select any record on the left to inspect its full provenance.'}</span>
        </div>
      </aside>
    )
  }

  const changed = detail ? listChangedFields(summary, detail) : []
  const changedSet = new Set(changed)
  const reason = detail ? quarantineTag(detail) : quarantineTag(summary)

  return (
    <aside className="lm-inspector" aria-live="polite">
      <header className="lm-inspector-mast">
        <span>{zh ? '线上记忆 · 逐字段审计' : 'LIVE MEMORY · FIELD AUDIT'}</span>
        <span>{zh ? '只读' : 'READ ONLY'}</span>
      </header>
      <div className="lm-inspector-mode">
        <span>{zh ? '已选中' : 'SELECTED'}</span>
        <b title={summary.memory_id}>{summary.memory_id}</b>
      </div>

      {loading && <div className="lm-inspector-status">{zh ? '正在读取详情…' : 'LOADING DETAIL…'}</div>}
      {error && <div className="lm-inspector-status error">{error}</div>}

      {detail && (
        <div className="lm-inspector-scroll">
          <div className="lm-detail-id">
            <span>{TIER_LABEL[detail.tier][zh ? 0 : 1]}</span>
            <b>{detail.memory_id}</b>
            {detail.quarantined && <i>{zh ? '已隔离' : 'QUARANTINED'}</i>}
          </div>

          <section className="lm-audit-section">
            <h4>{zh ? '全文' : 'FULL TEXT'}</h4>
            <p className="lm-full-text">{detail.text}</p>
          </section>

          <section className="lm-audit-section">
            <h4>{zh ? '当前状态' : 'CURRENT STATE'}</h4>
            <div className="lm-state-grid">
              <div className={changedSet.has('confidence') ? 'changed' : ''}><b>{detail.confidence.toFixed(2)}</b><span>{zh ? '置信' : 'CONFIDENCE'}</span></div>
              <div className={changedSet.has('importance') ? 'changed' : ''}><b>{detail.importance.toFixed(2)}</b><span>{zh ? '重要度' : 'IMPORTANCE'}</span></div>
              <div className={changedSet.has('strength') ? 'changed' : ''}><b>{detail.strength.toFixed(2)}</b><span>{zh ? '强度' : 'STRENGTH'}</span></div>
            </div>
            <AuditRow label={zh ? '访问次数' : 'ACCESS COUNT'}>{detail.access_count}</AuditRow>
            <div className="lm-time-axis">
              <span className={changedSet.has('valid_from') ? 'changed' : ''}>
                <i>VALID_FROM</i><b>{detail.valid_from ?? '∅'}</b>
              </span>
              <span className={changedSet.has('valid_to') ? 'changed' : ''}>
                <i>VALID_TO</i><b>{detail.valid_to ?? '∅'}</b>
              </span>
            </div>
            <AuditRow label={zh ? '首次观测' : 'FIRST OBSERVED'}>{detail.first_observed_at ?? '∅'}</AuditRow>
            <AuditRow label={zh ? '最近观测' : 'LAST OBSERVED'}>{detail.last_observed_at ?? '∅'}</AuditRow>
          </section>

          <section className="lm-audit-section">
            <h4>{zh ? '本步变更' : 'CHANGE AT THIS STEP'}</h4>
            {changed.length ? (
              <div className="lm-change-list">
                {changed.map((field) => <span key={field}>{field}</span>)}
              </div>
            ) : (
              <p className="lm-audit-note">{zh ? '列表快照与详情快照一致；本次只读查看没有写入动作。' : 'List and detail snapshots match; this read-only inspection made no write.'}</p>
            )}
          </section>

          <section className="lm-audit-section">
            <h4>{zh ? '来源与关系' : 'PROVENANCE AND RELATIONS'}</h4>
            <AuditRow label="TAGS"><ChipList items={detail.tags} /></AuditRow>
            <AuditRow label="ASSET_IDS"><ChipList items={detail.asset_ids} /></AuditRow>
            <AuditRow label="SOURCE_TRACE_IDS">
              <span className="lm-trace-count">{detail.source_trace_ids.length} {zh ? '条' : 'TRACES'}</span>
              <ChipList items={detail.source_trace_ids} />
            </AuditRow>
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
            <AuditRow label="EVIDENCE_SNAPSHOT">
              {detail.evidence_snapshot.count} · <ChipList items={detail.evidence_snapshot.evidence_ids} />
            </AuditRow>
            <AuditRow label="SUPERSEDED_BY">{detail.superseded_by ?? '∅'}</AuditRow>
            {detail.quarantined && (
              <AuditRow label={zh ? '隔离原因' : 'QUARANTINE REASON'}>
                <span className="lm-quarantine-reason">{reason ?? '∅'}</span>
              </AuditRow>
            )}
          </section>
        </div>
      )}
    </aside>
  )
}

export function LiveMemory({ lang }: { lang: Lang }) {
  const zh = lang === 'zh'
  const [data, setData] = useState<LiveMemoryListResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<LiveMemoryDetailRecord | null>(null)
  const [detailError, setDetailError] = useState<{ memoryId: string; message: string } | null>(null)

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
        setSelectedId((current) => current ?? payload.records[0]?.memory_id ?? null)
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setError(zh ? `线上记忆读取失败：${String(reason)}` : `Failed to read live memory: ${String(reason)}`)
      })
    return () => controller.abort()
  }, [zh])

  useEffect(() => {
    if (!selectedId || !data?.durable) return
    const controller = new AbortController()
    fetch(`/api/rca/memory/${encodeURIComponent(selectedId)}`, {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json() as Promise<LiveMemoryDetailResponse>
      })
      .then((payload) => setDetail(payload.record))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setDetailError({
          memoryId: selectedId,
          message: zh ? `详情读取失败：${String(reason)}` : `Failed to read detail: ${String(reason)}`,
        })
      })
    return () => controller.abort()
  }, [data?.durable, selectedId, zh])

  const activeByTier = useMemo(() => {
    const grouped = new Map<MemTier, LiveMemoryListRecord[]>(TIER_ORDER.map((tier) => [tier, []]))
    for (const record of data?.records ?? []) {
      if (!record.quarantined) grouped.get(record.tier)?.push(record)
    }
    return grouped
  }, [data])

  const quarantinedByReason = useMemo(() => {
    const grouped = new Map<string, LiveMemoryListRecord[]>()
    for (const record of data?.records ?? []) {
      if (!record.quarantined) continue
      const reason = quarantineTag(record) ?? (zh ? '未记录 quarantine: 原因标签' : 'NO quarantine: REASON TAG RECORDED')
      const rows = grouped.get(reason) ?? []
      rows.push(record)
      grouped.set(reason, rows)
    }
    return grouped
  }, [data, zh])

  const selectedSummary = data?.records.find((record) => record.memory_id === selectedId) ?? null
  const selectedDetail = detail?.memory_id === selectedId ? detail : null
  const selectedDetailError = detailError?.memoryId === selectedId ? detailError.message : null
  const detailLoading = Boolean(selectedId && data?.durable && !selectedDetail && !selectedDetailError)
  const selectMemory = (memoryId: string) => {
    setDetailError(null)
    setSelectedId(memoryId)
  }
  const activeCount = data
    ? TIER_ORDER.reduce((sum, tier) => sum + data.counts[tier], 0)
    : null

  return (
    <section id="live-memory" className="lm-root" aria-label={zh ? '线上记忆' : 'Live memory'}>
      <header className="lm-head">
        <div>
          <h2>{zh ? '线上记忆 · 这台机器现在真的记得什么' : 'LIVE MEMORY · WHAT THIS MACHINE ACTUALLY REMEMBERS NOW'}</h2>
          <p>{zh ? '与上面的离线基准无关；这是哨兵在生产中学到的' : 'Separate from the offline benchmark above; this is what the sentinel learned in production'}</p>
        </div>
        <div className="lm-stats" aria-live="polite">
          <span>{zh ? '活跃' : 'ACTIVE'} <b>{activeCount ?? '…'}</b></span>
          <i>·</i>
          <span>{zh ? '隔离' : 'QUARANTINED'} <b>{data?.counts.quarantined ?? '…'}</b></span>
          <i>·</i>
          <span>BM25 {zh ? '可检索文档' : 'SEARCHABLE DOCS'} <b>{activeCount ?? '…'}</b></span>
        </div>
      </header>

      {!data && !error && <div className="lm-message">{zh ? '正在读取线上记忆…' : 'READING LIVE MEMORY…'}</div>}
      {error && <div className="lm-message error">{error}</div>}
      {data && !data.durable && (
        <div className="lm-message nondurable">
          <b>{zh ? '当前没有持久化线上记忆' : 'NO DURABLE LIVE MEMORY IS AVAILABLE'}</b>
          <span>{zh ? '接口返回 durable=false；当前进程没有连接持久化记忆库。' : 'The API returned durable=false; this process is not connected to the durable memory repository.'}</span>
        </div>
      )}

      {data?.durable && (
        <>
          <div className="lm-body">
            <div className="lm-collections">
              <section className="lm-column active">
                <header className="lm-column-head">
                  <span>{zh ? '活跃 ACTIVE · BM25 当前可检索' : 'ACTIVE · CURRENTLY SEARCHABLE BY BM25'}</span>
                  <b>{activeCount}</b>
                </header>
                {TIER_ORDER.map((tier) => {
                  const records = activeByTier.get(tier) ?? []
                  return (
                    <section className={`lm-group tier-${tier}`} key={tier}>
                      <header><span>{TIER_LABEL[tier][zh ? 0 : 1]}</span><b>{records.length}</b></header>
                      <div className="lm-card-list">
                        {records.map((record) => (
                          <MemoryCard
                            key={record.memory_id}
                            record={record}
                            selected={selectedId === record.memory_id}
                            onSelect={() => selectMemory(record.memory_id)}
                            zh={zh}
                          />
                        ))}
                        {!records.length && <div className="lm-group-empty">{zh ? '该层当前 0 条' : '0 RECORDS IN THIS TIER'}</div>}
                      </div>
                    </section>
                  )
                })}
              </section>

              <section className="lm-column quarantined">
                <header className="lm-column-head">
                  <span>{zh ? '隔离 QUARANTINED · 完整审计留存' : 'QUARANTINED · RETAINED FOR FULL AUDIT'}</span>
                  <b>{data.counts.quarantined}</b>
                </header>
                {Array.from(quarantinedByReason.entries()).map(([reason, records]) => (
                  <section className="lm-group quarantine-group" key={reason}>
                    <header><span>{reason}</span><b>{records.length}</b></header>
                    <div className="lm-quarantine-list">
                      {records.map((record) => (
                        <button
                          type="button"
                          className={selectedId === record.memory_id ? 'lm-q-row selected' : 'lm-q-row'}
                          aria-pressed={selectedId === record.memory_id}
                          onClick={() => selectMemory(record.memory_id)}
                          key={record.memory_id}
                        >
                          <span>{record.memory_id}</span>
                          <i>{TIER_LABEL[record.tier][zh ? 0 : 1]}</i>
                        </button>
                      ))}
                    </div>
                  </section>
                ))}
              </section>
            </div>

            <LiveMemoryInspector
              summary={selectedSummary}
              detail={selectedDetail}
              loading={detailLoading}
              error={selectedDetailError}
              zh={zh}
            />
          </div>

          <footer className="lm-legend">
            <span><i className="sample-card" />{zh ? '方框 = 一条真实记忆' : 'FRAME = ONE REAL MEMORY'}</span>
            <span><i className="sample-acid" />{zh ? '荧光格 = 列表与详情快照间真实变化' : 'ACID CELL = REAL CHANGE BETWEEN LIST AND DETAIL SNAPSHOTS'}</span>
            <span><i className="sample-quarantine" />{zh ? '虚线框 = 已隔离，仍完整展示' : 'DASHED FRAME = QUARANTINED, STILL FULLY SHOWN'}</span>
            <span>▇▇▁▁ {zh ? '= 数值方块条，数字为接口原值' : '= DISCRETE SCALE; NUMBER IS THE API VALUE'}</span>
          </footer>
        </>
      )}
    </section>
  )
}
