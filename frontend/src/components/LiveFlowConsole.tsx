import {
  lazy,
  Suspense,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react'
import type { ControlLoopConnectionState } from '../hooks/useControlLoopSnapshot'
import type {
  FeedEvent,
  ProjectionBasisEntry,
  ControlLoopSnapshot,
  ControlLoopStreamDelta,
  StageLink,
  StageNode,
  StageTelemetry,
  StrategyControl,
  SuggestionRecord,
} from '../types'
import {
  formatDurationMs,
  formatMaybeTimestamp,
  parseTimestamp,
  timestampTooltip,
} from '../utils/time'
import { buildIncidentConvergenceModel } from './liveFlowConsoleEventField'
import { buildNodeInspectorSurfaceModel } from './liveFlowConsoleNodeInspector'

const TopologyCanvas = lazy(() =>
  import('./TopologyCanvas').then((module) => ({
    default: module.TopologyCanvas,
  })),
)

interface LiveFlowConsoleProps {
  connectionState: ControlLoopConnectionState
  snapshot: ControlLoopSnapshot
  latestDelta: ControlLoopStreamDelta | null
  selectedSuggestion: SuggestionRecord
  onSelectSuggestion: (suggestionId: string) => void
  transportIssue?: string | null
  locale: 'en' | 'zh'
  heroStageRef?: RefObject<HTMLElement | null>
}

interface LifecycleBand {
  mode: StageTelemetry['mode']
  state: StageTelemetry['state']
  label: string
  value: string
  detail: string
  stamp?: string
  meter: number
  durationMs?: number | null
  tone: 'raw' | 'alert' | 'suggestion' | 'neutral' | 'planned'
}

interface LifecyclePhase {
  id: string
  title: string
  purpose: string
  systems: string
  status: StageNode['status']
  stageIds: string[]
  facts: string[]
  band: LifecycleBand
}

interface LifecycleIntegrityIssue {
  id: string
  detail: string
  severity: 'hard' | 'soft'
}

interface HistoricalIncidentEvent extends FeedEvent {
  dedupeKey: string
  mergedSuggestionCount: number
  firstSeenTs: string
  lastSeenTs: string
  inference: string
  recommendation: string
  scopeMeaning: string
}

interface GuidedStage {
  id: string
  title: string
  summary: string
  value: string
  detail: string
  tone: LifecycleBand['tone']
  status: StageNode['status']
  phaseIds: string[]
  facts: string[]
  stamp?: string
  durationMs?: number | null
}

interface StoryCopy {
  eyebrow: string
  headlinePrimary: string
  headlineDevice: string
  headlineRoute: string
  summary: string
  whatHappenedLabel: string
  whatHappened: string
  whyLabel: string
  why: string
  nextLabel: string
  next: string
}

interface ProjectorFact {
  label: string
  value: string
}

type ProjectorEvidenceState = 'none' | 'partial' | 'rich'
type ProjectorTimingSource =
  | 'telemetry-window'
  | 'phase-sum'
  | 'timeline-window'
  | 'timeline-sum'
  | 'demo-clock'

interface ProjectorTimingMeasurement {
  durationMs: number | null
  source: ProjectorTimingSource
}

interface ProjectorStation {
  id: string
  title: string
  token: string
  caption: string
  detail: string
  facts: ProjectorFact[]
  sources: ProjectionBasisEntry[]
  guidedStageId: string
  evidenceState: ProjectorEvidenceState
}

const SCRAMBLE_GLYPHS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/+*-=<>'
const PROJECTOR_CLOCK_DURATION_MS = 3_200

function scrambleProjectorText(target: string, progress: number) {
  const revealCount = Math.floor(target.length * progress)

  return target
    .split('')
    .map((char, index) => {
      if (char === ' ') {
        return ' '
      }

      if (index < revealCount) {
        return char
      }

      return SCRAMBLE_GLYPHS[Math.floor(Math.random() * SCRAMBLE_GLYPHS.length)]
    })
    .join('')
}

function formatProjectorTimer(ms: number) {
  return `${(Math.max(0, ms) / 1000).toFixed(2)}s`
}

function positiveDurationMs(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? value
    : null
}

function measuredDurationWindowMs(
  startedAt: string | null | undefined,
  endedAt: string | null | undefined,
) {
  const startedAtStamp = parseTimestamp(startedAt)
  const endedAtStamp = parseTimestamp(endedAt)
  if (!startedAtStamp || !endedAtStamp) {
    return null
  }

  const durationMs = endedAtStamp.getTime() - startedAtStamp.getTime()
  return durationMs > 0 ? durationMs : null
}

function formatProjectorStationTimer(
  durationMs: number | null,
  hasMeasuredTiming: boolean,
  locale: 'en' | 'zh',
) {
  if (!hasMeasuredTiming) {
    return locale === 'zh' ? '回放' : 'demo'
  }

  if (durationMs === null) {
    return 'n/a'
  }

  return formatProjectorTimer(durationMs)
}

function isIpLike(value: string) {
  return /^(?:\d{1,3}\.){3}\d{1,3}$/.test(value) || value.includes(':')
}

function isMacLike(value: string) {
  return /^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$/i.test(value)
}

function controlValue(controls: StrategyControl[], label: string) {
  return controls.find((control) => control.label === label)?.currentValue ?? 'n/a'
}

function stageMetricValue(node: StageNode | undefined, label: string) {
  return node?.metrics.find((metric) => metric.label === label)?.value ?? 'n/a'
}

function buildLifecycle(
  snapshot: ControlLoopSnapshot,
  linkedSuggestion: SuggestionRecord,
): LifecyclePhase[] {
  const stageLookup = new Map(snapshot.stageNodes.map((node) => [node.id, node]))
  const telemetryLookup = new Map(
    (linkedSuggestion.stageTelemetry ?? []).map((item) => [item.stageId, item]),
  )
  const clusterTarget = Number.parseInt(
    controlValue(snapshot.strategyControls, 'AIOPS_CLUSTER_MIN_ALERTS'),
    10,
  )
  const clusterWindow = Number.parseInt(
    controlValue(snapshot.strategyControls, 'AIOPS_CLUSTER_WINDOW_SEC'),
    10,
  )
  const safeClusterTarget = Number.isFinite(clusterTarget) ? clusterTarget : 3
  const safeClusterWindow = Number.isFinite(clusterWindow) ? clusterWindow : 600
  const clusterProgress = Math.max(
    0,
    ...snapshot.clusterWatch.map((item) => item.progress),
  )
  const clusterRemaining = Math.max(0, safeClusterTarget - clusterProgress)
  const lcoreSource = stageLookup.get('lcore-source')
  const ingest = stageLookup.get('ingest')
  const forwarder = stageLookup.get('forwarder')
  const rawTopic = stageLookup.get('raw-topic')
  const correlator = stageLookup.get('correlator')
  const alertsTopic = stageLookup.get('alerts-topic')
  const aiopsAgent = stageLookup.get('aiops-agent')
  const suggestionsTopic = stageLookup.get('suggestions-topic')
  const remediation = stageLookup.get('remediation')
  const sourceTs =
    telemetryLookup.get('raw-topic')?.endedAt ??
    telemetryLookup.get('ingest')?.endedAt ??
    telemetryLookup.get('lcore-source')?.endedAt
  const handoffTs =
    telemetryLookup.get('raw-topic')?.endedAt ??
    telemetryLookup.get('forwarder')?.endedAt ??
    telemetryLookup.get('ingest')?.endedAt
  const alertTs =
    telemetryLookup.get('alerts-topic')?.endedAt ??
    telemetryLookup.get('correlator')?.endedAt
  const suggestionTs =
    telemetryLookup.get('suggestions-topic')?.endedAt ??
    telemetryLookup.get('aiops-agent')?.endedAt
  const clusterTelemetry = telemetryLookup.get('cluster-window')
  const clusterState: StageNode['status'] =
    linkedSuggestion.scope === 'cluster'
      ? 'flowing'
      : clusterProgress > 0
        ? 'watch'
        : 'steady'

  return [
    {
      id: 'source-signal',
      title: 'Source Signal',
      purpose: 'A core-router telemetry fact has entered the platform.',
      systems: 'LCORE-D edge source',
      status: lcoreSource?.status ?? 'steady',
      stageIds: ['lcore-source'],
      facts: [
        stageMetricValue(lcoreSource, 'mode'),
        stageMetricValue(lcoreSource, 'signal'),
      ],
      band: {
        mode: 'timestamp',
        state: 'active',
        label: 'seen',
        value: sourceTs ? formatMaybeTimestamp(sourceTs, 'time') : 'live',
        detail: 'LCORE source plane stays hot until a new canonical fact arrives',
        stamp: sourceTs,
        meter: 100,
        durationMs: 0,
        tone: 'raw',
      },
    },
    {
      id: 'edge-handoff',
      title: 'Edge Parse + Handoff',
      purpose: 'The edge normalizes, checkpoints, and hands the fact to the raw stream.',
      systems: 'lcore-streamer -> edge-forwarder -> netops.facts.raw.v1',
      status:
        rawTopic?.status === 'flowing' || ingest?.status === 'flowing'
          ? 'flowing'
          : 'steady',
      stageIds: ['ingest', 'forwarder', 'raw-topic'],
      facts: [
        `parsed ${stageMetricValue(ingest, 'parsed')}`,
        `freshness ${stageMetricValue(rawTopic, 'freshness')}`,
        `lossless ${stageMetricValue(forwarder, 'lossless')}`,
      ],
      band: {
        mode: 'timestamp',
        state: 'complete',
        label: 'handoff',
        value: handoffTs ? formatMaybeTimestamp(handoffTs, 'time') : 'ready',
        detail: `backlog ${stageMetricValue(ingest, 'backlog')} · raw topic ready`,
        stamp: handoffTs,
        meter: 100,
        durationMs: 0,
        tone: 'raw',
      },
    },
    {
      id: 'deterministic-alert',
      title: 'Deterministic Alert',
      purpose: 'The correlator confirms labeled fault transitions without model participation.',
      systems: 'core-correlator -> netops.alerts.v1',
      status:
        alertsTopic?.status === 'flowing' || correlator?.status === 'flowing'
          ? 'flowing'
          : 'steady',
      stageIds: ['correlator', 'alerts-topic'],
      facts: [
        `rule ${stageMetricValue(correlator, 'active rule')}`,
        `latest ${stageMetricValue(alertsTopic, 'latest')}`,
      ],
      band: {
        mode: telemetryLookup.get('correlator')?.mode ?? 'duration',
        state: telemetryLookup.get('correlator')?.state ?? 'steady',
        label: 'elapsed',
        value: formatDurationMs(telemetryLookup.get('correlator')?.durationMs),
        detail:
          alertTs && sourceTs
            ? `${formatMaybeTimestamp(sourceTs, 'time')} -> ${formatMaybeTimestamp(alertTs, 'time')}`
            : 'source fact to alert emission',
        stamp: alertTs,
        meter: 100,
        durationMs: telemetryLookup.get('correlator')?.durationMs,
        tone: 'alert',
      },
    },
    {
      id: 'cluster-gate',
      title: 'Cluster Gate',
      purpose: 'Repeated same-key alerts are counted until they become cluster-legible.',
      systems: 'same-key aggregation window',
      status: clusterState,
      stageIds: ['cluster-window'],
      facts: [
        `${clusterProgress}/${safeClusterTarget} in ${safeClusterWindow}s`,
        linkedSuggestion.scope === 'cluster'
          ? 'cluster path already reached'
          : `${clusterRemaining} more needed for cluster trigger`,
      ],
      band: {
        mode: 'gate',
        state: clusterTelemetry?.state ?? (clusterProgress > 0 ? 'watch' : 'steady'),
        label: 'progress',
        value: `${clusterProgress}/${safeClusterTarget}`,
        detail:
          linkedSuggestion.scope === 'cluster'
            ? `cluster gate reached inside ${safeClusterWindow}s`
            : `${clusterRemaining} more matching alert(s) needed inside ${safeClusterWindow}s`,
        stamp: clusterTelemetry?.endedAt,
        meter:
          safeClusterTarget > 0
            ? Math.max(10, Math.min(100, (clusterProgress / safeClusterTarget) * 100))
            : 0,
        durationMs: clusterTelemetry?.durationMs,
        tone: 'alert',
      },
    },
    {
      id: 'suggestion-emission',
      title: 'AIOps Suggestion',
      purpose: 'Evidence is bundled into structured operator guidance.',
      systems: 'core-aiops-agent -> netops.aiops.suggestions.v1',
      status:
        suggestionsTopic?.status === 'flowing' || aiopsAgent?.status === 'flowing'
          ? 'flowing'
          : 'steady',
      stageIds: ['aiops-agent', 'suggestions-topic'],
      facts: [
        `${linkedSuggestion.scope}-scope via ${linkedSuggestion.context.provider}`,
        stageMetricValue(suggestionsTopic, 'current day'),
      ],
      band: {
        mode: telemetryLookup.get('aiops-agent')?.mode ?? 'duration',
        state: telemetryLookup.get('aiops-agent')?.state ?? 'steady',
        label: 'elapsed',
        value: formatDurationMs(telemetryLookup.get('aiops-agent')?.durationMs),
        detail:
          suggestionTs && alertTs
            ? `${formatMaybeTimestamp(alertTs, 'time')} -> ${formatMaybeTimestamp(suggestionTs, 'time')}`
            : 'alert evidence to suggestion emission',
        stamp: suggestionTs,
        meter: 100,
        durationMs: telemetryLookup.get('aiops-agent')?.durationMs,
        tone: 'suggestion',
      },
    },
    {
      id: 'remediation-boundary',
      title: 'Remediation Boundary',
      purpose: 'Approval, execution, and feedback stay visible as the next control surface.',
      systems: 'approval -> execution -> feedback',
      status: remediation?.status ?? 'planned',
      stageIds: ['remediation'],
      facts: [
        stageMetricValue(remediation, 'status'),
        stageMetricValue(remediation, 'feedback'),
      ],
      band: {
        mode: 'reserved',
        state: 'planned',
        label: 'boundary',
        value: 'manual',
        detail: 'reserved control point · execution path not wired',
        meter: 10,
        durationMs: null,
        tone: 'planned',
      },
    },
  ]
}

function buildLifecyclePhases(
  snapshot: ControlLoopSnapshot,
  linkedSuggestion: SuggestionRecord,
): LifecyclePhase[] {
  return buildLifecycle(snapshot, linkedSuggestion)
}

function pulseKindForDelta(
  kind: ControlLoopStreamDelta['kind'] | FeedEvent['kind'] | undefined,
): FeedEvent['kind'] {
  if (kind === 'raw' || kind === 'alert') {
    return kind
  }
  return 'suggestion'
}

function toneForDelta(
  kind: ControlLoopStreamDelta['kind'] | FeedEvent['kind'] | undefined,
) {
  if (kind === 'system') {
    return 'live'
  }
  return pulseKindForDelta(kind)
}

function currentPhaseId(
  deltaKind: ControlLoopStreamDelta['kind'] | undefined,
  eventKind: FeedEvent['kind'] | undefined,
) {
  if (deltaKind === 'cluster') {
    return 'cluster-gate'
  }

  if (eventKind === 'raw') {
    return 'edge-handoff'
  }

  if (eventKind === 'alert') {
    return 'deterministic-alert'
  }

  return 'suggestion-emission'
}

function suggestionForEvent(
  event: FeedEvent | undefined,
  suggestions: SuggestionRecord[],
  fallback: SuggestionRecord,
) {
  if (!event) {
    return fallback
  }

  if (event.relatedSuggestionId) {
    const direct = suggestions.find(
      (suggestion) => suggestion.id === event.relatedSuggestionId,
    )
    if (direct) {
      return direct
    }
  }

  if (event.relatedAlertId) {
    const fromAlert = suggestions.find(
      (suggestion) => suggestion.alertId === event.relatedAlertId,
    )
    if (fromAlert) {
      return fromAlert
    }
  }

  if (event.service || event.device) {
    const byContext = suggestions.find(
      (suggestion) =>
        suggestion.context.service === event.service &&
        suggestion.context.srcDeviceKey === event.device,
    )
    if (byContext) {
      return byContext
    }
  }

  return fallback
}

function canonicalDeviceIdentity(suggestion: SuggestionRecord) {
  const srcmac = suggestion.evidenceBundle.device.srcmac
  if (
    typeof srcmac === 'string' &&
    srcmac.trim().length > 0 &&
    srcmac.trim().toLowerCase() !== 'none'
  ) {
    return srcmac.trim().toLowerCase()
  }

  const deviceName = suggestion.evidenceBundle.device.device_name
  if (typeof deviceName === 'string' && deviceName.trim().length > 0) {
    return deviceName.trim().toLowerCase()
  }

  return suggestion.context.srcDeviceKey.trim().toLowerCase()
}

function incidentKeyForSuggestion(suggestion: SuggestionRecord) {
  return [
    suggestion.ruleId.trim().toLowerCase(),
    suggestion.scope,
    suggestion.context.service.trim().toLowerCase(),
    canonicalDeviceIdentity(suggestion),
  ].join('::')
}

function incidentInferenceSummary(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
) {
  const hypothesis = suggestion.hypotheses[0]
  if (hypothesis) {
    return hypothesis
  }

  return locale === 'zh'
    ? '当前还没有额外假设文本，先顺着已附带的设备、拓扑和变更证据继续判断。'
    : 'There is no extra hypothesis text yet, so continue from the device, topology, and change evidence already attached.'
}

function incidentScopeMeaning(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
) {
  if (suggestion.scope === 'cluster') {
    return locale === 'zh'
      ? '这表示同一键值的重复告警已经跨过聚合门槛，问题不再是单次孤立波动。'
      : 'This means repeated same-key alerts crossed the cluster gate, so the issue is no longer an isolated one-off path.'
  }

  return locale === 'zh'
    ? '这表示证据目前仍然集中在一条服务/设备路径上，还没有扩展成重复模式事件。'
    : 'This means the evidence is still concentrated on one service/device path and has not widened into a repeated-pattern incident yet.'
}

function groupedRefreshLabel(
  mergedSuggestionCount: number,
  locale: 'en' | 'zh',
) {
  if (locale === 'zh') {
    return mergedSuggestionCount > 1
      ? `已合并 ${mergedSuggestionCount} 次建议刷新`
      : '当前只有 1 次建议刷新'
  }

  return mergedSuggestionCount > 1
    ? `merged ${mergedSuggestionCount} suggestion refreshes`
    : 'single suggestion refresh'
}

function historicalIncidentQueue(
  suggestions: SuggestionRecord[],
  locale: 'en' | 'zh',
): HistoricalIncidentEvent[] {
  const sortedSuggestions = suggestions
    .slice()
    .sort((left, right) => {
      const leftTs = parseTimestamp(left.suggestionTs)?.getTime() ?? 0
      const rightTs = parseTimestamp(right.suggestionTs)?.getTime() ?? 0
      return rightTs - leftTs
    })

  const groupedSuggestions = new Map<string, SuggestionRecord[]>()
  sortedSuggestions.forEach((suggestion) => {
    const key = incidentKeyForSuggestion(suggestion)
    const existing = groupedSuggestions.get(key)
    if (existing) {
      existing.push(suggestion)
      return
    }
    groupedSuggestions.set(key, [suggestion])
  })

  return Array.from(groupedSuggestions.entries()).map(([dedupeKey, bucket]) => {
    const latestSuggestion = bucket[0]
    const earliestSuggestion = bucket[bucket.length - 1]
    const deviceLabel = friendlyDeviceName(latestSuggestion)
    const refreshLabel = groupedRefreshLabel(bucket.length, locale)

    return {
      id: `incident-${dedupeKey}`,
      dedupeKey,
      stamp: latestSuggestion.suggestionTs,
      kind: 'suggestion' as const,
      title:
        locale === 'zh'
          ? `${deviceLabel} 的 ${latestSuggestion.context.service} 需要关注`
          : `${deviceLabel} needs attention on ${latestSuggestion.context.service}`,
      detail:
        locale === 'zh'
          ? `${refreshLabel}；${incidentScopeMeaning(latestSuggestion, locale)}`
          : `${refreshLabel}; ${incidentScopeMeaning(latestSuggestion, locale)}`,
      scope: latestSuggestion.scope,
      relatedAlertId: latestSuggestion.alertId,
      relatedSuggestionId: latestSuggestion.id,
      service: latestSuggestion.context.service,
      device: deviceLabel,
      provider: latestSuggestion.context.provider,
      actionCount: String(latestSuggestion.recommendedActions.length),
      hypothesisCount: String(latestSuggestion.hypotheses.length),
      evidence: evidenceKinds(latestSuggestion).join(' + '),
      mergedSuggestionCount: bucket.length,
      firstSeenTs: earliestSuggestion.suggestionTs,
      lastSeenTs: latestSuggestion.suggestionTs,
      inference: incidentInferenceSummary(latestSuggestion, locale),
      recommendation: primaryRecommendation(latestSuggestion),
      scopeMeaning: incidentScopeMeaning(latestSuggestion, locale),
    }
  })
}

function phasesForGuidedStage(
  lifecycle: LifecyclePhase[],
  selectedGuidedStage: GuidedStage,
) {
  return lifecycle.filter((phase) =>
    phase.stageIds.some((stageId) => selectedGuidedStage.phaseIds.includes(stageId)),
  )
}

function stageNodeTitle(
  snapshot: ControlLoopSnapshot,
  stageId: string,
  locale: 'en' | 'zh',
) {
  const matched = snapshot.stageNodes.find((node) => node.id === stageId)
  if (matched) {
    return matched.title
  }
  if (stageId === 'cluster-window') {
    return locale === 'zh' ? '聚合窗口' : 'Cluster window'
  }
  return stageId
}

function stageNodeSubtitle(
  snapshot: ControlLoopSnapshot,
  stageId: string,
  locale: 'en' | 'zh',
) {
  const matched = snapshot.stageNodes.find((node) => node.id === stageId)
  if (matched) {
    return matched.subtitle
  }
  if (stageId === 'cluster-window') {
    return locale === 'zh'
      ? '相同键值重复统计'
      : 'same-key aggregation window'
  }
  return locale === 'zh' ? '运行阶段' : 'control-loop stage'
}

function stageNodeMetrics(
  snapshot: ControlLoopSnapshot,
  stageId: string,
  locale: 'en' | 'zh',
  phase?: LifecyclePhase,
) {
  const matched = snapshot.stageNodes.find((node) => node.id === stageId)
  if (matched) {
    return matched.metrics
  }

  if (stageId === 'cluster-window') {
    return [
      {
        label: locale === 'zh' ? '门槛' : 'gate',
        value: phase?.band.value ?? 'n/a',
      },
      {
        label: locale === 'zh' ? '说明' : 'detail',
        value: phase?.band.detail ?? (locale === 'zh' ? '聚合窗口' : 'aggregation window'),
      },
    ]
  }

  return []
}

function stageLinkStateFromStatus(status: StageNode['status']): StageLink['state'] {
  if (status === 'planned') {
    return 'planned'
  }

  return status === 'steady' || status === 'watch' ? 'steady' : 'active'
}

function buildStageProcessGraph(
  snapshot: ControlLoopSnapshot,
  selectedStagePhases: LifecyclePhase[],
  locale: 'en' | 'zh',
) {
  const orderedStageIds = Array.from(
    new Set(selectedStagePhases.flatMap((phase) => phase.stageIds)),
  )

  const graphNodes: StageNode[] = orderedStageIds.map((stageId, index) => {
    const phase = selectedStagePhases.find((item) => item.stageIds.includes(stageId))
    const matched = snapshot.stageNodes.find((node) => node.id === stageId)

    return {
      id: stageId,
      title: stageNodeTitle(snapshot, stageId, locale),
      subtitle:
        matched?.subtitle ??
        phase?.title ??
        stageNodeSubtitle(snapshot, stageId, locale),
      status: matched?.status ?? phase?.status ?? 'steady',
      x: index * 290,
      y: index % 2 === 0 ? 64 : 136,
      metrics: stageNodeMetrics(snapshot, stageId, locale, phase).slice(0, 3),
    }
  })

  const graphLinks: StageLink[] = orderedStageIds.slice(0, -1).map((stageId, index) => {
    const target = orderedStageIds[index + 1]
    const targetNode = graphNodes.find((node) => node.id === target)

    return {
      id: `selected-stage-link-${stageId}-${target}`,
      source: stageId,
      target,
      state: stageLinkStateFromStatus(targetNode?.status ?? 'steady'),
    }
  })

  return {
    nodes: graphNodes,
    links: graphLinks,
  }
}

function reconstructedTimeline(
  linkedSuggestion: SuggestionRecord,
  snapshot: ControlLoopSnapshot,
  lifecycle: LifecyclePhase[],
) {
  const existingTimeline =
    linkedSuggestion.timeline && linkedSuggestion.timeline.length > 0
      ? linkedSuggestion.timeline
      : snapshot.timeline

  if (existingTimeline.length > 0) {
    return existingTimeline
  }

  return lifecycle.map((phase) => ({
    id: `reconstructed-${phase.id}`,
    stageId: phase.stageIds[0],
    stamp:
      phase.band.stamp ??
      linkedSuggestion.suggestionTs ??
      snapshot.controlLoop.latestSuggestionTs,
    title: phase.title,
    detail: `${phase.purpose} ${phase.facts.join(' · ')}`.trim(),
    durationMs: phase.band.durationMs,
  }))
}

function currentDayVolume(snapshot: ControlLoopSnapshot) {
  const metric = snapshot.overviewMetrics.find(
    (item) => item.id === 'current-day-volume',
  )
  if (!metric) {
    return null
  }

  const match = metric.value.match(/(\d+)\s*\/\s*(\d+)/)
  if (!match) {
    return null
  }

  return {
    alerts: Number.parseInt(match[1], 10),
    suggestions: Number.parseInt(match[2], 10),
  }
}

function lifecycleIntegrityIssues(
  snapshot: ControlLoopSnapshot,
  linkedSuggestion: SuggestionRecord,
  connectionState: ControlLoopConnectionState,
  transportIssue?: string | null,
): LifecycleIntegrityIssue[] {
  const issues: LifecycleIntegrityIssue[] = []
  if (transportIssue) {
    issues.push({
      id: 'transport-issue',
      detail: transportIssue,
      severity: 'hard',
    })
  }
  const missingTimeline = (linkedSuggestion.timeline?.length ?? 0) === 0
  const missingTelemetry = (linkedSuggestion.stageTelemetry?.length ?? 0) === 0

  if (
    connectionState !== 'fallback' &&
    connectionState !== 'connecting' &&
    (missingTimeline || missingTelemetry)
  ) {
    issues.push({
      id: 'missing-telemetry',
      detail:
        'Live snapshot for the active suggestion is missing timeline/stageTelemetry, so lifecycle timing cannot be trusted.',
      severity: 'hard',
    })
  }

  const latestAlert = parseTimestamp(snapshot.controlLoop.latestAlertTs)
  const latestSuggestion = parseTimestamp(snapshot.controlLoop.latestSuggestionTs)
  if (latestAlert && latestSuggestion) {
    const skewMs = latestSuggestion.getTime() - latestAlert.getTime()
    if (skewMs > 15 * 60_000) {
      issues.push({
        id: 'stream-skew',
        detail: `Latest suggestion timestamp leads the latest alert timestamp by ${formatDurationMs(skewMs)}.`,
        severity: 'soft',
      })
    }
  }

  const volume = currentDayVolume(snapshot)
  if (volume && volume.alerts === 0 && volume.suggestions > 0) {
    issues.push({
      id: 'volume-skew',
      detail: `Current-day volume is skewed: ${volume.alerts} alerts / ${volume.suggestions} suggestions.`,
      severity: 'soft',
    })
  }

  return issues
}

function primaryRecommendation(suggestion: SuggestionRecord) {
  return (
    suggestion.recommendedActions[0] ??
    'Inspect the evidence bundle before widening operator action.'
  )
}

function friendlyDeviceName(suggestion: SuggestionRecord) {
  const deviceName = suggestion.evidenceBundle.device.device_name
  if (typeof deviceName === 'string' && deviceName.trim().length > 0) {
    return deviceName.trim()
  }
  return suggestion.context.srcDeviceKey
}

function projectorDeviceName(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
) {
  const rawDeviceName = suggestion.evidenceBundle.device.device_name
  if (typeof rawDeviceName === 'string' && rawDeviceName.trim().length > 0) {
    return rawDeviceName.trim()
  }

  const vendor = suggestion.evidenceBundle.device.vendor
  const family = suggestion.evidenceBundle.device.family
  const role = suggestion.evidenceBundle.device.device_role
  const vendorText = typeof vendor === 'string' ? vendor.trim() : ''
  const familyText = typeof family === 'string' ? family.trim() : ''
  const roleText = typeof role === 'string' ? role.trim() : ''

  if (vendorText && familyText) {
    return `${vendorText} ${familyText}`
  }

  if (familyText) {
    return familyText
  }

  if (roleText) {
    return roleText
  }

  const fallback = suggestion.context.srcDeviceKey.trim()
  if (fallback && !isIpLike(fallback) && !isMacLike(fallback)) {
    return fallback
  }

  return locale === 'zh' ? '未命名设备' : 'Unlabeled Device'
}

function preferredIdentityLabel(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
) {
  const topology = suggestion.evidenceBundle.topology
  const device = suggestion.evidenceBundle.device
  const candidates = [
    topology.srcip,
    device.srcmac,
    suggestion.context.srcDeviceKey,
  ]

  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim().length > 0) {
      return candidate.trim()
    }
  }

  return locale === 'zh' ? '设备身份待补齐' : 'device identity pending'
}

