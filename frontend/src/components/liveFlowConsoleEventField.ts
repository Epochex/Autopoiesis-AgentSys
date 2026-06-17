import type { SuggestionRecord } from '../types'
import { parseTimestamp } from '../utils/time'

export type EventFieldLocale = 'en' | 'zh'

export interface IncidentQueueLike {
  id: string
  dedupeKey: string
  stamp: string
  title: string
  service?: string
  device?: string
  scope?: 'alert' | 'cluster'
}

export interface IncidentFieldPoint {
  eventId: string
  title: string
  stamp: string
  x: number
  y: number
  size: 'primary' | 'related' | 'ambient'
  reason: string
  targetAnchorId: IncidentFieldAnchor['id'] | null
}

export interface IncidentFieldAnchor {
  id: 'context-anchor' | 'cluster-anchor' | 'hypothesis-anchor'
  label: string
  headline: string
  detail: string
  x: number
  y: number
  tone: 'context' | 'cluster' | 'hypothesis'
}

export interface IncidentFieldLink {
  id: string
  sourceKind: 'point' | 'anchor'
  sourceId: string
  targetId: IncidentFieldAnchor['id']
  weight: 'soft' | 'medium' | 'strong'
  label: string
}

export interface IncidentRunbookDraft {
  title: string
  scopeLabel: string
  applicability: string
  prechecks: string[]
  operatorActions: string[]
  boundaries: string[]
  rollback: string[]
  evidenceLabels: string[]
}

export interface IncidentConvergenceModel {
  points: IncidentFieldPoint[]
  anchors: IncidentFieldAnchor[]
  links: IncidentFieldLink[]
  runbook: IncidentRunbookDraft
}

interface BuildIncidentConvergenceModelInput {
  locale: EventFieldLocale
  queueEvents: IncidentQueueLike[]
  activeEvent: IncidentQueueLike | null
  linkedSuggestion: SuggestionRecord
  clusterGateValue: string
  selectedInference: string
  selectedRecommendation: string
  selectedWindowSummary: string
  selectedScopeMeaning: string
  selectedRefreshSummary: string
}

function resolvedRunbookDraft(linkedSuggestion: SuggestionRecord) {
  const draft = linkedSuggestion.runbookDraft
  const applicability = draft?.applicability

  return {
    title:
      typeof draft?.title === 'string' && draft.title.trim().length > 0
        ? draft.title.trim()
        : '',
    applicability: {
      service:
        typeof applicability?.service === 'string' ? applicability.service : '',
      pathSignature:
        typeof applicability?.pathSignature === 'string'
          ? applicability.pathSignature
          : '',
    },
    prechecks: Array.isArray(draft?.prechecks) ? draft.prechecks : [],
    operatorActions: Array.isArray(draft?.operatorActions)
      ? draft.operatorActions
      : [],
    boundaries: Array.isArray(draft?.boundaries) ? draft.boundaries : [],
    rollbackGuidance: Array.isArray(draft?.rollbackGuidance)
      ? draft.rollbackGuidance
      : [],
  }
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

function stableHash(value: string) {
  let hash = 0
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 33 + value.charCodeAt(index)) >>> 0
  }
  return hash
}

function normalizedTime(eventTs: string, minTs: number, maxTs: number) {
  const currentTs = parseTimestamp(eventTs)?.getTime() ?? minTs
  if (maxTs <= minTs) {
    return 0.5
  }
  return clamp((currentTs - minTs) / (maxTs - minTs), 0, 1)
}

function relationReason(
  event: IncidentQueueLike,
  activeEvent: IncidentQueueLike,
  locale: EventFieldLocale,
) {
  if (event.id === activeEvent.id) {
    return {
      score: 4,
      reason:
        locale === 'zh' ? '当前选中事件' : 'current selected incident',
      targetAnchorId: 'hypothesis-anchor' as const,
    }
  }

  if (event.dedupeKey === activeEvent.dedupeKey) {
    return {
      score: 3,
      reason:
        locale === 'zh' ? '相同 incident key' : 'same incident key',
      targetAnchorId: 'cluster-anchor' as const,
    }
  }

  if (event.service && activeEvent.service && event.service === activeEvent.service) {
    return {
      score: 2,
      reason:
        locale === 'zh' ? '共享服务路径' : 'shared service path',
      targetAnchorId: 'context-anchor' as const,
    }
  }

  if (event.device && activeEvent.device && event.device === activeEvent.device) {
    return {
      score: 2,
      reason:
        locale === 'zh' ? '共享设备身份' : 'shared device identity',
      targetAnchorId: 'context-anchor' as const,
    }
  }

  return {
    score: 0,
    reason:
      locale === 'zh' ? '背景噪声事件' : 'background noise event',
    targetAnchorId: null,
  }
}

