import { useMemo, useState } from 'react'
import type { Lang } from '../i18n'

export type HypothesisStatus = 'proposed' | 'testing' | 'rejected' | 'confirmed'

export interface InvestigationHypothesis {
  hypothesis_id: string
  statement: string
  status: HypothesisStatus
  supporting_evidence_ids: string[]
  opposing_evidence_ids: string[]
}

export interface InvestigationProbe {
  probe_id: string
  description: string
  status: 'available' | 'selected' | 'completed' | 'failed'
  distinguishes_hypothesis_ids: string[]
}

export interface HypothesisView {
  state_version: number
  hypotheses: InvestigationHypothesis[]
  probes: InvestigationProbe[]
  confirmed_root_keys?: string[]
  active_root_keys?: string[]
}

export interface ProbeRound {
  probe_id: string
  command: string
  evidence_id?: string
  ok: boolean
  at?: string
}

const labels = (zh: boolean) => ({
  title: zh ? '候选根因竞争' : 'COMPETING ROOT CAUSES',
  version: zh ? '状态版本' : 'STATE VERSION',
  proposed: zh ? '待检查' : 'PROPOSED',
  testing: zh ? '检查中' : 'TESTING',
  rejected: zh ? '已排除' : 'REJECTED',
  confirmed: zh ? '已确认' : 'CONFIRMED',
  probe: zh ? '区分探针' : 'DISCRIMINATING PROBE',
  support: zh ? '支持证据' : 'SUPPORTING EVIDENCE',
  oppose: zh ? '反证' : 'OPPOSING EVIDENCE',
  notRun: zh ? '尚未执行' : 'NOT RUN',
  detail: zh ? '点选候选查看探针与证据' : 'SELECT A CANDIDATE FOR PROBE AND EVIDENCE',
})

const ROOT_ZH: Record<string, string> = {
  carrier_down: '必要物理网卡失去链路载波',
  default_route_missing: '主机缺少可用默认路由',
  neighbor_unreachable: '目标邻居无法在本地链路解析',
  service_failed: '必要系统服务处于失败状态',
  disk_pressure: '可写磁盘空间使用率达到 90%',
  memory_pressure: '主机可用内存低于 10%',
  healthcheck_failed: '本地调查服务健康检查失败',
  system_errors: '主机日志出现近期错误事件',
  kernel_errors: '内核出现错误或严重事件',
}

export function HypothesisRail({
  lang,
  view,
  rounds,
  onEvidence,
}: {
  lang: Lang
  view: HypothesisView | null
  rounds: ProbeRound[]
  onEvidence: (evidenceId: string) => void
}) {
  const zh = lang === 'zh'
  const tx = useMemo(() => labels(zh), [zh])
  const hypotheses = useMemo(() => view?.hypotheses ?? [], [view?.hypotheses])
  const defaultId = hypotheses.find((item) => item.status === 'confirmed')?.hypothesis_id
    ?? hypotheses.find((item) => item.status === 'testing')?.hypothesis_id
    ?? hypotheses[0]?.hypothesis_id
    ?? ''
  const [selectedId, setSelectedId] = useState(defaultId)

  if (!view || !hypotheses.length) return null
  const effectiveSelectedId = hypotheses.some((item) => item.hypothesis_id === selectedId)
    ? selectedId
    : defaultId
  const selected = hypotheses.find((item) => item.hypothesis_id === effectiveSelectedId) ?? hypotheses[0]
  const probe = view.probes.find((item) => item.distinguishes_hypothesis_ids.includes(selected.hypothesis_id))
  const round = probe ? [...rounds].reverse().find((item) => item.probe_id === probe.probe_id) : undefined
  const statusLabel = tx[selected.status]
  const statement = (item: InvestigationHypothesis) => (
    zh ? ROOT_ZH[item.hypothesis_id] ?? item.statement : item.statement
  )

  const evidenceLinks = (ids: string[]) => ids.length ? ids.map((id) => (
    <button key={id} type="button" onClick={() => onEvidence(id)}>{id}</button>
  )) : <span>{zh ? '无' : 'NONE'}</span>

  return (
    <section className="dx-hyp" aria-label={tx.title}>
      <header className="dx-hyp-head">
        <b>{tx.title}</b>
        <span>{tx.version} {view.state_version}</span>
        <small>{tx.detail}</small>
      </header>
      <div className="dx-hyp-track" role="list">
        {hypotheses.map((item, index) => (
          <button
            type="button"
            role="listitem"
            key={item.hypothesis_id}
            className={`dx-hyp-node is-${item.status}${item.hypothesis_id === selected.hypothesis_id ? ' is-selected' : ''}`}
            onClick={() => setSelectedId(item.hypothesis_id)}
            aria-pressed={item.hypothesis_id === selected.hypothesis_id}
          >
            <span>{String(index + 1).padStart(2, '0')}</span>
            <b>{statement(item)}</b>
            <em>{tx[item.status]}</em>
          </button>
        ))}
      </div>
      <div className={`dx-hyp-detail is-${selected.status}`}>
        <div>
          <span>{statusLabel}</span>
          <b>{statement(selected)}</b>
        </div>
        <dl>
          <div><dt>{tx.probe}</dt><dd><code>{probe?.description ?? tx.notRun}</code></dd></div>
          <div><dt>{tx.support}</dt><dd>{evidenceLinks(selected.supporting_evidence_ids)}</dd></div>
          <div><dt>{tx.oppose}</dt><dd>{evidenceLinks(selected.opposing_evidence_ids)}</dd></div>
        </dl>
        {round ? <footer>{round.ok ? 'OK' : 'FAILED'} · {round.evidence_id ?? tx.notRun}</footer> : null}
      </div>
    </section>
  )
}
