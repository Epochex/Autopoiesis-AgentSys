import { useMemo } from 'react'
import type {
  MemEvent,
  MemOp,
  MemRecord,
  MemoryReplayCase,
  Observatory,
} from '../types'
import { buildMemoryRunStories, countMemoryOps } from './memory-story'
import './memory-replay-story.css'

const OP_LABEL: Record<MemOp, [string, string]> = {
  ADD: ['新增', 'ADD'],
  UPDATE: ['更新', 'UPDATE'],
  NOOP: ['保持原值', 'NO CHANGE'],
  REINFORCE: ['强化', 'REINFORCE'],
  QUARANTINE: ['隔离', 'QUARANTINE'],
  INSIGHT: ['形成总结', 'FORM SUMMARY'],
  INSIGHT_REFRESH: ['刷新总结', 'REFRESH SUMMARY'],
  LINK: ['建立连接', 'LINK'],
  DECAY: ['降低强度', 'DECAY'],
  FORGET: ['移出可用库', 'FORGET'],
}

const TIER_LABEL: Record<string, [string, string]> = {
  episodic: ['案例记忆', 'EPISODIC'],
  semantic: ['规律记忆', 'SEMANTIC'],
  procedural: ['方法记忆', 'PROCEDURAL'],
  asset_profile: ['设备记忆', 'ASSET'],
}

const text = (value: [string, string], zh: boolean) => value[zh ? 0 : 1]
const shortId = (value: string) => value.replace(/^real_/, '').replace(/^(epi|sem|proc)-/, '')
const assetLabel = (value: string, zh: boolean) => value === 'DAHUA_FORTIGATE'
  ? (zh ? 'FortiGate 设备' : 'FortiGate appliance')
  : value