function evidenceLabels(
  suggestion: SuggestionRecord,
  locale: EventFieldLocale,
) {
  const labels = [
    Object.keys(suggestion.evidenceBundle.topology).length > 0
      ? locale === 'zh'
        ? '拓扑'
        : 'topology'
      : null,
    Object.keys(suggestion.evidenceBundle.device).length > 0
      ? locale === 'zh'
        ? '设备'
        : 'device'
      : null,
    Object.keys(suggestion.evidenceBundle.change).length > 0
      ? locale === 'zh'
        ? '变更'
        : 'change'
      : null,
    Object.keys(suggestion.evidenceBundle.historical).length > 0
      ? locale === 'zh'
        ? '历史'
        : 'historical'
      : null,
  ]

  return labels.filter((label): label is string => Boolean(label))
}

function buildRunbookDraft(
  input: BuildIncidentConvergenceModelInput,
): IncidentRunbookDraft {
  const {
    locale,
    linkedSuggestion,
    clusterGateValue,
    selectedRecommendation,
    selectedWindowSummary,
    selectedScopeMeaning,
    selectedRefreshSummary,
  } = input
  const runbookDraft = resolvedRunbookDraft(linkedSuggestion)
  const service = linkedSuggestion.context.service
  const device =
    typeof linkedSuggestion.evidenceBundle.device.device_name === 'string' &&
    linkedSuggestion.evidenceBundle.device.device_name.trim().length > 0
      ? linkedSuggestion.evidenceBundle.device.device_name.trim()
      : linkedSuggestion.context.srcDeviceKey
  const labels = evidenceLabels(linkedSuggestion, locale)
  const actions =
    runbookDraft.operatorActions.length > 0
      ? runbookDraft.operatorActions.slice(0, 4)
      : linkedSuggestion.recommendedActions.length > 0
        ? linkedSuggestion.recommendedActions.slice(0, 3)
      : [selectedRecommendation]
  const rollback =
    runbookDraft.rollbackGuidance.length > 0
      ? runbookDraft.rollbackGuidance
      : locale === 'zh'
        ? [
            '当前没有下发动作，因此不生成设备侧回滚命令',
            '如果路径扩宽，重新回看相同 tuple 的历史窗口',
          ]
        : [
            'no device-side rollback command is emitted in the current path',
            'rerun the tuple check if the path widens beyond the current slice',
          ]
  const boundaries =
    runbookDraft.boundaries.length > 0
      ? runbookDraft.boundaries
      : locale === 'zh'
        ? [
            '当前页面只输出建议，不执行设备写回',
            '高风险动作保留给人工审批面',
            selectedScopeMeaning,
          ]
        : [
            'this page emits guidance only and does not write back to devices',
            'high-risk steps stay behind the human approval surface',
            selectedScopeMeaning,
          ]

  if (locale === 'zh') {
    return {
      title: runbookDraft.title || 'Runbook 草案',
      scopeLabel: `${service} / ${device}`,
      applicability:
        runbookDraft.applicability.pathSignature.trim().length > 0
          ? `${runbookDraft.applicability.service} · ${runbookDraft.applicability.pathSignature}`
          : `${linkedSuggestion.scope}-scope · ${linkedSuggestion.context.provider}`,
      prechecks: [
        ...runbookDraft.prechecks.slice(0, 4),
        ...(runbookDraft.prechecks.length > 0
          ? []
          : [
              `事件窗口 ${selectedWindowSummary}`,
              `聚合门槛 ${clusterGateValue}`,
              `刷新情况 ${selectedRefreshSummary}`,
              `证据附带 ${labels.join(' / ') || '未附带'}`,
            ]),
      ],
      operatorActions: actions,
      boundaries,
      rollback,
      evidenceLabels: labels,
    }
  }

  return {
    title: runbookDraft.title || 'Runbook draft',
    scopeLabel: `${service} / ${device}`,
    applicability:
      runbookDraft.applicability.pathSignature.trim().length > 0
        ? `${runbookDraft.applicability.service} · ${runbookDraft.applicability.pathSignature}`
        : `${linkedSuggestion.scope}-scope · ${linkedSuggestion.context.provider}`,
    prechecks: [
      ...runbookDraft.prechecks.slice(0, 4),
      ...(runbookDraft.prechecks.length > 0
        ? []
        : [
            `event window ${selectedWindowSummary}`,
            `cluster gate ${clusterGateValue}`,
            `refresh state ${selectedRefreshSummary}`,
            `attached evidence ${labels.join(' / ') || 'none'}`,
          ]),
    ],
    operatorActions: actions,
    boundaries,
    rollback,
    evidenceLabels: labels,
  }
}

