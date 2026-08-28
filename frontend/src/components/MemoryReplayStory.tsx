import { useMemo, useState } from 'react'
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
  episodic: ['案例', 'EPISODIC'],
  semantic: ['规律', 'SEMANTIC'],
  procedural: ['方法', 'PROCEDURAL'],
  asset_profile: ['设备', 'ASSET'],
}

const STEP_LABEL = {
  case: ['本轮问题', 'CURRENT CASE'],
  retrieve: ['检索', 'RETRIEVE'],
  context: ['编译', 'COMPILE'],
  verify: ['核查', 'VERIFY'],
  writes: ['写入', 'WRITE'],
} as const

type TraceStep = keyof typeof STEP_LABEL
export type ReplayDeepView = 'context' | 'memory' | 'ledger'

const text = (value: readonly [string, string], zh: boolean) => value[zh ? 0 : 1]
const shortId = (value: string) => value.replace(/^real_/, '').replace(/^(epi|sem|proc)-/, '')
const assetLabel = (value: string, zh: boolean) => value === 'DAHUA_FORTIGATE'
  ? (zh ? 'FortiGate 设备' : 'FortiGate appliance')
  : value
const recalledIds = (retrieved: Observatory['recall'][number]['retrieved']) =>
  [...new Set(Object.values(retrieved).flatMap((ids) => ids ?? []))]

