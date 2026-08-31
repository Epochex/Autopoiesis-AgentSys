import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Lang } from '../i18n'
import './situational-overview.css'

type Severity = 'critical' | 'high' | 'medium' | 'low' | string

interface Segment {
  id: string
  cidr: string
  name: string
  role: string
  interfaceStatus: string
  vlanId?: number | null
  assetCount: number
  active24h: number
}

interface AssetActivity {
  flows24h: number
  bytes24h: number
  denied24h: number
  peers24h: number
  lastSeenAt?: string
  observedOutboundServices?: string[]
}

interface Asset {
  ip: string
  mac?: string | null
  name: string
  segment: string
  active24h: boolean
  risk?: Severity | null
  activity?: AssetActivity | null
}

interface Change {
  id: string
  at: string
  severity: Severity
  asset: string
  kind: string
  title: string
  evidenceSource: string
  caseIds: string[]
}

interface RiskAsset {
  asset: string
  name: string
  mac?: string | null
  segment: string
  severity: Severity
  reasons: string[]
  caseIds: string[]
  activity?: AssetActivity | null
}

interface BoundaryRecord {
  id: string
  source: string
  destination: string
  sourceSegment: string
  destinationSegment: string
  service: string
  port?: number | null
  action: string
  flows: number
  lastSeenAt: string
}

interface CandidatePath {
  id: string
  state: string
  label: string
  steps: { kind: string; label: string; segment?: string }[]
  flows: number
  lastSeenAt: string
  evidence: { source: string; action: string }
}

interface ProductionCase {
  caseId: string
  status: string
  severity: Severity
  subject: string
  service: string
  title: string
  summary: string
  lastSeenAt: string
  evidenceCount: number
  action: string
  readback?: Record<string, unknown> | null
}

interface ExternalSource {
  ip: string
  events: number
  eventTypes: string[]
  ports: number[]
  lastSeenAt: string
  intelMatch?: { source: string; label: string; updatedAt?: string } | null
}

interface Coverage {
  capability: string
  state: 'covered' | 'blind' | string
  label: string
  requires?: string | null
}

interface Overview {
  ok: boolean
  mode: 'production_observed'
  observedAt: string
  freshness: {
    latestFactAt?: string | null
    lagSeconds?: number | null
    routerFetchedAt?: string | null
    routerDegraded: boolean
  }
  inventory: { knownAssets: number; active24h: number; segments: Segment[]; assets: Asset[] }
  changes: Change[]
  behaviorDeviations: { id: string; asset: string; reasons: string[] }[]
  crossSegment: { records: BoundaryRecord[]; acceptedVisible: boolean; sameSegmentVisible: boolean }
  riskFusion: RiskAsset[]
  candidatePaths: CandidatePath[]
  externalSources: ExternalSource[]
  cases: ProductionCase[]
  funnel: {
    facts: number
    security_events: number
    alerts: number
    cases: number
    investigating: number
    escalated: number
    resolved: number
    actionsVerified: number
  }
  effectMeasurement: {
    qualified: boolean
    completedInvestigations: number
    recurrenceCohorts: number
    medianDecisionSeconds?: number | null
    medianClosureSeconds?: number | null
  }
  coverage: Coverage[]
  router: { interfaces: number; policies: number; devices: number }
}

type Selection =
  | { type: 'change'; id: string }
  | { type: 'risk'; id: string }
  | { type: 'path'; id: string }
  | { type: 'external'; id: string }
  | null