export function buildIncidentConvergenceModel(
  input: BuildIncidentConvergenceModelInput,
): IncidentConvergenceModel {
  const {
    locale,
    queueEvents,
    activeEvent,
    linkedSuggestion,
    clusterGateValue,
    selectedInference,
    selectedRecommendation,
    selectedScopeMeaning,
  } = input
  const fallbackEvent =
    queueEvents.find((event) => event.dedupeKey === [
      linkedSuggestion.ruleId.trim().toLowerCase(),
      linkedSuggestion.scope,
      linkedSuggestion.context.service.trim().toLowerCase(),
      linkedSuggestion.context.srcDeviceKey.trim().toLowerCase(),
    ].join('::')) ?? queueEvents[0] ?? null
  const focusEvent = activeEvent ?? fallbackEvent

  const anchors: IncidentFieldAnchor[] = [
    {
      id: 'context-anchor',
      label: locale === 'zh' ? 'context router' : 'context router',
      headline: locale === 'zh' ? '上下文收束' : 'context assembly',
      detail:
        locale === 'zh'
          ? '先保留路径、设备、变更与历史。'
          : 'Keep path, device, change, and history in view.',
      x: 63,
      y: 24,
      tone: 'context',
    },
    {
      id: 'cluster-anchor',
      label: locale === 'zh' ? 'incident cluster' : 'incident cluster',
      headline: locale === 'zh' ? '关联事件归并' : 'incident merge',
      detail: clusterGateValue,
      x: 72,
      y: 50,
      tone: 'cluster',
    },
    {
      id: 'hypothesis-anchor',
      label: locale === 'zh' ? 'hypothesis set' : 'hypothesis set',
      headline: locale === 'zh' ? '当前假设' : 'current hypothesis',
      detail: selectedInference,
      x: 81,
      y: 76,
      tone: 'hypothesis',
    },
  ]

  if (!focusEvent) {
    return {
      points: [],
      anchors,
      links: [],
      runbook: buildRunbookDraft(input),
    }
  }

  const priorityIds = new Set([focusEvent.id])
  const sortedPool = queueEvents
    .slice()
    .sort((left, right) => {
      const leftTs = parseTimestamp(left.stamp)?.getTime() ?? 0
      const rightTs = parseTimestamp(right.stamp)?.getTime() ?? 0
      return leftTs - rightTs
    })
  const relatedPool = sortedPool.filter((event) => {
    const relation = relationReason(event, focusEvent, locale)
    return relation.score > 0
  })

  relatedPool.slice(0, 11).forEach((event) => priorityIds.add(event.id))
  const selectedPool = sortedPool.filter((event) => priorityIds.has(event.id))
  const ambientPool = sortedPool
    .filter((event) => !priorityIds.has(event.id))
    .slice(-7)
  const pointsPool = [...selectedPool, ...ambientPool]
  const timestamps = pointsPool
    .map((event) => parseTimestamp(event.stamp)?.getTime() ?? 0)
    .sort((left, right) => left - right)
  const minTs = timestamps[0] ?? 0
  const maxTs = timestamps[timestamps.length - 1] ?? minTs

  const points = pointsPool.map((event, index) => {
    const relation = relationReason(event, focusEvent, locale)
    const hash = stableHash(`${event.id}:${event.dedupeKey}:${index}`)
    const timeRatio = normalizedTime(event.stamp, minTs, maxTs)
    const jitterX = ((hash % 11) - 5) * 0.55
    const jitterY = (((hash >> 3) % 17) - 8) * 1.6
    const baseY =
      relation.score >= 3 ? 36 : relation.score >= 2 ? 56 : 20 + (((hash >> 2) % 42))

    return {
      eventId: event.id,
      title: event.title,
      stamp: event.stamp,
      x: clamp(8 + timeRatio * 42 + jitterX, 6, 54),
      y: clamp(baseY + jitterY, 12, 86),
      size:
        relation.score >= 4
          ? 'primary'
          : relation.score >= 2
            ? 'related'
            : 'ambient',
      reason: relation.reason,
      targetAnchorId: relation.targetAnchorId,
    } satisfies IncidentFieldPoint
  })

  const links: IncidentFieldLink[] = [
    {
      id: 'anchor-context-cluster',
      sourceKind: 'anchor',
      sourceId: 'context-anchor',
      targetId: 'cluster-anchor',
      weight: 'medium',
      label:
        locale === 'zh' ? '上下文压缩' : 'context compression',
    },
    {
      id: 'anchor-cluster-hypothesis',
      sourceKind: 'anchor',
      sourceId: 'cluster-anchor',
      targetId: 'hypothesis-anchor',
      weight: 'strong',
      label:
        locale === 'zh' ? '假设生成' : 'hypothesis build',
    },
  ]

  points.forEach((point) => {
    if (!point.targetAnchorId) {
      return
    }

    links.push({
      id: `point-${point.eventId}-${point.targetAnchorId}`,
      sourceKind: 'point',
      sourceId: point.eventId,
      targetId: point.targetAnchorId,
      weight:
        point.size === 'primary'
          ? 'strong'
          : point.size === 'related'
            ? 'medium'
            : 'soft',
      label: point.reason,
    })
  })

  return {
    points,
    anchors,
    links,
    runbook: buildRunbookDraft({
      ...input,
      selectedRecommendation:
        linkedSuggestion.recommendedActions[0] ?? selectedRecommendation,
      selectedScopeMeaning,
    }),
  }
}
