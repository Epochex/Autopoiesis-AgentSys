import { lazy, Suspense } from 'react'
import { ErrorBoundary } from './ErrorBoundary'
import type { ControlLoopSnapshot, SuggestionRecord } from '../types'

const TopologyCanvas = lazy(() =>
  import('./TopologyCanvas').then((module) => ({
    default: module.TopologyCanvas,
  })),
)

interface PipelineTopologyViewProps {
  snapshot: ControlLoopSnapshot
  locale: 'en' | 'zh'
}

function byLatestSuggestion(a: SuggestionRecord, b: SuggestionRecord) {
  return (
    new Date(b.suggestionTs).getTime() - new Date(a.suggestionTs).getTime()
  )
}

function mostFrequent(items: string[]) {
  const counts = new Map<string, number>()

  items
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((item) => {
      counts.set(item, (counts.get(item) ?? 0) + 1)
    })

  return [...counts.entries()].sort((left, right) => right[1] - left[1])[0]?.[0]
}

function formatStageState(status: ControlLoopSnapshot['stageNodes'][number]['status']) {
  switch (status) {
    case 'flowing':
      return 'live path'
    case 'watch':
      return 'watch gate'
    case 'planned':
      return 'reserved boundary'
    default:
      return 'steady lane'
  }
}

function layoutSystemFlowNodes(nodes: ControlLoopSnapshot['stageNodes']) {
  const positions: Record<string, { x: number; y: number }> = {
    'lcore-source': { x: 0, y: 96 },
    fortigate: { x: 0, y: 96 },
    ingest: { x: 232, y: 96 },
    forwarder: { x: 464, y: 96 },
    'raw-topic': { x: 696, y: 96 },
    correlator: { x: 696, y: 308 },
    'alerts-topic': { x: 960, y: 308 },
    'aiops-agent': { x: 1224, y: 308 },
    'suggestions-topic': { x: 1488, y: 308 },
    'alerts-sink': { x: 960, y: 520 },
    clickhouse: { x: 1224, y: 520 },
    remediation: { x: 1488, y: 520 },
  }

  return nodes.map((node) => ({
    ...node,
    x: positions[node.id]?.x ?? node.x,
    y: positions[node.id]?.y ?? node.y,
  }))
}