export function MemoryReplayStory({
  obs,
  cases,
  cursorSeq,
  currentEvent,
  onCursor,
  onSelectMemory,
  zh,
}: {
  obs: Observatory
  cases: MemoryReplayCase[]
  cursorSeq: number
  currentEvent: MemEvent | null
  onCursor: (seq: number) => void
  onSelectMemory: (memoryId: string) => void
  zh: boolean
}) {
  const stories = useMemo(() => buildMemoryRunStories(obs, cases), [obs, cases])
  const recordById = useMemo(
    () => new Map<string, MemRecord>(obs.records.map((record) => [record.memory_id, record])),
    [obs.records],
  )
  const active = stories.find((story) => story.runId === currentEvent?.run_id) ?? stories[0] ?? null
  if (!active) return null

  const recall = active.recall
  const retrievedCount = Object.values(recall.retrieved).reduce((sum, ids) => sum + (ids?.length ?? 0), 0)
  const included = new Set(recall.included_memory_ids)
  const topCandidates = [...recall.retrieval_candidates]
    .sort((a, b) => b.final_score - a.final_score)
    .slice(0, 3)
  const visibleWrites = active.events.filter((event) => event.seq <= cursorSeq)
  const currentWrite = active.events.find((event) => event.seq === cursorSeq) ?? null
  const includedTiers = recall.included_memory_ids.reduce<Record<string, number>>((counts, id) => {
    const tier = recordById.get(id)?.tier ?? 'episodic'
    counts[tier] = (counts[tier] ?? 0) + 1
    return counts
  }, {})
  const allOps = countMemoryOps(obs.events)
  const activeOps = Object.keys(allOps) as MemOp[]
  const status = obs.capability_status ?? {}
  const quietMechanisms = [
    { key: 'conflict_update', label: zh ? '冲突更新' : 'CONFLICT UPDATE' },
    { key: 'contradiction_quarantine', label: zh ? '矛盾隔离' : 'CONTRADICTION QUARANTINE' },
    { key: 'eviction', label: zh ? '容量淘汰' : 'CAPACITY EVICTION' },
  ].filter(({ key }) => status[key] && !status[key].fired)

  return (
    <section className="mrs" aria-label={zh ? '一次记忆回放的完整过程' : 'Complete memory replay story'}>
      <header className="mrs-head">
        <div>
          <span className="mrs-kicker">{zh ? '固定留出集 · 四轮离线对照记录' : 'FIXED HELD-OUT SET · FOUR-PASS OFFLINE RECORD'}</span>
          <h2>{zh ? '当前运行的检索、核查与记忆变化' : 'RETRIEVAL, VERIFICATION, AND MEMORY CHANGES IN THE CURRENT RUN'}</h2>
        </div>
        <div className="mrs-ledger-proof">
          <span>{zh ? `生命周期账本 ${obs.events.length} 条` : `${obs.events.length} LIFECYCLE EVENTS`}</span>
          <b>{activeOps.map((op) => `${text(OP_LABEL[op], zh)} ${allOps[op]}`).join(' · ')}</b>
          {quietMechanisms.length > 0 ? (
            <em>{quietMechanisms.map((item) => item.label).join(' · ')} {zh ? '本次未触发' : 'DID NOT FIRE'}</em>
          ) : null}
        </div>
      </header>

      <div className="mrs-run-map" role="group" aria-label={zh ? '四轮六案例导航' : 'Four-pass, six-case navigation'}>
        {Array.from({ length: Math.max(1, ...stories.map((story) => story.pass + 1)) }, (_, pass) => (
          <div className="mrs-run-row" key={pass}>
            <span className="mrs-pass">P{pass + 1}</span>
            {cases.map((item, caseIndex) => {
              const story = stories.find((candidate) => candidate.pass === pass && candidate.caseId === item.id)
              if (!story) return <span className="mrs-run-missing" key={item.id} />
              const isActive = story.runId === active.runId
              const isDone = cursorSeq > story.lastEventSeq
              return (
                <button
                  type="button"
                  key={item.id}
                  className={`mrs-run${isActive ? ' active' : ''}${isDone ? ' done' : ''}`}
                  onClick={() => onCursor(story.firstEventSeq)}
                  aria-pressed={isActive}
                  title={`${item.query} · ${item.rootCause}`}
                >
                  <span>{String(caseIndex + 1).padStart(2, '0')}</span>
                  <b>{shortId(item.rootCause)}</b>
                  <em>{story.recall.resolved ? (zh ? '根因一致' : 'ROOT MATCH') : (zh ? '独立核查' : 'FRESH CHECK')}</em>
                </button>
              )
            })}
          </div>
        ))}
      </div>

      <div className="mrs-current">
        <div className="mrs-current-head">
          <span>{zh ? '当前演示' : 'NOW SHOWING'}</span>
          <b>P{active.pass + 1} · {zh ? '案例' : 'CASE'} {active.caseIndex + 1}/{cases.length}</b>
          <code>{zh ? '账本序号' : 'LEDGER SEQ'} {cursorSeq}/{Math.max(0, obs.events.length - 1)}</code>
        </div>

        <div className="mrs-flow">
          <article className="mrs-stage">
            <span className="mrs-stage-no">01</span>
            <h3>{zh ? '收到本轮问题' : 'RECEIVE CASE'}</h3>
            <p className="mrs-query">{active.caseInfo?.query ?? active.caseId}</p>
            <div className="mrs-assets">
              {(active.caseInfo?.assets ?? []).map((asset) => <span key={asset}>{assetLabel(asset, zh)}</span>)}
            </div>
            <dl><dt>{zh ? '留出集核对目标' : 'HELD-OUT TARGET'}</dt><dd>{active.caseInfo?.rootCause ?? shortId(active.caseId)}</dd></dl>
          </article>

          <article className="mrs-stage">
            <span className="mrs-stage-no">02</span>
            <h3>{zh ? '检索旧记忆' : 'RETRIEVE MEMORY'}</h3>
            <div className="mrs-big"><b>{retrievedCount}</b><span>{zh ? '条被取回' : 'RETRIEVED'}</span></div>
            {topCandidates.length > 0 ? topCandidates.map((candidate, index) => (
              <button key={candidate.memory_id} className="mrs-candidate" onClick={() => onSelectMemory(candidate.memory_id)}>
                <i>{index + 1}</i>
                <span>{recordById.get(candidate.memory_id)?.text ?? candidate.memory_id}</span>
                <b>{candidate.final_score.toFixed(2)}</b>
              </button>
            )) : <p className="mrs-empty">{zh ? '空库，当前没有候选记录。' : 'EMPTY STORE, NO CANDIDATES YET.'}</p>}
          </article>

          <article className="mrs-stage">
            <span className="mrs-stage-no">03</span>
            <h3>{zh ? '组装调查上下文' : 'ASSEMBLE CONTEXT'}</h3>
            <div className="mrs-big"><b>{included.size}</b><span>{zh ? '条被编译器纳入' : 'INCLUDED BY COMPILER'}</span></div>
            <div className="mrs-tier-counts">
              {Object.entries(includedTiers).map(([tier, count]) => (
                <span key={tier}><b>{count}</b>{text(TIER_LABEL[tier] ?? [tier, tier], zh)}</span>
              ))}
            </div>
            <dl><dt>{zh ? '找到但未采用' : 'FOUND, NOT USED'}</dt><dd>{recall.dropped_memory_ids.length}</dd></dl>
            <dl><dt>{zh ? '保存了具体丢弃原因' : 'DROP REASONS RECORDED'}</dt><dd>{(recall.context_drops ?? []).length}</dd></dl>
          </article>

          <article className="mrs-stage">
            <span className="mrs-stage-no">04</span>
            <h3>{zh ? '用新证据核查' : 'VERIFY WITH FRESH EVIDENCE'}</h3>
            <div className="mrs-big"><b>{recall.probes}</b><span>{zh ? '个探针' : 'PROBES'}</span></div>
            <p className="mrs-result">
              {recall.resolved
                ? (zh ? '记忆中的根因与本轮新证据结论一致' : 'THE STORED ROOT MATCHES THE FRESH-EVIDENCE RESULT')
                : (zh ? '本轮没有产生记忆根因一致事件' : 'NO MEMORY ROOT-MATCH EVENT IN THIS RUN')}
            </p>
            <dl><dt>{zh ? '方法记忆减少探针' : 'PROCEDURAL SHORTCUT'}</dt><dd>{recall.shortcut ? (zh ? '已触发' : 'FIRED') : (zh ? '未触发' : 'DID NOT FIRE')}</dd></dl>
          </article>

          <article className="mrs-stage write">
            <span className="mrs-stage-no">05</span>
            <h3>{zh ? '把结果写回记忆' : 'WRITE BACK'}</h3>
            <div className="mrs-big"><b>{visibleWrites.length}/{active.events.length}</b><span>{zh ? '项变化已发生' : 'CHANGES APPLIED'}</span></div>
            <div className="mrs-write-list">
              {visibleWrites.slice(-4).map((event) => (
                <button key={event.seq} className={event.seq === currentWrite?.seq ? 'current' : ''} onClick={() => onSelectMemory(event.memory_id)}>
                  <span>{text(OP_LABEL[event.op], zh)}</span>
                  <b>{shortId(event.memory_id)}</b>
                  <i>#{event.seq}</i>
                </button>
              ))}
              {visibleWrites.length === 0 ? <p className="mrs-empty">{zh ? '等待本轮首个写入事件。' : 'WAITING FOR THE FIRST WRITE.'}</p> : null}
            </div>
          </article>
        </div>

      </div>
    </section>
  )
}