export function MemoryReplayStory({
  obs,
  cases,
  cursorSeq,
  currentEvent,
  playing,
  onCursor,
  onTogglePlay,
  onSelectMemory,
  onDeepDive,
  zh,
}: {
  obs: Observatory
  cases: MemoryReplayCase[]
  cursorSeq: number
  currentEvent: MemEvent | null
  playing: boolean
  onCursor: (seq: number) => void
  onTogglePlay: () => void
  onSelectMemory: (memoryId: string) => void
  onDeepDive: (view: ReplayDeepView) => void
  zh: boolean
}) {
  const [openStep, setOpenStep] = useState<TraceStep | null>(null)
  const stories = useMemo(() => buildMemoryRunStories(obs, cases), [obs, cases])
  const recordById = useMemo(
    () => new Map<string, MemRecord>(obs.records.map((record) => [record.memory_id, record])),
    [obs.records],
  )
  const active = stories.find((story) => story.runId === currentEvent?.run_id) ?? stories[0] ?? null
  if (!active) return null

  const recall = active.recall
  const retrievedIds = recalledIds(recall.retrieved)
  const included = new Set(recall.included_memory_ids)
  const topCandidates = [...recall.retrieval_candidates]
    .sort((a, b) => b.final_score - a.final_score)
    .slice(0, 5)
  const visibleWrites = active.events.filter((event) => event.seq <= cursorSeq)
  const currentWrite = active.events.find((event) => event.seq === cursorSeq) ?? null
  const includedTiers = recall.included_memory_ids.reduce<Record<string, number>>((counts, id) => {
    const tier = recordById.get(id)?.tier ?? 'episodic'
    counts[tier] = (counts[tier] ?? 0) + 1
    return counts
  }, {})
  const allOps = countMemoryOps(obs.events)
  const activeOps = Object.keys(allOps) as MemOp[]
  const passCount = Math.max(1, ...stories.map((story) => story.pass + 1))
  const maxRetrieved = Math.max(1, ...stories.map((story) => recalledIds(story.recall.retrieved).length))
  const status = obs.capability_status ?? {}
  const quietMechanisms = [
    { key: 'conflict_update', label: zh ? '冲突更新' : 'CONFLICT UPDATE' },
    { key: 'contradiction_quarantine', label: zh ? '矛盾隔离' : 'CONTRADICTION QUARANTINE' },
    { key: 'eviction', label: zh ? '容量淘汰' : 'CAPACITY EVICTION' },
  ].filter(({ key }) => status[key] && !status[key].fired)

  const toggleStep = (step: TraceStep) => {
    setOpenStep((current) => current === step ? null : step)
  }

  const inspectMemory = (memoryId: string) => {
    onSelectMemory(memoryId)
    onDeepDive('memory')
  }

  return (
    <section className="mrs" aria-label={zh ? '当前运行的检索、核查与记忆变化' : 'Retrieval, verification, and memory changes in the current run'}>
      <header className="mrs-head">
        <div>
          <span className="mrs-kicker">{zh ? '固定留出集 · 四轮离线对照记录' : 'FIXED HELD-OUT SET · FOUR-PASS OFFLINE RECORD'}</span>
          <h2>{zh ? '当前运行的检索、核查与记忆变化' : 'RETRIEVAL, VERIFICATION, AND MEMORY CHANGES'}</h2>
        </div>
        <div className="mrs-transport">
          <button type="button" onClick={onTogglePlay} aria-pressed={playing}>
            <i aria-hidden="true">{playing ? 'Ⅱ' : '▶'}</i>
            {playing ? (zh ? '暂停' : 'PAUSE') : (zh ? '播放' : 'PLAY')}
          </button>
          <output>{String(cursorSeq).padStart(3, '0')} / {String(Math.max(0, obs.events.length - 1)).padStart(3, '0')}</output>
        </div>
      </header>

      <div className="mrs-overview">
        <div className="mrs-selected-run">
          <span>P{active.pass + 1} / {String(active.caseIndex + 1).padStart(2, '0')}</span>
          <b>{shortId(active.caseInfo?.rootCause ?? active.caseId)}</b>
          <em>{active.recall.resolved ? (zh ? '根因一致' : 'ROOT MATCH') : (zh ? '独立核查' : 'FRESH CHECK')}</em>
        </div>
        <div className="mrs-run-map" role="group" aria-label={zh ? '四轮六案例运行带' : 'Four-pass, six-case run bands'}>
          <div className="mrs-case-axis" aria-hidden="true">
            <span />
            {cases.map((item, index) => <b key={item.id}>{String(index + 1).padStart(2, '0')}</b>)}
          </div>
          {Array.from({ length: passCount }, (_, pass) => (
            <div className="mrs-run-row" key={pass}>
              <span className="mrs-pass">P{pass + 1}</span>
              {cases.map((item) => {
                const story = stories.find((candidate) => candidate.pass === pass && candidate.caseId === item.id)
                if (!story) return <span className="mrs-run-missing" key={item.id} />
                const isActive = story.runId === active.runId
                const isDone = cursorSeq > story.lastEventSeq
                const volume = recalledIds(story.recall.retrieved).length
                return (
                  <button
                    type="button"
                    key={item.id}
                    className={`mrs-run${isActive ? ' active' : ''}${isDone ? ' done' : ''}${story.recall.resolved ? ' match' : ''}`}
                    onClick={() => onCursor(story.firstEventSeq)}
                    aria-pressed={isActive}
                    aria-label={`P${pass + 1}, ${item.query}, ${volume} ${zh ? '条检索结果' : 'retrieved records'}`}
                    title={`${item.query} · ${item.rootCause}`}
                  >
                    <i className={volume === 0 ? 'empty' : ''} style={{ height: `${volume === 0 ? 0 : Math.max(10, (volume / maxRetrieved) * 100)}%` }} />
                    <span aria-hidden="true" />
                  </button>
                )
              })}
            </div>
          ))}
        </div>
        <div className="mrs-overview-key">
          <span><i className="seen" />{zh ? '检索量' : 'RETRIEVAL VOLUME'}</span>
          <span><i className="match" />{zh ? '根因一致' : 'ROOT MATCH'}</span>
          <span>{zh ? '点击任一运行切换' : 'SELECT ANY RUN'}</span>
        </div>
      </div>

      <div className="mrs-current">
        <div className="mrs-current-head">
          <span>{zh ? '本轮信号路径' : 'CURRENT SIGNAL PATH'}</span>
          <b>{zh ? '点击节点查看证据' : 'SELECT A NODE TO INSPECT'}</b>
          <code>{active.runId}</code>
        </div>

        <div className="mrs-trace">
          <button type="button" className={`mrs-node case${openStep === 'case' ? ' open' : ''}`} onClick={() => toggleStep('case')} aria-expanded={openStep === 'case'}>
            <span className="mrs-node-index">01</span>
            <span className="mrs-node-label">{text(STEP_LABEL.case, zh)}</span>
            <b className="mrs-case-mark">?</b>
            <em>{String(active.caseIndex + 1).padStart(2, '0')}</em>
          </button>

          <button type="button" className={`mrs-node retrieve${openStep === 'retrieve' ? ' open' : ''}`} onClick={() => toggleStep('retrieve')} aria-expanded={openStep === 'retrieve'}>
            <span className="mrs-node-index">02</span>
            <span className="mrs-node-label">{text(STEP_LABEL.retrieve, zh)}</span>
            <span className="mrs-dot-field" aria-hidden="true">
              {retrievedIds.map((id) => <i key={id} className={included.has(id) ? 'included' : 'dropped'} />)}
            </span>
            <strong>{retrievedIds.length}</strong>
          </button>

          <button type="button" className={`mrs-node context${openStep === 'context' ? ' open' : ''}`} onClick={() => toggleStep('context')} aria-expanded={openStep === 'context'}>
            <span className="mrs-node-index">03</span>
            <span className="mrs-node-label">{text(STEP_LABEL.context, zh)}</span>
            <span className="mrs-funnel" aria-hidden="true"><i /><i /><b>{included.size}</b></span>
            <em>{recall.dropped_memory_ids.length} {zh ? '丢弃' : 'DROP'}</em>
          </button>

          <button type="button" className={`mrs-node verify${openStep === 'verify' ? ' open' : ''}`} onClick={() => toggleStep('verify')} aria-expanded={openStep === 'verify'}>
            <span className="mrs-node-index">04</span>
            <span className="mrs-node-label">{text(STEP_LABEL.verify, zh)}</span>
            <span className="mrs-probe-line" aria-hidden="true">
              {Array.from({ length: Math.min(12, recall.probes) }, (_, index) => <i key={index} />)}
              <b className={recall.resolved ? 'match' : ''} />
            </span>
            <strong>{recall.probes}</strong>
          </button>

          <button type="button" className={`mrs-node writes${openStep === 'writes' ? ' open' : ''}`} onClick={() => toggleStep('writes')} aria-expanded={openStep === 'writes'}>
            <span className="mrs-node-index">05</span>
            <span className="mrs-node-label">{text(STEP_LABEL.writes, zh)}</span>
            <span className="mrs-write-code" aria-hidden="true">
              {active.events.map((event) => (
                <i
                  key={event.seq}
                  className={`${event.seq <= cursorSeq ? 'visible' : ''}${event.seq === currentWrite?.seq ? ' current' : ''} op-${event.op.toLowerCase()}`}
                />
              ))}
            </span>
            <strong>{visibleWrites.length}<small>/{active.events.length}</small></strong>
          </button>
        </div>

        {openStep ? (
          <section className={`mrs-detail step-${openStep}`} aria-live="polite">
            <header>
              <span>{String(Object.keys(STEP_LABEL).indexOf(openStep) + 1).padStart(2, '0')}</span>
              <h3>{text(STEP_LABEL[openStep], zh)}</h3>
              <button type="button" onClick={() => setOpenStep(null)} aria-label={zh ? '收起详情' : 'Close detail'}>×</button>
            </header>

            {openStep === 'case' ? (
              <div className="mrs-detail-case">
                <p>{active.caseInfo?.query ?? active.caseId}</p>
                <div>{(active.caseInfo?.assets ?? []).map((asset) => <span key={asset}>{assetLabel(asset, zh)}</span>)}</div>
                <dl><dt>{zh ? '留出集核对目标' : 'HELD-OUT TARGET'}</dt><dd>{active.caseInfo?.rootCause ?? shortId(active.caseId)}</dd></dl>
              </div>
            ) : null}

            {openStep === 'retrieve' ? (
              <div className="mrs-detail-retrieval">
                {topCandidates.length ? topCandidates.map((candidate, index) => (
                  <button key={candidate.memory_id} onClick={() => inspectMemory(candidate.memory_id)}>
                    <i>{String(index + 1).padStart(2, '0')}</i>
                    <span>{recordById.get(candidate.memory_id)?.text ?? candidate.memory_id}</span>
                    <b>{candidate.final_score.toFixed(2)}</b>
                  </button>
                )) : <p className="mrs-empty">{zh ? '空库，当前没有候选记录。' : 'EMPTY STORE, NO CANDIDATES YET.'}</p>}
              </div>
            ) : null}

            {openStep === 'context' ? (
              <div className="mrs-detail-context">
                <div className="mrs-tier-bars">
                  {Object.entries(includedTiers).map(([tier, count]) => (
                    <span key={tier}><b style={{ width: `${(count / Math.max(1, included.size)) * 100}%` }} /><i>{text(TIER_LABEL[tier] ?? [tier, tier], zh)}</i><em>{count}</em></span>
                  ))}
                </div>
                <dl><dt>{zh ? '检索结果' : 'RETRIEVED'}</dt><dd>{retrievedIds.length}</dd><dt>{zh ? '编译器纳入' : 'INCLUDED'}</dt><dd>{included.size}</dd><dt>{zh ? '丢弃原因记录' : 'DROP REASONS'}</dt><dd>{(recall.context_drops ?? []).length}</dd></dl>
                <button className="mrs-deep-link" type="button" onClick={() => onDeepDive('context')}>{zh ? '展开完整排名与得分' : 'OPEN FULL RANKING AND SCORES'} →</button>
              </div>
            ) : null}

            {openStep === 'verify' ? (
              <div className="mrs-detail-verify">
                <b>{recall.probes}</b>
                <p>{recall.resolved
                  ? (zh ? '记忆根因与本轮新证据结论一致' : 'THE STORED ROOT MATCHES THE FRESH-EVIDENCE RESULT')
                  : (zh ? '本轮没有产生记忆根因一致事件' : 'NO MEMORY ROOT-MATCH EVENT IN THIS RUN')}</p>
                <dl><dt>{zh ? '方法记忆减少探针' : 'PROCEDURAL SHORTCUT'}</dt><dd>{recall.shortcut ? (zh ? '已触发' : 'FIRED') : (zh ? '未触发' : 'DID NOT FIRE')}</dd></dl>
                <button className="mrs-deep-link" type="button" onClick={() => onDeepDive('context')}>{zh ? '查看本轮采用的证据' : 'OPEN EVIDENCE USED IN THIS RUN'} →</button>
              </div>
            ) : null}

            {openStep === 'writes' ? (
              <div className="mrs-detail-writes">
                <div className="mrs-op-summary">
                  {activeOps.map((op) => <span key={op}><i>{allOps[op]}</i>{text(OP_LABEL[op], zh)}</span>)}
                </div>
                <div className="mrs-write-list">
                  {visibleWrites.slice(-6).map((event) => (
                    <button key={event.seq} className={event.seq === currentWrite?.seq ? 'current' : ''} onClick={() => inspectMemory(event.memory_id)}>
                      <span>{text(OP_LABEL[event.op], zh)}</span><b>{shortId(event.memory_id)}</b><i>#{event.seq}</i>
                    </button>
                  ))}
                  {visibleWrites.length === 0 ? <p className="mrs-empty">{zh ? '等待本轮首个写入事件。' : 'WAITING FOR THE FIRST WRITE.'}</p> : null}
                </div>
                {quietMechanisms.length ? <p className="mrs-quiet">{quietMechanisms.map((item) => item.label).join(' · ')} {zh ? '本次未触发' : 'DID NOT FIRE'}</p> : null}
                <div className="mrs-deep-actions">
                  <button className="mrs-deep-link" type="button" onClick={() => onDeepDive('memory')}>{zh ? '展开记忆空间' : 'OPEN MEMORY SPACE'} →</button>
                  <button className="mrs-deep-link" type="button" onClick={() => onDeepDive('ledger')}>{zh ? '展开生命周期账本' : 'OPEN LIFECYCLE LEDGER'} →</button>
                </div>
              </div>
            ) : null}
          </section>
        ) : null}
      </div>
    </section>
  )
}