function projectorRouteLine(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
) {
  return `${suggestion.context.service} / ${preferredIdentityLabel(suggestion, locale)}`
}

function projectorEvidenceState(
  facts: ProjectorFact[],
  locale: 'en' | 'zh',
): ProjectorEvidenceState {
  const emptyTokens =
    locale === 'zh'
      ? new Set(['未附带', '设备身份待补齐', 'unknown', 'n/a'])
      : new Set(['not attached', 'device identity pending', 'unknown', 'n/a'])
  const meaningfulCount = facts.filter((fact) => {
    const value = fact.value.trim().toLowerCase()
    return value.length > 0 && !emptyTokens.has(value)
  }).length

  if (meaningfulCount >= 3) {
    return 'rich'
  }
  if (meaningfulCount >= 1) {
    return 'partial'
  }
  return 'none'
}

function prettyRuleName(ruleId: string) {
  return ruleId
    .replace(/_v\d+$/i, '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

function normalizedRuleId(ruleId: string) {
  return ruleId.replace(/_v\d+$/i, '').trim().toLowerCase()
}

function analysisModeLabel(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
) {
  const provider = suggestion.context.provider.trim().toLowerCase() || 'template'
  if (locale === 'zh') {
    return provider === 'template'
      ? '规则告警 + 模板建议'
      : `规则告警 + ${provider} 建议`
  }

  return provider === 'template'
    ? 'deterministic alert + template suggestion'
    : `deterministic alert + ${provider} suggestion`
}

function incidentHeadline(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
) {
  const rule = normalizedRuleId(suggestion.ruleId)
  const isCluster = suggestion.scope === 'cluster'

  if (rule === 'deny_burst') {
    if (locale === 'zh') {
      return isCluster ? '重复 deny 模式' : 'deny 阈值命中'
    }
    return isCluster ? 'Repeated deny pattern' : 'Deny threshold hit'
  }

  if (rule === 'bytes_spike') {
    if (locale === 'zh') {
      return isCluster ? '重复流量峰值' : '流量峰值命中'
    }
    return isCluster ? 'Repeated traffic spike' : 'Traffic spike hit'
  }

  if (locale === 'zh') {
    return isCluster ? '重复规则模式' : '规则阈值命中'
  }
  return isCluster ? 'Repeated rule pattern' : 'Rule threshold hit'
}

function storyScopeLabel(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
) {
  if (locale === 'zh') {
    return suggestion.scope === 'cluster' ? '重复模式事件' : '单路径事件'
  }
  return suggestion.scope === 'cluster' ? 'repeated-pattern incident' : 'single-path incident'
}

function storyCopy(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
): StoryCopy {
  const deviceName = projectorDeviceName(suggestion, locale)
  const scopeLabel = storyScopeLabel(suggestion, locale)
  const ruleLabel = prettyRuleName(suggestion.ruleId)
  const modeLabel = analysisModeLabel(suggestion, locale)
  const recentSimilar = suggestion.context.recentSimilar1h
  const routeLine = projectorRouteLine(suggestion, locale)

  if (locale === 'zh') {
    return {
      eyebrow: '当前运行事件',
      headlinePrimary: incidentHeadline(suggestion, locale),
      headlineDevice: deviceName,
      headlineRoute: routeLine,
      summary: `${deviceName} 的 ${suggestion.context.service} 刚被系统锁定为当前主路径。现在看到的是 ${scopeLabel}，分析方式是 ${modeLabel}。`,
      whatHappenedLabel: '发生了什么',
      whatHappened: `${ruleLabel} 在 ${deviceName} 上触发，当前焦点是 ${suggestion.context.service} 这条通信路径。`,
      whyLabel: '为什么重要',
      why: recentSimilar > 0
        ? `过去 1 小时内还有 ${recentSimilar} 次相似告警，说明这不是一次孤立波动。`
        : `当前建议来自 ${modeLabel}，并且已经附带设备、拓扑和变化上下文，可以直接进入判断。`,
      nextLabel: '下一步该做什么',
      next: primaryRecommendation(suggestion),
    }
  }

  return {
    eyebrow: 'Live incident',
    headlinePrimary: incidentHeadline(suggestion, locale),
    headlineDevice: deviceName,
    headlineRoute: routeLine,
    summary: `${deviceName} is the current focus on ${suggestion.context.service}. This slice is a ${scopeLabel} running in ${modeLabel}.`,
    whatHappenedLabel: 'What happened',
    whatHappened: `${ruleLabel} fired on ${deviceName}. The current slice is the ${suggestion.context.service} traffic path.`,
    whyLabel: 'Why it matters',
    why: recentSimilar > 0
      ? `${recentSimilar} similar alerts were seen in the last hour, so this is no longer a one-off spike.`
      : `The current recommendation comes from ${modeLabel}, and device, topology, and change context are already attached for review.`,
    nextLabel: 'What to do next',
    next: primaryRecommendation(suggestion),
  }
}

function primaryHypothesisEntry(suggestion: SuggestionRecord) {
  const items = suggestion.hypothesisSet?.items ?? []
  return (
    items.find((item) => item.hypothesisId === suggestion.hypothesisSet.primaryHypothesisId) ??
    items[0] ??
    null
  )
}

function reviewDispositionLabel(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
) {
  const disposition = suggestion.reviewVerdict?.recommendedDisposition ?? ''
  const mapping: Record<string, { en: string; zh: string }> = {
    return_to_evidence_gather: {
      en: 'return to evidence gather',
      zh: '回到补证阶段',
    },
    project_with_operator_boundary: {
      en: 'project with operator boundary',
      zh: '投影到人工边界',
    },
    ready_for_projection: {
      en: 'ready for projection',
      zh: '可进入建议投影',
    },
  }

  return mapping[disposition]?.[locale] ?? (locale === 'zh' ? '审查已附带' : 'review attached')
}

function evidenceKinds(suggestion: SuggestionRecord) {
  return [
    Object.keys(suggestion.evidenceBundle.topology).length > 0 ? 'topology' : null,
    Object.keys(suggestion.evidenceBundle.device).length > 0 ? 'device' : null,
    Object.keys(suggestion.evidenceBundle.change).length > 0 ? 'change' : null,
    Object.keys(suggestion.evidenceBundle.historical).length > 0 ? 'historical' : null,
  ].filter((item): item is string => item !== null)
}

function compactEvidenceValue(
  value: string | number | boolean | string[] | null | undefined,
  locale: 'en' | 'zh',
) {
  if (Array.isArray(value)) {
    const normalized = value.map((item) => item?.trim()).filter(Boolean)
    return normalized.length > 0
      ? normalized.slice(0, 2).join(' · ')
      : locale === 'zh'
        ? '未附带'
        : 'not attached'
  }

  if (typeof value === 'boolean') {
    return locale === 'zh' ? (value ? '已附带' : '未附带') : value ? 'attached' : 'not attached'
  }

  if (typeof value === 'number') {
    return `${value}`
  }

  if (typeof value === 'string' && value.trim().length > 0) {
    return value.trim()
  }

  return locale === 'zh' ? '未附带' : 'not attached'
}

function firstCompactRecordValue<
  TValue extends string | number | boolean | string[] | null | undefined,
>(
  record: Record<string, TValue>,
  keys: string[],
  locale: 'en' | 'zh',
) {
  for (const key of keys) {
    const value = record[key]
    if (
      value !== undefined &&
      value !== null &&
      (!(typeof value === 'string') || value.trim().length > 0)
    ) {
      return compactEvidenceValue(value, locale)
    }
  }

  return locale === 'zh' ? '未附带' : 'not attached'
}

function buildProjectorStations(
  suggestion: SuggestionRecord,
  locale: 'en' | 'zh',
  clusterGateValue: string,
) {
  const topology = suggestion.evidenceBundle.topology
  const device = suggestion.evidenceBundle.device
  const change = suggestion.evidenceBundle.change
  const projectionBasis = suggestion.projectionBasis ?? {}
  const primaryHypothesis = primaryHypothesisEntry(suggestion)
  const reviewVerdict = suggestion.reviewVerdict
  const ruleLabel = prettyRuleName(suggestion.ruleId)
  const deviceLabel = friendlyDeviceName(suggestion)
  const hypothesis =
    primaryHypothesis?.statement ??
    suggestion.hypotheses[0] ??
    (locale === 'zh'
      ? '当前没有单独假设，继续沿着附带证据检查。'
      : 'No standalone hypothesis text is attached yet.')
  const pathToken = [
    firstCompactRecordValue(topology, ['srcip', 'src_device_key'], locale),
    firstCompactRecordValue(topology, ['dstip', 'neighbor_refs', 'zone'], locale),
  ].join(' -> ')
  const triggerFacts = [
    {
      label: locale === 'zh' ? '当前服务' : 'service',
      value: suggestion.context.service,
    },
    {
      label: locale === 'zh' ? '设备名' : 'device name',
      value: projectorDeviceName(suggestion, locale),
    },
    {
      label: locale === 'zh' ? '事件身份' : 'event identity',
      value: preferredIdentityLabel(suggestion, locale),
    },
  ]
  const aggregateFacts = [
    {
      label: locale === 'zh' ? '当前判断' : 'current posture',
      value: storyScopeLabel(suggestion, locale),
    },
    {
      label: locale === 'zh' ? '聚合门槛' : 'cluster gate',
      value: clusterGateValue,
    },
    {
      label: locale === 'zh' ? '相似事件' : 'recent similar',
      value: `${suggestion.context.recentSimilar1h}`,
    },
  ]
  const pathFacts = [
    {
      label: locale === 'zh' ? '源 IP/MAC' : 'source identity',
      value: firstCompactRecordValue(topology, ['srcip', 'src_device_key'], locale),
    },
    {
      label: locale === 'zh' ? '服务' : 'service',
      value: suggestion.context.service,
    },
    {
      label: locale === 'zh' ? '目标路径' : 'destination',
      value: firstCompactRecordValue(topology, ['dstip', 'neighbor_refs', 'zone'], locale),
    },
  ]
  const deviceFacts = [
    {
      label: locale === 'zh' ? '设备名' : 'device name',
      value: projectorDeviceName(suggestion, locale),
    },
    {
      label: locale === 'zh' ? '设备画像' : 'fingerprint',
      value: firstCompactRecordValue(device, ['device_role', 'vendor', 'family'], locale),
    },
    {
      label: locale === 'zh' ? '变更线索' : 'change clue',
      value: firstCompactRecordValue(change, ['change_refs', 'level', 'suspected_change'], locale),
    },
  ]
  const inferenceFacts = [
    {
      label: locale === 'zh' ? '优先级' : 'priority',
      value: suggestion.priority.toUpperCase(),
    },
    {
      label: locale === 'zh' ? '把握度' : 'confidence',
      value: suggestion.confidenceLabel,
    },
    {
      label: locale === 'zh' ? '假设状态' : 'hypothesis state',
      value: primaryHypothesis?.reviewState ?? (locale === 'zh' ? '候选' : 'candidate'),
    },
  ]
  const actionFacts = [
    {
      label: locale === 'zh' ? '建议来源' : 'provider',
      value: compactEvidenceValue(suggestion.context.provider, locale),
    },
    {
      label: locale === 'zh' ? '当前服务' : 'service',
      value: suggestion.context.service,
    },
    {
      label: locale === 'zh' ? '审查结论' : 'review verdict',
      value: reviewVerdict?.verdictStatus ?? (locale === 'zh' ? '人工审查' : 'operator review'),
    },
    {
      label: locale === 'zh' ? '审批要求' : 'approval',
      value:
        reviewVerdict?.approvalRequired
          ? locale === 'zh'
            ? '需要'
            : 'required'
          : locale === 'zh'
            ? '无需'
            : 'not required',
    },
    {
      label: locale === 'zh' ? '动作类型' : 'action type',
      value: reviewDispositionLabel(suggestion, locale),
    },
  ]

  return [
    {
      id: 'projector-trigger',
      title: locale === 'zh' ? '触发' : 'Trigger',
      token: formatMaybeTimestamp(suggestion.suggestionTs, 'time'),
      caption:
        locale === 'zh'
          ? '系统已锁定目标路径'
          : 'service lane locked',
      detail:
        locale === 'zh'
          ? `${ruleLabel} 刚把 ${deviceLabel} 的 ${suggestion.context.service} 标成当前主事件，后面所有节点都会围绕这条路径继续解释。`
          : `${ruleLabel} just marked ${suggestion.context.service} on ${deviceLabel} as the current incident, and the rest of the chain now explains that path.`,
      facts: triggerFacts,
      sources: projectionBasis['projector-trigger'] ?? [],
      guidedStageId: 'guided-source',
      evidenceState: projectorEvidenceState(triggerFacts, locale),
    },
    {
      id: 'projector-aggregate',
      title: locale === 'zh' ? '聚合' : 'Aggregate',
      token: clusterGateValue,
      caption: locale === 'zh' ? '系统判断是否已成重复模式' : 'repeat-pattern check',
      detail:
        locale === 'zh'
          ? suggestion.scope === 'cluster'
            ? '相同键值已经跨过聚合门槛，所以系统不再把它当成单次抖动，而是当成重复模式事件。'
            : '证据暂时还集中在这一条路径上，系统正在观察它会不会继续累积成重复模式。'
          : suggestion.scope === 'cluster'
          ? 'The same-key evidence crossed the cluster gate, so the system now treats it as a repeated pattern instead of a one-off spike.'
          : 'The evidence is still concentrated on one path, so the system is watching whether it grows into a repeated pattern.',
      facts: aggregateFacts,
      sources: projectionBasis['projector-aggregate'] ?? [],
      guidedStageId:
        suggestion.scope === 'cluster' ? 'guided-cluster' : 'guided-alert',
      evidenceState: projectorEvidenceState(aggregateFacts, locale),
    },
    {
      id: 'projector-path',
      title: locale === 'zh' ? '路径' : 'Path',
      token: compactEvidenceValue(topology.zone, locale),
      caption:
        locale === 'zh'
          ? '当前主路径'
          : 'main traffic lane',
      detail:
        locale === 'zh'
          ? `现在优先看的就是 ${pathToken} 这条路线，它告诉我们问题是在什么路径上被观察到的。`
          : `The current review stays centered on ${pathToken}, which tells us where the issue is being observed.`,
      facts: pathFacts,
      sources: projectionBasis['projector-path'] ?? [],
      guidedStageId: 'guided-source',
      evidenceState: projectorEvidenceState(pathFacts, locale),
    },
    {
      id: 'projector-device',
      title: locale === 'zh' ? '设备/变更' : 'Device / Change',
      token: firstCompactRecordValue(change, ['level', 'suspected_change'], locale),
      caption:
        locale === 'zh'
          ? '设备身份与变更线索'
          : 'identity and change clues',
      detail:
        locale === 'zh'
          ? `系统现在用 ${deviceLabel} 作为主设备身份，同时结合变更线索判断这更像偶发噪声，还是设备姿态真的发生了变化。`
          : `The system now uses ${deviceLabel} as the main identity anchor and combines it with change clues to decide whether this is noise or a real posture shift.`,
      facts: deviceFacts,
      sources: projectionBasis['projector-device'] ?? [],
      guidedStageId: 'guided-alert',
      evidenceState: projectorEvidenceState(deviceFacts, locale),
    },
    {
      id: 'projector-inference',
      title: locale === 'zh' ? '推断' : 'Inference',
      token: suggestion.confidenceLabel,
      caption:
        locale === 'zh'
          ? '当前主假设'
          : 'primary hypothesis',
      detail: hypothesis,
      facts: inferenceFacts,
      sources: projectionBasis['projector-inference'] ?? [],
      guidedStageId: 'guided-suggestion',
      evidenceState: projectorEvidenceState(inferenceFacts, locale),
    },
    {
      id: 'projector-action',
      title: locale === 'zh' ? '动作' : 'Action',
      token: compactEvidenceValue(suggestion.context.provider, locale),
      caption:
        locale === 'zh'
          ? '审查后落点'
          : 'review disposition',
      detail:
        reviewVerdict?.blockingIssues?.[0] ??
        reviewVerdict?.reviewSummary ??
        primaryRecommendation(suggestion),
      facts: actionFacts,
      sources: projectionBasis['projector-action'] ?? [],
      guidedStageId: 'guided-operator',
      evidenceState: projectorEvidenceState(actionFacts, locale),
    },
  ] satisfies ProjectorStation[]
}

function projectorTimingMeasurement(
  lifecycle: LifecyclePhase[],
  suggestion: SuggestionRecord,
  timeline: ControlLoopSnapshot['timeline'],
): ProjectorTimingMeasurement {
  const telemetry = suggestion.stageTelemetry ?? []
  const startedAtValues = telemetry
    .map((item) => parseTimestamp(item.startedAt ?? item.endedAt))
    .filter((stamp): stamp is Date => Boolean(stamp))
  const endedAtValues = telemetry
    .map((item) => parseTimestamp(item.endedAt ?? item.startedAt))
    .filter((stamp): stamp is Date => Boolean(stamp))

  if (startedAtValues.length > 0 && endedAtValues.length > 0) {
    const startedAtMs = Math.min(...startedAtValues.map((stamp) => stamp.getTime()))
    const endedAtMs = Math.max(...endedAtValues.map((stamp) => stamp.getTime()))
    const measuredMs = Math.max(0, endedAtMs - startedAtMs)

    if (measuredMs > 0 && measuredMs <= 10 * 60_000) {
      return {
        durationMs: measuredMs,
        source: 'telemetry-window',
      }
    }
  }

  const summedDurationMs = lifecycle.reduce((total, phase) => {
    const durationMs = phase.band.durationMs
    if (!durationMs || durationMs <= 0 || durationMs > 5 * 60_000) {
      return total
    }
    return total + durationMs
  }, 0)

  if (summedDurationMs > 0 && summedDurationMs <= 10 * 60_000) {
    return {
      durationMs: summedDurationMs,
      source: 'phase-sum',
    }
  }

  const timelineStamps = timeline
    .map((step) => parseTimestamp(step.stamp))
    .filter((stamp): stamp is Date => Boolean(stamp))

  if (timelineStamps.length > 1) {
    const startedAtMs = Math.min(...timelineStamps.map((stamp) => stamp.getTime()))
    const endedAtMs = Math.max(...timelineStamps.map((stamp) => stamp.getTime()))
    const measuredMs = Math.max(0, endedAtMs - startedAtMs)

    if (measuredMs > 0 && measuredMs <= 10 * 60_000) {
      return {
        durationMs: measuredMs,
        source: 'timeline-window',
      }
    }
  }

  const summedTimelineDurationMs = timeline.reduce((total, step) => {
    const durationMs = step.durationMs
    if (!durationMs || durationMs <= 0 || durationMs > 5 * 60_000) {
      return total
    }
    return total + durationMs
  }, 0)

  if (summedTimelineDurationMs > 0 && summedTimelineDurationMs <= 10 * 60_000) {
    return {
      durationMs: summedTimelineDurationMs,
      source: 'timeline-sum',
    }
  }

  return {
    durationMs: null,
    source: 'demo-clock',
  }
}

function projectorTimingSourceLabel(
  source: ProjectorTimingSource,
  locale: 'en' | 'zh',
) {
  const mapping: Record<ProjectorTimingSource, { en: string; zh: string }> = {
    'telemetry-window': {
      en: 'stage telemetry window',
      zh: '阶段时序窗口',
    },
    'phase-sum': {
      en: 'phase duration sum',
      zh: '阶段耗时求和',
    },
    'timeline-window': {
      en: 'timeline timestamp window',
      zh: '时间线时间窗',
    },
    'timeline-sum': {
      en: 'timeline duration sum',
      zh: '时间线耗时求和',
    },
    'demo-clock': {
      en: 'demo playback clock',
      zh: '演示回放时钟',
    },
  }

  return mapping[source][locale]
}

function projectorPlaybackStepMs(
  stations: ProjectorStation[],
  suggestion: SuggestionRecord,
) {
  return stations.map((station) => {
    switch (station.id) {
      case 'projector-trigger':
        return 520
      case 'projector-aggregate':
        return suggestion.scope === 'cluster' ? 620 : 520
      case 'projector-path':
        return 520
      case 'projector-device':
        return 520
      case 'projector-inference':
        return suggestion.scope === 'cluster' ? 500 : 600
      case 'projector-action':
        return 520
      default:
        return PROJECTOR_CLOCK_DURATION_MS / Math.max(stations.length, 1)
    }
  })
}

function projectorMeasuredStepMs(
  stations: ProjectorStation[],
  suggestion: SuggestionRecord,
) {
  const telemetryLookup = new Map(
    (suggestion.stageTelemetry ?? []).map((item) => [item.stageId, item]),
  )
  const timelineLookup = new Map(
    (suggestion.timeline ?? [])
      .filter((item): item is typeof item & { stageId: string } => Boolean(item.stageId))
      .map((item) => [item.stageId, item]),
  )
  const sourceTs =
    telemetryLookup.get('raw-topic')?.endedAt ??
    telemetryLookup.get('ingest')?.endedAt ??
    telemetryLookup.get('lcore-source')?.endedAt
  const handoffTs =
    telemetryLookup.get('raw-topic')?.endedAt ??
    telemetryLookup.get('forwarder')?.endedAt ??
    telemetryLookup.get('ingest')?.endedAt
  const alertTs =
    telemetryLookup.get('alerts-topic')?.endedAt ??
    telemetryLookup.get('correlator')?.endedAt
  const suggestionTs =
    telemetryLookup.get('suggestions-topic')?.endedAt ??
    telemetryLookup.get('aiops-agent')?.endedAt

  return stations.map((station) => {
    switch (station.id) {
      case 'projector-trigger':
        return measuredDurationWindowMs(sourceTs, handoffTs)
      case 'projector-aggregate':
        return (
          positiveDurationMs(telemetryLookup.get('cluster-window')?.durationMs) ??
          positiveDurationMs(telemetryLookup.get('correlator')?.durationMs) ??
          positiveDurationMs(timelineLookup.get('correlator')?.durationMs) ??
          measuredDurationWindowMs(handoffTs ?? sourceTs, alertTs)
        )
      case 'projector-path':
        return positiveDurationMs(timelineLookup.get('alerts-topic')?.durationMs)
      case 'projector-device':
        return null
      case 'projector-inference':
        return (
          positiveDurationMs(telemetryLookup.get('aiops-agent')?.durationMs) ??
          positiveDurationMs(timelineLookup.get('suggestions-topic')?.durationMs) ??
          measuredDurationWindowMs(alertTs, suggestionTs)
        )
      case 'projector-action':
        return positiveDurationMs(telemetryLookup.get('remediation')?.durationMs)
      default:
        return null
    }
  })
}

function playbackIndexForElapsed(
  stepDurations: number[],
  elapsedMs: number,
) {
  let consumedMs = 0
  for (let index = 0; index < stepDurations.length; index += 1) {
    consumedMs += stepDurations[index]
    if (elapsedMs < consumedMs) {
      return index
    }
  }
  return Math.max(0, stepDurations.length - 1)
}

function buildGuidedStages(lifecycle: LifecyclePhase[]): GuidedStage[] {
  const [source, handoff, alert, cluster, suggestion, remediation] = lifecycle

  return [
    {
      id: 'guided-source',
      title: 'Source Signal',
      summary: 'Signal entered the platform and was handed to the raw path.',
      value: handoff.band.value,
      detail: handoff.band.detail,
      tone: 'raw',
      status:
        handoff.status === 'flowing' || source.status === 'flowing'
          ? 'flowing'
          : source.status,
      phaseIds: [...source.stageIds, ...handoff.stageIds],
      facts: [source.facts[1], handoff.facts[0]],
      stamp: handoff.band.stamp ?? source.band.stamp,
      durationMs: handoff.band.durationMs,
    },
    {
      id: 'guided-alert',
      title: 'Deterministic Alert',
      summary: alert.purpose,
      value: alert.band.value,
      detail: alert.band.detail,
      tone: 'alert',
      status: alert.status,
      phaseIds: alert.stageIds,
      facts: alert.facts,
      stamp: alert.band.stamp,
      durationMs: alert.band.durationMs,
    },
    {
      id: 'guided-cluster',
      title: 'Cluster Gate',
      summary: cluster.purpose,
      value: cluster.band.value,
      detail: cluster.band.detail,
      tone: 'alert',
      status: cluster.status,
      phaseIds: cluster.stageIds,
      facts: cluster.facts,
      stamp: cluster.band.stamp,
      durationMs: cluster.band.durationMs,
    },
    {
      id: 'guided-suggestion',
      title: 'AIOps Suggestion',
      summary: suggestion.purpose,
      value: suggestion.band.value,
      detail: suggestion.band.detail,
      tone: 'suggestion',
      status: suggestion.status,
      phaseIds: suggestion.stageIds,
      facts: suggestion.facts,
      stamp: suggestion.band.stamp,
      durationMs: suggestion.band.durationMs,
    },
    {
      id: 'guided-operator',
      title: 'Operator Action',
      summary: 'Approval and execution stay available as the next control boundary.',
      value: remediation.band.value,
      detail: remediation.band.detail,
      tone: 'neutral',
      status: remediation.status,
      phaseIds: remediation.stageIds,
      facts: remediation.facts,
      stamp: remediation.band.stamp,
      durationMs: remediation.band.durationMs,
    },
  ]
}

function guidedStageIdForPhase(phaseId: string) {
  if (phaseId === 'source-signal' || phaseId === 'edge-handoff') {
    return 'guided-source'
  }
  if (phaseId === 'deterministic-alert') {
    return 'guided-alert'
  }
  if (phaseId === 'cluster-gate') {
    return 'guided-cluster'
  }
  if (phaseId === 'suggestion-emission') {
    return 'guided-suggestion'
  }
  return 'guided-operator'
}

function localizedStageTitle(stageId: string, locale: 'en' | 'zh') {
  const mapping: Record<
    string,
    {
      en: string
      zh: string
    }
  > = {
    'guided-source': { en: 'Input Received', zh: '信号进入' },
    'guided-alert': { en: 'Rule Threshold Hit', zh: '规则触发' },
    'guided-cluster': { en: 'Pattern Check', zh: '模式检查' },
    'guided-suggestion': { en: 'Suggestion Ready', zh: '建议生成' },
    'guided-operator': { en: 'Human Review', zh: '人工处置' },
  }

  return mapping[stageId]?.[locale] ?? stageId
}

function controlLoopMetricValue(snapshot: ControlLoopSnapshot, metricId: string) {
  return snapshot.overviewMetrics.find((metric) => metric.id === metricId)?.value ?? 'n/a'
}

function projectorInspectorRole(
  stationId: ProjectorStation['id'],
  locale: 'en' | 'zh',
) {
  const roles: Record<ProjectorStation['id'], { en: string; zh: string }> = {
    'projector-trigger': {
      en: 'raw event lock',
      zh: '原始事件锁定',
    },
    'projector-aggregate': {
      en: 'repeat-pattern gate',
      zh: '重复模式门控',
    },
    'projector-path': {
      en: 'path concentration read',
      zh: '路径集中读取',
    },
    'projector-device': {
      en: 'identity + change check',
      zh: '身份与变更校验',
    },
    'projector-inference': {
      en: 'current reading',
      zh: '当前判断',
    },
    'projector-action': {
      en: 'operator next move',
      zh: '下一步动作',
    },
  }

  return roles[stationId][locale]
}

function projectorInspectorState(
  stationIndex: number,
  playbackIndex: number,
  playbackRunning: boolean,
  locale: 'en' | 'zh',
) {
  if (!playbackRunning) {
    return locale === 'zh' ? '已锁定检视' : 'focused review'
  }

  if (stationIndex < playbackIndex) {
    return locale === 'zh' ? '已通过' : 'completed'
  }

  if (stationIndex === playbackIndex) {
    return locale === 'zh' ? '当前节点' : 'active node'
  }

  return locale === 'zh' ? '后续节点' : 'upcoming node'
}

function projectorTransitionCopy(
  station: ProjectorStation,
  nextStation: ProjectorStation | undefined,
  locale: 'en' | 'zh',
) {
  if (!nextStation) {
    return locale === 'zh'
      ? '当前节点已经位于链路末端，后续进入人工确认与执行边界。'
      : 'This node sits at the end of the visible chain, so the next step is the operator boundary.'
  }

  const mapping: Record<ProjectorStation['id'], { en: string; zh: string }> = {
    'projector-trigger': {
      en: `The locked tuple is handed to ${nextStation.title.toLowerCase()} for repeat and spread checking.`,
      zh: `已锁定的触发元组会继续交给 ${nextStation.title} 节点判断是否形成重复或扩散。`,
    },
    'projector-aggregate': {
      en: `The current gate result is then projected into ${nextStation.title.toLowerCase()} to keep the route focus narrow.`,
      zh: `当前门控结果会继续投到 ${nextStation.title} 节点，用来收窄真正需要看的路径。`,
    },
    'projector-path': {
      en: `The route lock is then passed into ${nextStation.title.toLowerCase()} so identity and change clues can be attached.`,
      zh: `路径锁定结果会继续传到 ${nextStation.title} 节点，把设备身份和变更线索接上来。`,
    },
    'projector-device': {
      en: `Identity and change markers feed ${nextStation.title.toLowerCase()} so the reading can stay attached to evidence.`,
      zh: `设备身份和变更标记会继续送到 ${nextStation.title} 节点，让判断保持贴合证据。`,
    },
    'projector-inference': {
      en: `The current reading is then condensed into ${nextStation.title.toLowerCase()} for the next operator move.`,
      zh: `当前判断会继续收束到 ${nextStation.title} 节点，形成下一步动作。`,
    },
    'projector-action': {
      en: 'The current recommendation is ready for operator review.',
      zh: '当前建议已经进入人工确认边界。',
    },
  }

  return mapping[station.id][locale]
}

function projectorInspectorFields(
  station: ProjectorStation,
  locale: 'en' | 'zh',
) {
  return station.facts.map((fact) => ({
    label: fact.label,
    value: compactEvidenceValue(fact.value, locale),
  }))
}

export function LiveFlowConsole({
  connectionState,
  snapshot,
  latestDelta,
  selectedSuggestion,
  onSelectSuggestion,
  transportIssue,
  locale,
  heroStageRef,
}: LiveFlowConsoleProps) {
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)
  const [selectedLinkedSuggestionId, setSelectedLinkedSuggestionId] = useState<string | null>(null)
  const [pinnedEvent, setPinnedEvent] = useState<HistoricalIncidentEvent | null>(null)
  const [pinnedSuggestion, setPinnedSuggestion] = useState<SuggestionRecord | null>(null)
  const [selectedProjectorId, setSelectedProjectorId] = useState<string | null>(null)
  const [projectorTheme, setProjectorTheme] = useState<'calm' | 'alert'>('calm')
  const [projectorReplaySeed, setProjectorReplaySeed] = useState(0)
  const [projectorPlaybackElapsedMs, setProjectorPlaybackElapsedMs] = useState(0)
  const [projectorPlaybackIndex, setProjectorPlaybackIndex] = useState(0)
  const [projectorPlaybackRunning, setProjectorPlaybackRunning] = useState(false)
  const [expandedStageSelection, setExpandedStageSelection] = useState<{
    suggestionId: string
    stageId: string | null
  } | null>(null)
  const [isIncidentGraphExpanded, setIsIncidentGraphExpanded] = useState(false)

  const queueEvents = historicalIncidentQueue(snapshot.suggestions, locale)
  const activeEvent =
    queueEvents.find((event) => event.id === selectedEventId) ??
    pinnedEvent ??
    queueEvents.find(
      (event) => event.dedupeKey === incidentKeyForSuggestion(selectedSuggestion),
    ) ??
    queueEvents[0]
  const linkedSuggestion =
    snapshot.suggestions.find(
      (suggestion) => suggestion.id === selectedLinkedSuggestionId,
    ) ??
    pinnedSuggestion ??
    suggestionForEvent(
      activeEvent,
      snapshot.suggestions,
      selectedSuggestion,
    )
  const expandedStageId =
    expandedStageSelection?.suggestionId === linkedSuggestion.id
      ? expandedStageSelection.stageId
      : null
  const lifecycle = buildLifecyclePhases(snapshot, linkedSuggestion)
  const pulseKind = pulseKindForDelta(latestDelta?.kind ?? snapshot.feed[0]?.kind)
  const pulseTone = toneForDelta(latestDelta?.kind ?? snapshot.feed[0]?.kind)
  const currentPhase = currentPhaseId(latestDelta?.kind, activeEvent?.kind ?? pulseKind)
  const guidedStages = buildGuidedStages(lifecycle)
  const activeGuidedStageId = guidedStageIdForPhase(currentPhase)
  const selectedGuidedStage =
    guidedStages.find(
      (stage) => stage.id === (expandedStageId ?? activeGuidedStageId),
    ) ?? guidedStages[0]
  const selectedStagePhases = phasesForGuidedStage(lifecycle, selectedGuidedStage)
  const incidentProcessGraph = buildStageProcessGraph(
    snapshot,
    lifecycle,
    locale,
  )
  const incidentTimeline = reconstructedTimeline(linkedSuggestion, snapshot, lifecycle)
  const story = storyCopy(linkedSuggestion, locale)
  const [scrambledHeadlinePrimary, setScrambledHeadlinePrimary] = useState(
    story.headlinePrimary,
  )
  const [scrambledHeadlineDevice, setScrambledHeadlineDevice] = useState(
    story.headlineDevice,
  )
  const [scrambledHeadlineRoute, setScrambledHeadlineRoute] = useState(
    story.headlineRoute,
  )
  const hasMountedProjectorRef = useRef(false)
  const relatedTimelineSteps = incidentTimeline.filter(
    (step) => step.stageId && selectedGuidedStage.phaseIds.includes(step.stageId),
  )
  const integrityIssues = lifecycleIntegrityIssues(
    snapshot,
    linkedSuggestion,
    connectionState,
    transportIssue,
  )
  const hardIntegrityIssues = integrityIssues.filter(
    (issue) => issue.severity === 'hard',
  )
  const softIntegrityIssues = integrityIssues.filter(
    (issue) => issue.severity === 'soft',
  )
  const clusterGateValue =
    linkedSuggestion.stageTelemetry?.find(
      (item) => item.stageId === 'cluster-window',
    )?.value ??
    (linkedSuggestion.context.clusterWindowSec > 0
      ? `${linkedSuggestion.context.clusterSize}/${linkedSuggestion.context.clusterWindowSec}s`
      : locale === 'zh'
        ? '未达到自然聚合门槛'
        : 'cluster gate not naturally met yet')
  const selectedInference =
    activeEvent?.inference ?? incidentInferenceSummary(linkedSuggestion, locale)
  const selectedRecommendation =
    activeEvent?.recommendation ?? primaryRecommendation(linkedSuggestion)
  const selectedScopeMeaning =
    activeEvent?.scopeMeaning ?? incidentScopeMeaning(linkedSuggestion, locale)
  const selectedRefreshSummary = groupedRefreshLabel(
    activeEvent?.mergedSuggestionCount ?? 1,
    locale,
  )
  const selectedWindowSummary = activeEvent
    ? `${formatMaybeTimestamp(activeEvent.firstSeenTs, 'time')} - ${formatMaybeTimestamp(activeEvent.lastSeenTs, 'time')}`
    : `${formatMaybeTimestamp(linkedSuggestion.context.clusterFirstAlertTs, 'time')} - ${formatMaybeTimestamp(linkedSuggestion.context.clusterLastAlertTs, 'time')}`
  const incidentConvergenceModel = buildIncidentConvergenceModel({
    locale,
    queueEvents,
    activeEvent,
    linkedSuggestion,
    clusterGateValue,
    selectedInference,
    selectedRecommendation,
    selectedWindowSummary,
    selectedScopeMeaning,
    selectedRefreshSummary,
  })
  const projectorStations = useMemo(
    () =>
      buildProjectorStations(
        linkedSuggestion,
        locale,
        clusterGateValue,
      ),
    [clusterGateValue, linkedSuggestion.id, locale],
  )
  const projectorPlaybackStepDurations = useMemo(
    () => projectorPlaybackStepMs(projectorStations, linkedSuggestion),
    [linkedSuggestion.scope, projectorStations],
  )
  const projectorPlaybackDurationMs = useMemo(
    () =>
      projectorPlaybackStepDurations.reduce(
        (total, durationMs) => total + durationMs,
        0,
      ),
    [projectorPlaybackStepDurations],
  )
  const projectorMeasuredStepDurations = useMemo(
    () => projectorMeasuredStepMs(projectorStations, linkedSuggestion),
    [linkedSuggestion, projectorStations],
  )
  const projectorTiming = useMemo(
    () =>
      projectorTimingMeasurement(
        lifecycle,
        linkedSuggestion,
        incidentTimeline,
      ),
    [incidentTimeline, lifecycle, linkedSuggestion],
  )
  const projectorActualDurationMs = projectorTiming.durationMs
  const projectorDisplayDurationMs =
    projectorActualDurationMs ?? projectorPlaybackDurationMs
  const playbackDrivenStation =
    projectorStations[projectorPlaybackIndex] ?? projectorStations[0]
  const selectedProjector =
    projectorPlaybackRunning
      ? playbackDrivenStation
      : projectorStations.find((station) => station.id === selectedProjectorId) ??
        projectorStations.find(
          (station) => station.guidedStageId === selectedGuidedStage.id,
        ) ??
        projectorStations[0]
  const selectedProjectorIndex = projectorStations.findIndex(
    (station) => station.id === selectedProjector.id,
  )
  const nextProjector =
    selectedProjectorIndex >= 0
      ? projectorStations[selectedProjectorIndex + 1]
      : undefined
  const projectorPlaybackRatio =
    projectorPlaybackDurationMs > 0
      ? Math.min(1, projectorPlaybackElapsedMs / projectorPlaybackDurationMs)
      : 1
  const projectorDisplayedElapsedMs =
    projectorActualDurationMs !== null
      ? projectorDisplayDurationMs
      : projectorPlaybackRunning
        ? projectorDisplayDurationMs * projectorPlaybackRatio
        : projectorDisplayDurationMs
  const projectorTimingLabel =
    projectorActualDurationMs !== null
      ? locale === 'zh'
        ? '实测链路时长'
        : 'measured chain timing'
      : locale === 'zh'
        ? '演示回放时钟'
        : 'demo playback clock'
  const projectorTimingNote =
    projectorActualDurationMs !== null
      ? locale === 'zh'
        ? `${projectorTimingSourceLabel(projectorTiming.source, locale)}，不是前端回放编排`
        : `${projectorTimingSourceLabel(projectorTiming.source, locale)}; not the frontend replay schedule`
      : locale === 'zh'
        ? `未拿到有效阶段时序，当前仅显示固定 ${formatProjectorTimer(projectorPlaybackDurationMs)} 的前端回放编排`
        : `No valid stage timing available; showing the fixed ${formatProjectorTimer(projectorPlaybackDurationMs)} frontend replay schedule.`
  const inspectorState = projectorInspectorState(
    selectedProjectorIndex,
    projectorPlaybackIndex,
    projectorPlaybackRunning,
    locale,
  )
  const inspectorRole = projectorInspectorRole(selectedProjector.id, locale)
  const inspectorTransition = projectorTransitionCopy(
    selectedProjector,
    nextProjector,
    locale,
  )
  const inspectorFields = projectorInspectorFields(selectedProjector, locale)
  const nodeInspectorSurface = buildNodeInspectorSurfaceModel({
    title: selectedProjector.title,
    role: inspectorRole,
    state: inspectorState,
    token: selectedProjector.token,
    caption: selectedProjector.caption,
    facts: inspectorFields,
    detail: selectedProjector.detail,
    transition: inspectorTransition,
    nextTitle: nextProjector?.title,
    sources: selectedProjector.sources,
  })
  const controlLoopStripItems = [
    {
      id: 'raw',
      label: 'RAW',
      value: controlLoopMetricValue(snapshot, 'raw-freshness'),
    },
    {
      id: 'alert',
      label: 'ALERT',
      value: formatMaybeTimestamp(snapshot.controlLoop.latestAlertTs, 'time'),
    },
    {
      id: 'sgst',
      label: 'SGST',
      value: formatMaybeTimestamp(snapshot.controlLoop.latestSuggestionTs, 'time'),
    },
    {
      id: 'loop',
      label: 'LOOP',
      value: controlLoopMetricValue(snapshot, 'closure'),
    },
  ]
  const currentIncidentKey = incidentKeyForSuggestion(selectedSuggestion)
  const currentChainEvent =
    queueEvents.find((event) => event.dedupeKey === currentIncidentKey) ?? null
  const isViewingCurrentChain = activeEvent?.dedupeKey === currentIncidentKey

  useEffect(() => {
    if (selectedEventId || queueEvents.length === 0) {
      return
    }

    const seededEvent =
      queueEvents.find(
        (event) => event.dedupeKey === incidentKeyForSuggestion(selectedSuggestion),
      ) ?? queueEvents[0]
    const seededSuggestion = suggestionForEvent(
      seededEvent,
      snapshot.suggestions,
      selectedSuggestion,
    )

    setSelectedEventId(seededEvent.id)
    setSelectedLinkedSuggestionId(seededSuggestion.id)
    setPinnedEvent(seededEvent)
    setPinnedSuggestion(seededSuggestion)
    setProjectorReplaySeed((currentValue) => currentValue + 1)
  }, [queueEvents, selectedEventId, selectedSuggestion, snapshot.suggestions])

  useEffect(() => {
    if (!hasMountedProjectorRef.current) {
      hasMountedProjectorRef.current = true
      setScrambledHeadlinePrimary(story.headlinePrimary)
      setScrambledHeadlineDevice(story.headlineDevice)
      setScrambledHeadlineRoute(story.headlineRoute)
      return
    }

    setProjectorTheme('alert')

    let frame = 0
    const totalFrames = 20
    const scrambleTimer = window.setInterval(() => {
      frame += 1
      const progress = Math.min(frame / totalFrames, 1)
      setScrambledHeadlinePrimary(
        scrambleProjectorText(story.headlinePrimary, progress),
      )
      setScrambledHeadlineDevice(
        scrambleProjectorText(story.headlineDevice, progress),
      )
      setScrambledHeadlineRoute(
        scrambleProjectorText(story.headlineRoute, progress),
      )

      if (progress >= 1) {
        window.clearInterval(scrambleTimer)
      }
    }, 50)

    return () => {
      window.clearInterval(scrambleTimer)
    }
  }, [
    linkedSuggestion.id,
    locale,
    story.headlineDevice,
    story.headlinePrimary,
    story.headlineRoute,
  ])

  useEffect(() => {
    if (projectorReplaySeed === 0) {
      return
    }

    setSelectedProjectorId(null)
    setProjectorPlaybackElapsedMs(0)
    setProjectorPlaybackIndex(0)
    setProjectorPlaybackRunning(true)
    setProjectorTheme('alert')
    setExpandedStageSelection({
      suggestionId: linkedSuggestion.id,
      stageId: projectorStations[0]?.guidedStageId ?? null,
    })

    let startMs: number | null = null
    let frameId = 0

    const step = (frameMs: number) => {
      if (startMs === null) {
        startMs = frameMs
      }

      const elapsedMs = frameMs - startMs
      const clampedElapsedMs = Math.min(elapsedMs, projectorPlaybackDurationMs)
      const nextIndex = playbackIndexForElapsed(
        projectorPlaybackStepDurations,
        clampedElapsedMs,
      )

      setProjectorPlaybackElapsedMs(clampedElapsedMs)
      setProjectorPlaybackIndex(nextIndex)
      setExpandedStageSelection((currentValue) => {
        const nextStageId = projectorStations[nextIndex]?.guidedStageId ?? null
        if (
          currentValue?.suggestionId === linkedSuggestion.id &&
          currentValue?.stageId === nextStageId
        ) {
          return currentValue
        }
        return {
          suggestionId: linkedSuggestion.id,
          stageId: nextStageId,
        }
      })

      if (clampedElapsedMs >= projectorPlaybackDurationMs) {
        setProjectorPlaybackRunning(false)
        setProjectorTheme('calm')
        setProjectorPlaybackElapsedMs(projectorPlaybackDurationMs)
        setProjectorPlaybackIndex(projectorStations.length - 1)
        setSelectedProjectorId(
          projectorStations[projectorStations.length - 1]?.id ?? null,
        )
        return
      }

      frameId = window.requestAnimationFrame(step)
    }

    frameId = window.requestAnimationFrame(step)

    return () => window.cancelAnimationFrame(frameId)
  }, [projectorReplaySeed])

  const selectIncident = (event: HistoricalIncidentEvent) => {
    const nextSuggestion = suggestionForEvent(
      event,
      snapshot.suggestions,
      selectedSuggestion,
    )
    setSelectedEventId(event.id)
    setSelectedLinkedSuggestionId(nextSuggestion.id)
    setPinnedEvent(event)
    setPinnedSuggestion(nextSuggestion)
    setProjectorReplaySeed((currentValue) => currentValue + 1)
    setSelectedProjectorId(null)
    setExpandedStageSelection(null)
    if (nextSuggestion.id !== selectedSuggestion.id) {
      onSelectSuggestion(nextSuggestion.id)
    }
  }

  const handleSelectProjectorStation = (station: ProjectorStation) => {
    setProjectorPlaybackRunning(false)
    setProjectorTheme('calm')
    setProjectorPlaybackIndex(
      projectorStations.findIndex((item) => item.id === station.id),
    )
    setSelectedProjectorId(station.id)
    setExpandedStageSelection({
      suggestionId: linkedSuggestion.id,
      stageId: station.guidedStageId,
    })
  }

  const focusCurrentChain = () => {
    const liveEvent =
      currentChainEvent ??
      queueEvents.find(
        (event) => event.relatedSuggestionId === selectedSuggestion.id,
      ) ??
      queueEvents[0]

    if (!liveEvent) {
      return
    }

    selectIncident(liveEvent)
  }

  return (
    <section className="page console-page">
      {hardIntegrityIssues.length > 0 ? (
        <section className="section integrity-warning">
          <div className="section-header">
            <div>
              <h2 className="section-title">Control Loop Integrity Warning</h2>
              <span className="section-subtitle">
                This console detected live snapshot drift instead of silently pretending
                lifecycle timing is valid.
              </span>
            </div>
            <span className="section-kicker">hard warning / not cosmetic</span>
          </div>
          <ul className="integrity-list">
            {hardIntegrityIssues.map((issue) => (
              <li key={issue.id}>{issue.detail}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <section
        ref={heroStageRef}
        id="control-loop-main-stage"
        className="section story-stage-shell"
      >
        <div className="section-header story-stage-header">
          <div>
            <h2 className="section-title">
              {locale === 'zh' ? '事件主画面' : 'Incident Main Stage'}
            </h2>
            <span className="section-subtitle">
              {locale === 'zh'
                ? '先看发生了什么、影响谁、系统准备做什么，再按阶段继续往下钻。'
                : 'See what happened, who it affects, and what the system prepared before drilling into deeper details.'}
            </span>
          </div>
          <div className="annotation-stack">
            <span className="section-kicker">
              {locale === 'zh' ? '全屏主画面 / 结果优先' : 'full-screen stage / result first'}
            </span>
            <div className="story-stage-actions">
              <span className={`signal-chip tone-${pulseTone}`}>{pulseTone}</span>
            </div>
          </div>
        </div>

        <div className={`story-stage-canvas projector-canvas is-${projectorTheme}-theme`}>
          <aside className="story-stage-darkside story-stage-rail">
            <div className="story-stage-tag">
              <span>{locale === 'zh' ? '事件队列' : 'event queue'}</span>
              <strong>
                {locale === 'zh'
                  ? `${queueEvents.length} 条历史问题事件`
                  : `${queueEvents.length} historical incidents`}
              </strong>
            </div>

            <div className="story-stage-queue-head">
              <span className="section-kicker">
                {locale === 'zh'
                  ? '历史序列 / 当前链路'
                  : 'history sequence / live chain'}
              </span>
              <button
                type="button"
                className={`story-stage-focus-switch ${isViewingCurrentChain ? 'is-active' : ''}`}
                onClick={focusCurrentChain}
              >
                <span>
                  {locale === 'zh'
                    ? '跳出历史序列，转到当前事件链路'
                    : 'leave history and focus the current incident chain'}
                </span>
                <strong>
                  {locale === 'zh'
                    ? currentChainEvent?.title ?? '当前链路'
                    : currentChainEvent?.title ?? 'current incident chain'}
                </strong>
              </button>
            </div>

            <div className="story-stage-queue">
              {queueEvents.map((event, index) => {
                const isLead = index === 0
                const isActive = activeEvent?.id === event.id

                return (
                  <button
                    key={event.id}
                    type="button"
                    className={`event-row kind-${event.kind} ${isLead ? 'is-lead' : ''} ${isActive ? 'is-active' : ''}`}
                    onClick={() => selectIncident(event)}
                  >
                    <div className="event-row-head">
                      <span className={`signal-chip tone-${event.kind}`}>
                        {event.scope ? `${event.kind}/${event.scope}` : event.kind}
                      </span>
                      <div className="event-row-topline">
                        <span className="event-row-count">
                          {groupedRefreshLabel(event.mergedSuggestionCount, locale)}
                        </span>
                        <span
                          className="event-stamp"
                          title={timestampTooltip(event.stamp)}
                        >
                          {formatMaybeTimestamp(event.stamp, 'time')}
                        </span>
                      </div>
                    </div>
                    <strong>{event.title}</strong>
                    <p>{event.detail}</p>
                    <div className="event-row-meta">
                      <span>{event.service ?? 'n/a'}</span>
                      <span>{event.device ?? event.provider ?? 'control-loop'}</span>
                      <span>{event.scopeMeaning}</span>
                    </div>
                  </button>
                )
              })}
            </div>

            <article className="story-stage-node-slab">
              <div className="story-stage-node-shell">
                <div className="story-stage-node-head">
                  <span className="section-kicker">
                    {locale === 'zh' ? '节点检视板' : 'node inspector'}
                  </span>
                  <div className="story-stage-node-headline">
                    <span className="story-stage-node-index">
                      {(selectedProjectorIndex + 1).toString().padStart(2, '0')}
                    </span>
                    <div className="story-stage-node-titleblock">
                      <strong>{nodeInspectorSurface.title}</strong>
                      <em>{nodeInspectorSurface.role}</em>
                    </div>
                    <div className="story-stage-node-statebar">
                      <span>
                        {locale === 'zh' ? '当前状态' : 'current state'}
                      </span>
                      <strong>{nodeInspectorSurface.state}</strong>
                    </div>
                  </div>
                </div>

                <div className="story-stage-node-baseline">
                  <div className="story-stage-node-tokenrail">
                    <span className="story-stage-node-token">{nodeInspectorSurface.token}</span>
                    <span className="story-stage-node-caption">{nodeInspectorSurface.caption}</span>
                  </div>
                  <div className="story-stage-node-guideline" aria-hidden="true" />
                </div>

                <div className="story-stage-node-core">
                  <section className="story-stage-node-band is-evidence">
                    <span className="story-stage-node-label">
                      {locale === 'zh' ? '局部证据' : 'local evidence'}
                    </span>
                    <dl className="story-stage-node-facts">
                      {nodeInspectorSurface.localEvidence.map((field) => (
                        <div
                          key={`${selectedProjector.id}-${field.label}`}
                          className="story-stage-node-fact"
                        >
                          <dt>{field.label}</dt>
                          <dd>{field.value}</dd>
                        </div>
                      ))}
                    </dl>
                  </section>

                  <section className="story-stage-node-band is-transition">
                    <span className="story-stage-node-label">
                      {locale === 'zh' ? '后续依赖' : 'next dependency'}
                    </span>
                    <strong>{nodeInspectorSurface.caption}</strong>
                    <p>{nodeInspectorSurface.transitionNote}</p>
                  </section>
                </div>

                <div className="story-stage-node-annotation-rail">
                  <section className="story-stage-node-annotation">
                    <div className="story-stage-node-annotation-head">
                      <span className="story-stage-node-label">
                        {locale === 'zh' ? '局部读取' : 'local reading'}
                      </span>
                      <strong>{nodeInspectorSurface.nextDependency}</strong>
                    </div>
                    <p>{nodeInspectorSurface.readingLine}</p>
                  </section>

                  <section className="story-stage-node-annotation">
                    <span className="story-stage-node-label">
                      {locale === 'zh' ? 'basis markers' : 'basis markers'}
                    </span>
                    {nodeInspectorSurface.basisMarkers.length > 0 ? (
                      <div className="story-stage-node-marker-strip">
                        {nodeInspectorSurface.basisMarkers.map((marker) => (
                          <article
                            key={`${selectedProjector.id}-${marker.ref}-${marker.label}`}
                            className="story-stage-node-marker"
                          >
                            <span>{marker.label}</span>
                            <strong>{marker.value}</strong>
                            <em>{marker.ref}</em>
                          </article>
                        ))}
                      </div>
                    ) : (
                      <p>
                        {locale === 'zh'
                          ? '当前节点没有单独 basis marker。'
                          : 'No standalone basis marker for the current node.'}
                      </p>
                    )}
                  </section>
                </div>

                {softIntegrityIssues.length > 0 ? (
                  <div className="story-stage-node-note">
                    <span>{locale === 'zh' ? '运行提示' : 'control-loop note'}</span>
                    <strong>
                      {locale === 'zh'
                        ? '当前快照仍有轻微时间窗偏差。'
                        : 'The current snapshot still carries mild timing skew.'}
                    </strong>
                  </div>
                ) : null}

                <div className="story-stage-node-telemetry-strip">
                  {controlLoopStripItems.map((item) => (
                    <div key={item.id} className="story-stage-node-telemetry-item">
                      <span>{item.label}</span>
                      <strong>{item.value}</strong>
                    </div>
                  ))}
                </div>
              </div>
            </article>
          </aside>

          <section className="incident-projector-shell">
            <div className="incident-projector-head">
              <div className="incident-projector-copy">
                <p className="incident-projector-kicker">{story.eyebrow}</p>
                <h3 className="incident-projector-headline">
                  <span className="headline-primary">{scrambledHeadlinePrimary}</span>
                  <span className="headline-device">{scrambledHeadlineDevice}</span>
                  <span className="headline-route">{scrambledHeadlineRoute}</span>
                </h3>
                <p className="incident-projector-summary">{story.summary}</p>
              </div>

              <div
                className={`incident-projector-timing ${projectorPlaybackRunning ? 'is-running' : 'is-complete'}`}
              >
                <span className="incident-projector-timing-label">
                  {projectorTimingLabel}
                </span>
                <strong>
                  {projectorDisplayedElapsedMs !== null
                    ? `${formatProjectorTimer(projectorDisplayedElapsedMs)}${projectorActualDurationMs === null ? '*' : ''}`
                    : locale === 'zh'
                      ? '时序缺失'
                      : 'timing unavailable'}
                </strong>
                <span className="incident-projector-timing-note">
                  {projectorTimingNote}
                </span>
              </div>
            </div>

            <div className="incident-projector-stage">
              <div className="incident-projector-beam" aria-hidden="true">
                <span
                  className="incident-projector-progress"
                  style={{
                    width: `${Math.max(
                      ((selectedProjectorIndex + 1) / projectorStations.length) * 100,
                      projectorPlaybackRatio * 100,
                    )}%`,
                  }}
                />
                <span className="incident-projector-scan" />
              </div>

              <div className="incident-projector-path">
                {projectorStations.map((station, index) => {
                  const isActive = station.id === selectedProjector.id
                  const isRaised = index % 2 === 0
                  const isLit = index <= selectedProjectorIndex
                  const timerTone =
                    projectorPlaybackRunning && isActive
                      ? 'is-active'
                      : isLit
                        ? 'is-complete'
                        : 'is-idle'
                  const stationElapsedMs =
                    projectorActualDurationMs !== null
                      ? projectorMeasuredStepDurations[index] ?? null
                      : null

                  return (
                    <button
                      key={station.id}
                      type="button"
                      className={`incident-projector-node evidence-${station.evidenceState} ${isActive ? 'is-active' : ''} ${isLit ? 'is-lit' : ''} ${isRaised ? 'is-raised' : 'is-lowered'}`}
                      onClick={() => handleSelectProjectorStation(station)}
                    >
                      <span className="incident-projector-index">
                        {(index + 1).toString().padStart(2, '0')}
                      </span>
                      <span className="incident-projector-dot" aria-hidden="true" />
                      <span className="incident-projector-label">
                        <span className="incident-projector-label-head">
                          <strong>{station.title}</strong>
                          <span className={`incident-projector-node-timer ${timerTone}`}>
                            {formatProjectorStationTimer(
                              stationElapsedMs,
                              projectorActualDurationMs !== null,
                              locale,
                            )}
                          </span>
                        </span>
                        <span className="incident-projector-token">{station.token}</span>
                      </span>
                      <span className="incident-projector-caption">{station.caption}</span>
                      {isActive ? (
                        <span
                          className={`incident-projector-annotation ${isRaised ? 'is-below' : 'is-above'}`}
                        >
                          <span className="incident-projector-annotation-copy">
                            {station.detail}
                          </span>
                          <dl className="incident-projector-facts">
                            {station.facts.map((fact) => (
                              <div
                                key={`${station.id}-${fact.label}`}
                                className="incident-projector-fact"
                              >
                                <dt>{fact.label}</dt>
                                <dd>{fact.value}</dd>
                              </div>
                            ))}
                          </dl>
                          {station.sources.length > 0 ? (
                            <div className="incident-projector-sources">
                              <strong>
                                {locale === 'zh' ? '依据源' : 'basis sources'}
                              </strong>
                              <div className="incident-projector-source-list">
                                {station.sources.map((source) => (
                                  <article
                                    key={`${station.id}-${source.section}-${source.field}-${source.label}`}
                                    className="incident-projector-source"
                                  >
                                    <span className="incident-projector-source-head">
                                      <em>{source.label}</em>
                                      <code>{source.section}.{source.field}</code>
                                    </span>
                                    <strong>{source.value}</strong>
                                    <p>{source.reason}</p>
                                  </article>
                                ))}
                              </div>
                            </div>
                          ) : null}
                        </span>
                      ) : null}
                    </button>
                  )
                })}
              </div>

              <div className="incident-projector-toolbar">
                <div className="incident-projector-toolbar-meta">
                  <span className="section-kicker">
                    {locale === 'zh' ? '当前投影' : 'current projection'}
                  </span>
                  <strong>{selectedProjector.title}</strong>
                  <span>{selectedProjector.token}</span>
                </div>
                <p className="incident-projector-guidance">
                  {locale === 'zh'
                    ? `分析：${selectedInference} 建议：${selectedRecommendation}`
                    : `Analysis: ${selectedInference} Recommendation: ${selectedRecommendation}`}
                </p>
              </div>
            </div>
          </section>
        </div>
      </section>

      <section className="section incident-convergence-shell">
        <div className="section-header incident-convergence-header">
          <div>
            <h2 className="section-title">
              {locale === 'zh' ? '事件收束面' : 'Incident Convergence Field'}
            </h2>
            <span className="section-subtitle">
              {locale === 'zh'
                ? '保留离散事件的原始分布，再把当前上下文、归并依据、假设收束和 runbook 草案放到同一块工作面里。'
                : 'Keep the raw event spread visible, then show context assembly, incident grouping, hypothesis convergence, and the runbook draft on one working surface.'}
            </span>
          </div>
          <div className="incident-convergence-actions">
            <span className="section-kicker">
              {locale === 'zh' ? 'flow map / 全屏工作面' : 'flow map / fullscreen workspace'}
            </span>
            <button
              type="button"
              className="story-stage-action is-bright"
              onClick={() => setIsIncidentGraphExpanded(true)}
            >
              {locale === 'zh' ? '打开 Flow Map' : 'Open Flow Map'}
            </button>
          </div>
        </div>

        <div className="incident-convergence-layout">
          <section className="incident-convergence-field">
            <div className="incident-convergence-field-head">
              <div>
                <span className="section-kicker">
                  {locale === 'zh' ? '离散事件 / 上下文收束' : 'distributed events / context convergence'}
                </span>
                <strong>{activeEvent?.title ?? linkedSuggestion.summary}</strong>
              </div>
              <div className="incident-convergence-field-meta">
                <span>{selectedRefreshSummary}</span>
                <span>{clusterGateValue}</span>
                <span>{localizedStageTitle(selectedGuidedStage.id, locale)}</span>
              </div>
            </div>

            <div className="incident-convergence-surface">
              <svg
                className="incident-convergence-links"
                viewBox="0 0 100 100"
                preserveAspectRatio="none"
                aria-hidden="true"
              >
                {incidentConvergenceModel.links.map((link) => {
                  const sourceAnchor =
                    link.sourceKind === 'anchor'
                      ? incidentConvergenceModel.anchors.find(
                          (anchor) => anchor.id === link.sourceId,
                        )
                      : null
                  const sourcePoint =
                    link.sourceKind === 'point'
                      ? incidentConvergenceModel.points.find(
                          (point) => point.eventId === link.sourceId,
                        )
                      : null
                  const targetAnchor = incidentConvergenceModel.anchors.find(
                    (anchor) => anchor.id === link.targetId,
                  )

                  if (!targetAnchor || (!sourceAnchor && !sourcePoint)) {
                    return null
                  }

                  const fromX = sourceAnchor?.x ?? sourcePoint?.x ?? 0
                  const fromY = sourceAnchor?.y ?? sourcePoint?.y ?? 0
                  const toX = targetAnchor.x
                  const toY = targetAnchor.y

                  return (
                    <g
                      key={link.id}
                      className={`incident-convergence-link weight-${link.weight}`}
                    >
                      <line x1={fromX} y1={fromY} x2={toX} y2={toY} />
                    </g>
                  )
                })}
              </svg>

              {incidentConvergenceModel.anchors.map((anchor) => (
                <article
                  key={anchor.id}
                  className={`incident-convergence-anchor tone-${anchor.tone}`}
                  style={{ left: `${anchor.x}%`, top: `${anchor.y}%` }}
                >
                  <span>{anchor.label}</span>
                  <strong>{anchor.headline}</strong>
                  <p>{anchor.detail}</p>
                </article>
              ))}

              {incidentConvergenceModel.points.map((point) => {
                const event = queueEvents.find((item) => item.id === point.eventId)

                return (
                  <button
                    key={point.eventId}
                    type="button"
                    className={`incident-convergence-point size-${point.size}`}
                    style={{ left: `${point.x}%`, top: `${point.y}%` }}
                    onClick={() => {
                      if (event) {
                        selectIncident(event)
                      }
                    }}
                    title={`${point.title} · ${point.reason}`}
                  >
                    <span className="incident-convergence-dot" />
                    <span className="incident-convergence-point-copy">
                      <strong>{point.title}</strong>
                      <em>{formatMaybeTimestamp(point.stamp, 'time')}</em>
                      <span>{point.reason}</span>
                    </span>
                  </button>
                )
              })}

              <div className="incident-convergence-footer">
                <article className="incident-convergence-reading">
                  <span className="story-stage-node-label">
                    {locale === 'zh' ? '当前判断' : 'current reading'}
                  </span>
                  <strong>{selectedInference}</strong>
                  <p>{selectedScopeMeaning}</p>
                </article>

                <div className="incident-convergence-phase-strip">
                  {selectedStagePhases.map((phase, index) => (
                    <article
                      key={`${selectedGuidedStage.id}-${phase.id}`}
                      className={`incident-convergence-phase state-${phase.status}`}
                    >
                      <span>{(index + 1).toString().padStart(2, '0')}</span>
                      <strong>{phase.title}</strong>
                      <p>{phase.band.detail}</p>
                    </article>
                  ))}
                </div>
              </div>
            </div>
          </section>

          <aside className="incident-runbook-surface">
            <div className="incident-runbook-head">
              <span className="section-kicker">
                {locale === 'zh' ? 'operator handoff / runbook' : 'operator handoff / runbook'}
              </span>
              <strong>{incidentConvergenceModel.runbook.title}</strong>
              <em>{incidentConvergenceModel.runbook.scopeLabel}</em>
              <span>{incidentConvergenceModel.runbook.applicability}</span>
            </div>

            <section className="incident-runbook-section">
              <span>{locale === 'zh' ? '预检查' : 'prechecks'}</span>
              <ul>
                {incidentConvergenceModel.runbook.prechecks.map((item) => (
                  <li key={`precheck-${item}`}>{item}</li>
                ))}
              </ul>
            </section>

            <section className="incident-runbook-section">
              <span>{locale === 'zh' ? '动作草案' : 'operator actions'}</span>
              <ul>
                {incidentConvergenceModel.runbook.operatorActions.map((item) => (
                  <li key={`action-${item}`}>{item}</li>
                ))}
              </ul>
            </section>

            <section className="incident-runbook-section">
              <span>{locale === 'zh' ? '依据源' : 'basis sources'}</span>
              {selectedProjector.sources.length > 0 ? (
                <ul>
                  {selectedProjector.sources.slice(0, 4).map((source) => (
                    <li key={`${source.section}-${source.field}-${source.label}`}>
                      {source.label}: {source.value}
                    </li>
                  ))}
                </ul>
              ) : (
                <ul>
                  <li>
                    {locale === 'zh'
                      ? '当前节点没有额外 basis source。'
                      : 'No additional basis source is attached for the current node.'}
                  </li>
                </ul>
              )}
            </section>

            <section className="incident-runbook-section">
              <span>{locale === 'zh' ? '审批边界' : 'governance boundary'}</span>
              <ul>
                {incidentConvergenceModel.runbook.boundaries.map((item) => (
                  <li key={`boundary-${item}`}>{item}</li>
                ))}
              </ul>
            </section>

            <section className="incident-runbook-section">
              <span>{locale === 'zh' ? '回退说明' : 'rollback posture'}</span>
              <ul>
                {incidentConvergenceModel.runbook.rollback.map((item) => (
                  <li key={`rollback-${item}`}>{item}</li>
                ))}
              </ul>
            </section>

            {relatedTimelineSteps.length > 0 ? (
              <section className="incident-runbook-section is-timeline">
                <span>{locale === 'zh' ? '相关时间点' : 'related timeline steps'}</span>
                <ul>
                  {relatedTimelineSteps.map((step) => (
                    <li key={`${selectedGuidedStage.id}-${step.id}`}>
                      {formatMaybeTimestamp(step.stamp, 'time')} · {step.title}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </aside>
        </div>
      </section>

      {isIncidentGraphExpanded ? (
        <div className="pipeline-overlay">
          <button
            type="button"
            className="pipeline-overlay-backdrop"
            aria-label={locale === 'zh' ? '关闭流程图' : 'Close pipeline'}
            onClick={() => setIsIncidentGraphExpanded(false)}
          />
          <div className="pipeline-overlay-shell">
            <section className="section pipeline-overlay-panel">
              <div className="section-header">
                <div>
                  <h2 className="section-title">
                    {locale === 'zh' ? '历史事件全屏流程图' : 'Fullscreen Incident Pipeline'}
                  </h2>
                  <span className="section-subtitle">
                    {locale === 'zh'
                      ? '当前事件的流程、证据和阶段链路在这里展开到全屏。'
                      : 'Expand the selected incident graph, evidence path, and stage chain into a full-screen workspace.'}
                  </span>
                </div>
                <button
                  type="button"
                  className="story-stage-action"
                  onClick={() => setIsIncidentGraphExpanded(false)}
                >
                  {locale === 'zh' ? '关闭' : 'Close'}
                </button>
              </div>

              <div className="pipeline-overlay-grid">
                <div className="pipeline-overlay-graph">
                  <Suspense
                    fallback={
                      <div className="flow-frame">
                        <div className="flow-surface chart-fallback">
                          {locale === 'zh' ? '正在载入全屏流程图...' : 'loading fullscreen pipeline...'}
                        </div>
                      </div>
                    }
                  >
                    <TopologyCanvas
                      nodes={incidentProcessGraph.nodes}
                      links={incidentProcessGraph.links}
                    />
                  </Suspense>
                </div>

                <aside className="pipeline-overlay-sidebar">
                  <article className="focus-card">
                    <strong>{locale === 'zh' ? '事件摘要' : 'incident summary'}</strong>
                    <ul className="focus-list">
                      <li>{activeEvent?.title ?? linkedSuggestion.summary}</li>
                      <li>{story.summary}</li>
                      <li>{primaryRecommendation(linkedSuggestion)}</li>
                    </ul>
                  </article>

                  <article className="focus-card">
                    <strong>{locale === 'zh' ? '完整时间线' : 'full timeline'}</strong>
                    <ul className="focus-list">
                      {incidentTimeline.map((step) => (
                        <li key={`${linkedSuggestion.id}-overlay-${step.id}`}>
                          {formatMaybeTimestamp(step.stamp, 'time')} · {step.title}
                        </li>
                      ))}
                    </ul>
                  </article>
                </aside>
              </div>
            </section>
          </div>
        </div>
      ) : null}

    </section>
  )
}