export function PipelineTopologyView({
  snapshot,
  locale,
}: PipelineTopologyViewProps) {
  const suggestions = [...snapshot.suggestions].sort(byLatestSuggestion)
  const latestSuggestion = suggestions[0]
  const systemNodes = layoutSystemFlowNodes(snapshot.stageNodes)

  const serviceCount = new Set(
    suggestions.map((suggestion) => suggestion.context.service).filter(Boolean),
  ).size
  const deviceCount = new Set(
    suggestions
      .map((suggestion) => suggestion.context.srcDeviceKey)
      .filter(Boolean),
  ).size
  const clusterIncidentCount = suggestions.filter(
    (suggestion) => suggestion.scope === 'cluster',
  ).length
  const singlePathCount = suggestions.length - clusterIncidentCount
  const readyClusterCount = snapshot.clusterWatch.filter(
    (item) => item.progress >= item.target,
  ).length
  const warmingClusterCount = snapshot.clusterWatch.filter(
    (item) => item.progress < item.target,
  ).length
  const dominantAction =
    mostFrequent(
      suggestions.flatMap((suggestion) => suggestion.recommendedActions),
    ) ?? 'No remediation action has been emitted yet.'
  const dominantRule =
    mostFrequent(suggestions.map((suggestion) => suggestion.ruleId)) ??
    'No dominant rule in the current slice'
  const highlightedStages = snapshot.stageNodes.filter(
    (node) => node.status === 'flowing' || node.status === 'watch',
  )
  const stageFocus =
    highlightedStages.length > 0 ? highlightedStages : snapshot.stageNodes
  const activeStageSummary =
    stageFocus.length > 0
      ? stageFocus
          .slice(0, 3)
          .map((node) => node.title)
          .join(' -> ')
      : 'No active control-loop path is available in the current snapshot.'
  const systemVerdict = latestSuggestion
    ? clusterIncidentCount > 0
      ? `${clusterIncidentCount} repeated-pattern incident(s) already crossed the cluster gate, so this map is currently showing a system-level pattern instead of a single noisy port.`
      : `${singlePathCount} single-path incident(s) are still below cluster gate, so the system is tracking isolated pressure before it becomes a repeated pattern.`
    : 'No live suggestion is currently active, so the map is acting as a structural reference view.'
  const focusExplanation = latestSuggestion
    ? `${latestSuggestion.summary} Dominant rule: ${dominantRule}. Latest scope: ${latestSuggestion.scope === 'cluster' ? 'repeated-pattern incident' : 'single-path incident'}.`
    : 'Suggestions have not populated yet, so use this view as the end-to-end system map only.'
  const copy =
    locale === 'zh'
      ? {
          kicker: '系统总览 / 全链路',
          title: '系统链路地图',
          subtitle: '这一页只保留系统级链路，不再让单条 suggestion 把地图切碎。',
          incidents: '活跃事件',
          footprint: '影响范围',
          cluster: '聚合态势',
          action: '主导动作',
          verdict: '系统判读',
          readMap: '地图读法',
          pathRibbon: '当前主链路',
          loading: '正在载入系统链路图...',
        }
      : {
          kicker: 'integrated control-loop map',
          title: 'System Flow Map',
          subtitle: 'This page stays system-wide and keeps the map readable instead of fragmenting it around one suggestion at a time.',
          incidents: 'Active incidents',
          footprint: 'Affected footprint',
          cluster: 'Cluster posture',
          action: 'Dominant action',
          verdict: 'System verdict',
          readMap: 'How to read',
          pathRibbon: 'Current path',
          loading: 'loading system flow map...',
        }

  return (
    <section className="page system-flow-page">
      <section className="section system-flow-stage">
        <div className="system-flow-map-shell">
          <div className="system-flow-topband">
            <div className="system-flow-heading swiss-flow-heading">
              <span className="section-kicker">{copy.kicker}</span>
              <h2 className="section-title">{copy.title}</h2>
              <span className="section-subtitle">
                {copy.subtitle}
              </span>
              <div className="system-flow-ribbons">
                <article className="system-flow-ribbon-card">
                  <span>{copy.verdict}</span>
                  <strong>
                    {latestSuggestion?.summary ??
                      (locale === 'zh'
                        ? '当前没有可展开的建议切片。'
                        : 'No live suggestion is available right now.')}
                  </strong>
                  <p>{systemVerdict}</p>
                </article>
                <article className="system-flow-ribbon-card">
                  <span>{copy.readMap}</span>
                  <strong>{activeStageSummary}</strong>
                  <p>{focusExplanation}</p>
                </article>
              </div>
            </div>

            <div className="system-flow-stat-strip">
              <article className="system-flow-stat-card">
                <span>{copy.incidents}</span>
                <strong>{suggestions.length}</strong>
                <p>
                  {locale === 'zh'
                    ? `${clusterIncidentCount} 条重复模式 / ${singlePathCount} 条单路径`
                    : `${clusterIncidentCount} repeated-pattern / ${singlePathCount} single-path`}
                </p>
              </article>
              <article className="system-flow-stat-card">
                <span>{copy.footprint}</span>
                <strong>
                  {locale === 'zh'
                    ? `${serviceCount} 项服务`
                    : `${serviceCount} service${serviceCount === 1 ? '' : 's'}`}
                </strong>
                <p>
                  {locale === 'zh'
                    ? `当前切片涉及 ${deviceCount} 台设备`
                    : `${deviceCount} device${deviceCount === 1 ? '' : 's'} in the current slice`}
                </p>
              </article>
              <article className="system-flow-stat-card">
                <span>{copy.cluster}</span>
                <strong>{readyClusterCount}</strong>
                <p>
                  {locale === 'zh'
                    ? `${warmingClusterCount} 个 watch 槽仍在预热`
                    : `${warmingClusterCount} watch slot${warmingClusterCount === 1 ? '' : 's'} still warming`}
                </p>
              </article>
              <article className="system-flow-stat-card">
                <span>{copy.action}</span>
                <strong>{dominantRule}</strong>
                <p>{dominantAction}</p>
              </article>
            </div>
          </div>

          <ErrorBoundary title="System Flow Map">
            <Suspense
              fallback={
                <div className="flow-frame system-flow-frame chart-fallback">
                  {copy.loading}
                </div>
              }
            >
              <div className="system-flow-canvas-layer">
                <TopologyCanvas
                  nodes={systemNodes}
                  links={snapshot.stageLinks}
                  fitPadding={0.04}
                  nodeWidth={206}
                  showEdgeLabels={false}
                  showMiniMap={false}
                />
              </div>
            </Suspense>
          </ErrorBoundary>

          <div className="system-flow-bottom-rail">
            <article className="system-flow-path-pill system-flow-path-pill-primary">
              <span>{copy.pathRibbon}</span>
              <strong>{activeStageSummary}</strong>
            </article>
            <div className="system-flow-stage-list">
              {snapshot.stageNodes.map((node) => (
                <div key={node.id} className="system-flow-stage-item">
                  <strong>{node.title}</strong>
                  <span>{formatStageState(node.status)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>
    </section>
  )
}