const compact = (value: number): string => {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`
  if (value >= 10_000) return `${Math.round(value / 1_000)}k`
  return value.toLocaleString()
}

const clock = (value?: string | null): string => {
  if (!value) return 'N/A'
  const parsed = new Date(value.includes('T') ? value : `${value.replace(' ', 'T')}Z`)
  if (Number.isNaN(parsed.valueOf())) return value.slice(11, 19)
  return parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

const relativeFreshness = (seconds?: number | null, zh = true): string => {
  if (seconds == null) return zh ? '事实流时间未知' : 'fact stream time unavailable'
  if (seconds < 15) return zh ? '事实流刚刚更新' : 'fact stream current'
  if (seconds < 120) return zh ? `${seconds} 秒前更新` : `updated ${seconds}s ago`
  return zh ? `${Math.floor(seconds / 60)} 分钟前更新` : `updated ${Math.floor(seconds / 60)}m ago`
}

const caseStatus = (status: string, zh: boolean): string => {
  const labels: Record<string, [string, string]> = {
    open: ['待调查', 'OPEN'], investigating: ['调查中', 'INVESTIGATING'],
    escalated: ['需进一步处置', 'ESCALATED'], resolved: ['已关闭', 'RESOLVED'],
  }
  return (labels[status] ?? [status, status])[zh ? 0 : 1]
}

export function SituationalOverview({
  lang,
  onOpenCase,
}: {
  lang: Lang
  onOpenCase: (subject: string, caseId: string) => void
}) {
  const zh = lang === 'zh'
  const [data, setData] = useState<Overview | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [selection, setSelection] = useState<Selection>(null)
  const [segment, setSegment] = useState<string>('all')

  const load = useCallback(async () => {
    try {
      const response = await fetch('/api/rca/situational-overview', { headers: { Accept: 'application/json' } })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const result = await response.json() as Overview
      setData(result)
      setState('ready')
      setSelection((current) => current ?? (result.changes[0] ? { type: 'change', id: result.changes[0].id } : null))
    } catch {
      setState((current) => current === 'loading' ? 'error' : current)
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 15_000)
    return () => window.clearInterval(timer)
  }, [load])

  const selected = useMemo(() => {
    if (!data || !selection) return null
    if (selection.type === 'change') return data.changes.find((item) => item.id === selection.id) ?? null
    if (selection.type === 'risk') return data.riskFusion.find((item) => item.asset === selection.id) ?? null
    if (selection.type === 'path') return data.candidatePaths.find((item) => item.id === selection.id) ?? null
    return data.externalSources.find((item) => item.ip === selection.id) ?? null
  }, [data, selection])

  if (state === 'loading') return <main className="sa sa-message"><span className="orbit" />{zh ? '正在读取生产事实…' : 'READING PRODUCTION FACTS…'}</main>
  if (state === 'error' || !data) return <main className="sa sa-message is-error">{zh ? '生产态势接口暂时不可达' : 'PRODUCTION SITUATION ENDPOINT UNREACHABLE'}</main>

  const activeSegments = data.inventory.segments.filter((item) => item.assetCount > 0)
  const visibleRisks = data.riskFusion.filter((item) => segment === 'all' || item.segment === segment)
  const visibleAssets = data.inventory.assets.filter((item) => segment === 'all' || item.segment === segment)
  const blind = data.coverage.filter((item) => item.state === 'blind')
  const activeCases = data.cases.filter((item) => ['open', 'investigating', 'escalated'].includes(item.status))
  const selectedCases = selection?.type === 'change'
    ? data.cases.filter((item) => (selected as Change | null)?.caseIds?.includes(item.caseId))
    : selection?.type === 'risk'
      ? data.cases.filter((item) => (selected as RiskAsset | null)?.caseIds?.includes(item.caseId))
      : []

  return (
    <main className="sa" aria-label={zh ? '生产内网态势' : 'Production network situation'}>
      <header className="sa-intro">
        <div>
          <span className="sa-eyebrow">{zh ? '生产观测 · 当前网络' : 'PRODUCTION OBSERVATION · CURRENT NETWORK'}</span>
          <h1>{zh ? <>内网现在<mark>发生了什么</mark></> : <>WHAT IS <mark>HAPPENING NOW</mark></>}</h1>
          <p>
            {zh
              ? `${data.inventory.knownAssets} 个可寻址资产，${data.inventory.active24h} 个在 24 小时内留下记录，${activeCases.length} 起高影响案件仍需处理。`
              : `${data.inventory.knownAssets} addressable assets, ${data.inventory.active24h} active in 24 hours, ${activeCases.length} high-impact cases need attention.`}
          </p>
        </div>
        <div className="sa-source-stamp">
          <span className={data.freshness.lagSeconds != null && data.freshness.lagSeconds < 120 ? 'is-live' : 'is-stale'} />
          <b>{relativeFreshness(data.freshness.lagSeconds, zh)}</b>
          <small>ClickHouse · FortiGate · {zh ? '生产案件库' : 'production case store'}</small>
        </div>
      </header>

      <section className="sa-pulse" aria-label={zh ? '生产链路漏斗' : 'production funnel'}>
        {[
          [zh ? '网络记录' : 'NETWORK FACTS', data.funnel.facts],
          [zh ? '安全事件' : 'SECURITY EVENTS', data.funnel.security_events],
          [zh ? '规则告警' : 'RULE ALERTS', data.funnel.alerts],
          [zh ? '高影响案件' : 'HIGH-IMPACT CASES', data.funnel.cases],
          [zh ? '结果已回读' : 'VERIFIED OUTCOMES', data.funnel.actionsVerified],
        ].map(([label, value], index) => (
          <div className="sa-pulse-step" key={String(label)}>
            <span>{String(index + 1).padStart(2, '0')}</span>
            <b>{compact(Number(value))}</b>
            <small>{label}</small>
          </div>
        ))}
      </section>

      <section className="sa-field">
        <div className="sa-segments">
          <div className="sa-section-title">
            <span>{zh ? '资产平面' : 'ASSET PLANE'}</span>
            <button className={segment === 'all' ? 'is-on' : ''} onClick={() => setSegment('all')}>
              {zh ? '全网' : 'ALL'}
            </button>
          </div>
          <div className="sa-segment-lanes">
            {activeSegments.map((item) => {
              const assets = data.inventory.assets.filter((asset) => asset.segment === item.cidr)
              return (
                <button
                  key={item.id}
                  className={`sa-segment ${segment === item.cidr ? 'is-on' : ''}`}
                  onClick={() => setSegment(item.cidr)}
                >
                  <span className="sa-segment-name">{item.name}</span>
                  <code>{item.cidr}</code>
                  <div className="sa-asset-dots" aria-hidden="true">
                    {assets.slice(0, 28).map((asset) => (
                      <i key={asset.ip} className={`${asset.active24h ? 'is-active' : ''} ${asset.risk ? `sev-${asset.risk}` : ''}`} />
                    ))}
                    {assets.length > 28 ? <em>+{assets.length - 28}</em> : null}
                  </div>
                  <small>{item.active24h}/{item.assetCount} {zh ? '活跃' : 'active'}</small>
                </button>
              )
            })}
          </div>
          <div className="sa-field-caption">
            <span>{zh ? `当前范围 ${segment === 'all' ? '全网' : segment}` : `scope ${segment}`}</span>
            <span>{visibleAssets.filter((item) => item.active24h).length} {zh ? '个活跃地址' : 'active addresses'}</span>
            <span>{visibleRisks.length} {zh ? '个需关注对象' : 'objects need attention'}</span>
          </div>
        </div>

        <div className="sa-change-line">
          <div className="sa-section-title"><span>{zh ? '刚刚发生的变化' : 'RECENT CHANGES'}</span><small>{clock(data.changes[0]?.at)}</small></div>
          <ol>
            {data.changes.slice(0, 7).map((item) => (
              <li key={item.id} className={`sev-${item.severity}`}>
                <button onClick={() => setSelection({ type: 'change', id: item.id })} aria-current={selection?.type === 'change' && selection.id === item.id}>
                  <time>{clock(item.at)}</time>
                  <i />
                  <span><b>{item.asset}</b><small>{item.title}</small></span>
                </button>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="sa-boundaries">
        <div className="sa-section-title">
          <span>{zh ? '跨区访问留下的网络证据链' : 'CROSS-ZONE NETWORK EVIDENCE'}</span>
          <small>{data.crossSegment.acceptedVisible ? (zh ? '包含成功与阻断记录' : 'allowed and blocked visible') : (zh ? '当前数据只含阻断记录' : 'current data contains blocked records only')}</small>
        </div>
        {data.candidatePaths.length ? (
          <div className="sa-paths">
            {data.candidatePaths.slice(0, 6).map((path) => (
              <button key={path.id} onClick={() => setSelection({ type: 'path', id: path.id })} aria-current={selection?.type === 'path' && selection.id === path.id}>
                <span className="sa-path-node"><b>{path.steps[0]?.label}</b><small>{path.steps[0]?.segment}</small></span>
                <i className="sa-path-wire"><em>{compact(path.flows)}</em></i>
                <span className="sa-path-gate">{path.steps[1]?.label}</span>
                <i className="sa-path-wire is-short" />
                <span className="sa-path-node is-target"><b>{path.steps[2]?.label}</b><small>{path.steps[2]?.segment}</small></span>
              </button>
            ))}
          </div>
        ) : <p className="sa-empty">{zh ? '最近 24 小时没有跨网段记录。' : 'No cross-zone records in the latest 24 hours.'}</p>}
      </section>

      <section className="sa-workbench">
        <div className="sa-risk-radar">
          <div className="sa-section-title"><span>{zh ? '需要处理的对象' : 'OBJECTS REQUIRING ACTION'}</span><small>{visibleRisks.length}</small></div>
          <div className="sa-risk-head"><span>{zh ? '资产' : 'ASSET'}</span><span>{zh ? '为什么出现' : 'WHY IT APPEARS'}</span><span>{zh ? '状态' : 'STATE'}</span></div>
          {visibleRisks.slice(0, 10).map((item) => (
            <button key={item.asset} className={`sa-risk-row sev-${item.severity}`} onClick={() => setSelection({ type: 'risk', id: item.asset })} aria-current={selection?.type === 'risk' && selection.id === item.asset}>
              <span><b>{item.name}</b><code>{item.asset}</code></span>
              <span>{item.reasons[0]}</span>
              <span>{item.caseIds.length ? (zh ? '已开案' : 'CASE OPEN') : (zh ? '持续观察' : 'UNDER WATCH')}</span>
            </button>
          ))}
        </div>

        <aside className="sa-inspector" aria-live="polite">
          <div className="sa-section-title"><span>{zh ? '所选对象' : 'SELECTED OBJECT'}</span><small>{selection?.type ?? ''}</small></div>
          {!selected ? <p className="sa-empty">{zh ? '选择一条变化、路径或资产查看依据。' : 'Select a change, path, or asset to inspect.'}</p> : null}

          {selection?.type === 'change' && selected ? (
            <>
              <span className={`sa-severity sev-${(selected as Change).severity}`}>{(selected as Change).severity}</span>
              <h2>{(selected as Change).title}</h2>
              <dl><div><dt>{zh ? '对象' : 'OBJECT'}</dt><dd>{(selected as Change).asset}</dd></div><div><dt>{zh ? '证据源' : 'SOURCE'}</dt><dd>{(selected as Change).evidenceSource}</dd></div><div><dt>{zh ? '时间' : 'TIME'}</dt><dd>{clock((selected as Change).at)}</dd></div></dl>
            </>
          ) : null}

          {selection?.type === 'risk' && selected ? (
            <>
              <span className={`sa-severity sev-${(selected as RiskAsset).severity}`}>{(selected as RiskAsset).severity}</span>
              <h2>{(selected as RiskAsset).name}</h2>
              <code>{(selected as RiskAsset).asset} · {(selected as RiskAsset).segment}</code>
              <ul>{(selected as RiskAsset).reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
              {(selected as RiskAsset).activity ? <p className="sa-measure">{compact((selected as RiskAsset).activity?.flows24h ?? 0)} {zh ? '条记录' : 'records'} · {compact((selected as RiskAsset).activity?.denied24h ?? 0)} {zh ? '条被拒绝' : 'denied'} · {(selected as RiskAsset).activity?.peers24h} {zh ? '个目标' : 'peers'}</p> : null}
            </>
          ) : null}

          {selection?.type === 'path' && selected ? (
            <>
              <span className="sa-severity sev-medium">{(selected as CandidatePath).label}</span>
              <h2>{(selected as CandidatePath).steps.map((step) => step.label).join(' → ')}</h2>
              <p>{zh ? `${compact((selected as CandidatePath).flows)} 条设备记录支持这条链，设备结果为 ${(selected as CandidatePath).evidence.action}。` : `${compact((selected as CandidatePath).flows)} device records support this chain; device result: ${(selected as CandidatePath).evidence.action}.`}</p>
              <code>{(selected as CandidatePath).evidence.source} · {clock((selected as CandidatePath).lastSeenAt)}</code>
            </>
          ) : null}

          {selection?.type === 'external' && selected ? (
            <>
              <span className="sa-severity sev-high">{zh ? '公网管理入口活动' : 'INTERNET MANAGEMENT ACTIVITY'}</span>
              <h2>{(selected as ExternalSource).ip}</h2>
              <p>{zh
                ? `${compact((selected as ExternalSource).events)} 条去重安全事件，涉及端口 ${(selected as ExternalSource).ports.join('、') || 'N/A'}。`
                : `${compact((selected as ExternalSource).events)} unique security events on ports ${(selected as ExternalSource).ports.join(', ') || 'N/A'}.`}</p>
              <ul>{(selected as ExternalSource).eventTypes.map((kind) => <li key={kind}>{kind}</li>)}</ul>
              {(selected as ExternalSource).intelMatch ? <p className="sa-measure">{(selected as ExternalSource).intelMatch?.source} · {(selected as ExternalSource).intelMatch?.label}</p> : <code>{zh ? '当前仅有本地设备证据，未接入外部来源标签' : 'local device evidence only; no external source label configured'}</code>}
            </>
          ) : null}

          {selectedCases.map((item) => (
            <button className="sa-case-link" key={item.caseId} onClick={() => onOpenCase(item.subject, item.caseId)}>
              <span>{caseStatus(item.status, zh)}</span><b>{item.title}</b><small>{item.evidenceCount} {zh ? '项证据' : 'evidence items'} · {zh ? '打开调查' : 'open investigation'} ▸</small>
            </button>
          ))}
        </aside>
      </section>

      {data.externalSources.length ? (
        <section className="sa-external">
          <div className="sa-section-title"><span>{zh ? '公网入口压力' : 'INTERNET-FACING PRESSURE'}</span><small>{zh ? '管理探测、登录失败与成功会话记录' : 'management probes, login failures and successful session records'}</small></div>
          <div className="sa-external-track">
            {data.externalSources.slice(0, 12).map((item) => (
              <button key={item.ip} onClick={() => setSelection({ type: 'external', id: item.ip })}>
                <b>{item.ip}</b><i style={{ height: `${Math.max(8, Math.min(54, Math.log10(item.events + 1) * 14))}px` }} /><small>{compact(item.events)}</small>
              </button>
            ))}
          </div>
        </section>
      ) : null}

      {data.effectMeasurement.qualified ? (
        <section className="sa-effect">
          <b>{zh ? '长期效果已有足够生产样本' : 'LONG-TERM EFFECT HAS ENOUGH PRODUCTION SAMPLES'}</b>
          <span>{data.effectMeasurement.completedInvestigations} {zh ? '起调查' : 'investigations'}</span>
          <span>{data.effectMeasurement.recurrenceCohorts} {zh ? '组复发配对' : 'recurrence cohorts'}</span>
          <span>{data.effectMeasurement.medianDecisionSeconds}s {zh ? '中位形成决定时间' : 'median decision time'}</span>
        </section>
      ) : null}

      <details className="sa-coverage">
        <summary>{zh ? `当前还有 ${blind.length} 个观测盲区，展开查看接入条件` : `${blind.length} observation gaps, open for required sensors`}</summary>
        <div>
          {blind.map((item) => <p key={item.capability}><b>{item.label}</b><span>{item.requires}</span></p>)}
        </div>
      </details>
    </main>
  )
}
