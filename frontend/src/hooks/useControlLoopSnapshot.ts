import { startTransition, useEffect, useState } from 'react'
import { controlLoopSnapshot as fallbackSnapshot } from '../data/controlLoopModel'
import type {
  HypothesisSet,
  ReviewVerdict,
  ControlLoopSnapshot,
  ControlLoopStreamDelta,
  ControlLoopStreamEnvelope,
  RunbookDraft,
  SuggestionRecord,
} from '../types'

export type ControlLoopConnectionState =
  | 'connecting'
  | 'live'
  | 'degraded'
  | 'fallback'

const SNAPSHOT_ENDPOINT = '/api/control-loop/snapshot'
const STREAM_ENDPOINT = '/api/control-loop/stream'
const SNAPSHOT_TIMEOUT_MS = 25_000
const STREAM_HANDSHAKE_TIMEOUT_MS = 8_000

function stringList(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

function normalizeRunbookDraft(suggestion: SuggestionRecord): RunbookDraft {
  const draft = suggestion.runbookDraft as Partial<RunbookDraft> | undefined
  const applicability = draft?.applicability
  const approvalBoundary = draft?.approvalBoundary
  const changeSummary = draft?.changeSummary

  return {
    planId:
      typeof draft?.planId === 'string' && draft.planId.trim().length > 0
        ? draft.planId
        : 'fallback-runbook-draft',
    planScope: suggestion.scope,
    planStatus:
      typeof draft?.planStatus === 'string' && draft.planStatus.trim().length > 0
        ? draft.planStatus
        : 'draft_ready',
    title:
      typeof draft?.title === 'string' && draft.title.trim().length > 0
        ? draft.title
        : 'Runbook draft',
    applicability: {
      ruleId:
        typeof applicability?.ruleId === 'string' && applicability.ruleId.trim().length > 0
          ? applicability.ruleId
          : suggestion.ruleId,
      service:
        typeof applicability?.service === 'string' && applicability.service.trim().length > 0
          ? applicability.service
          : suggestion.context.service,
      pathSignature:
        typeof applicability?.pathSignature === 'string'
          ? applicability.pathSignature
          : '',
    },
    hypothesisRef:
      typeof draft?.hypothesisRef === 'string' ? draft.hypothesisRef : '',
    hypothesisStatement:
      typeof draft?.hypothesisStatement === 'string'
        ? draft.hypothesisStatement
        : '',
    prechecks: stringList(draft?.prechecks),
    operatorActions:
      stringList(draft?.operatorActions).length > 0
        ? stringList(draft?.operatorActions)
        : stringList(suggestion.recommendedActions),
    boundaries:
      stringList(draft?.boundaries).length > 0
        ? stringList(draft?.boundaries)
        : ['guidance only'],
    rollbackGuidance: stringList(draft?.rollbackGuidance),
    approvalBoundary: {
      approvalRequired: approvalBoundary?.approvalRequired === true,
      executionMode:
        typeof approvalBoundary?.executionMode === 'string' &&
        approvalBoundary.executionMode.trim().length > 0
          ? approvalBoundary.executionMode
          : 'human_gated',
      writePathAllowed: approvalBoundary?.writePathAllowed === true,
    },
    evidenceRefs: stringList(draft?.evidenceRefs),
    changeSummary: {
      suspectedChange: changeSummary?.suspectedChange === true,
      changeRefs: stringList(changeSummary?.changeRefs),
    },
  }
}

function normalizeHypothesisSet(suggestion: SuggestionRecord): HypothesisSet {
  const set = suggestion.hypothesisSet as Partial<HypothesisSet> | undefined
  const items = Array.isArray(set?.items) ? set.items : []

  return {
    setId:
      typeof set?.setId === 'string' && set.setId.trim().length > 0
        ? set.setId
        : 'fallback-hypothesis-set',
    primaryHypothesisId:
      typeof set?.primaryHypothesisId === 'string'
        ? set.primaryHypothesisId
        : items[0]?.hypothesisId ?? '',
    suggestionScope: suggestion.scope,
    items,
    summary: {
      totalHypotheses:
        typeof set?.summary?.totalHypotheses === 'number'
          ? set.summary.totalHypotheses
          : items.length,
      directRefCount:
        typeof set?.summary?.directRefCount === 'number'
          ? set.summary.directRefCount
          : 0,
      supportingRefCount:
        typeof set?.summary?.supportingRefCount === 'number'
          ? set.summary.supportingRefCount
          : 0,
      contradictoryRefCount:
        typeof set?.summary?.contradictoryRefCount === 'number'
          ? set.summary.contradictoryRefCount
          : 0,
      missingRefCount:
        typeof set?.summary?.missingRefCount === 'number'
          ? set.summary.missingRefCount
          : 0,
    },
  }
}

function normalizeReviewVerdict(suggestion: SuggestionRecord): ReviewVerdict {
  const verdict = suggestion.reviewVerdict as Partial<ReviewVerdict> | undefined

  return {
    verdictId:
      typeof verdict?.verdictId === 'string' && verdict.verdictId.trim().length > 0
        ? verdict.verdictId
        : 'fallback-review-verdict',
    suggestionScope: suggestion.scope,
    verdictStatus:
      typeof verdict?.verdictStatus === 'string' && verdict.verdictStatus.trim().length > 0
        ? verdict.verdictStatus
        : 'operator_review',
    recommendedDisposition:
      typeof verdict?.recommendedDisposition === 'string' &&
      verdict.recommendedDisposition.trim().length > 0
        ? verdict.recommendedDisposition
        : 'project_with_operator_boundary',
    approvalRequired: verdict?.approvalRequired === true,
    blockingIssues: stringList(verdict?.blockingIssues),
    checks: {
      evidenceSufficiency: verdict?.checks?.evidenceSufficiency ?? {
        status: 'unknown',
        detail: 'legacy suggestion fallback',
      },
      temporalFreshness: verdict?.checks?.temporalFreshness ?? {
        status: 'unknown',
        detail: 'legacy suggestion fallback',
      },
      topologyConsistency: verdict?.checks?.topologyConsistency ?? {
        status: 'unknown',
        detail: 'legacy suggestion fallback',
      },
      overreachRisk: verdict?.checks?.overreachRisk ?? {
        status: 'unknown',
        detail: 'legacy suggestion fallback',
      },
      remediationExecutability: verdict?.checks?.remediationExecutability ?? {
        status: 'unknown',
        detail: 'legacy suggestion fallback',
      },
      rollbackReadiness: verdict?.checks?.rollbackReadiness ?? {
        status: 'unknown',
        detail: 'legacy suggestion fallback',
      },
    },
    reviewSummary:
      typeof verdict?.reviewSummary === 'string' && verdict.reviewSummary.trim().length > 0
        ? verdict.reviewSummary
        : suggestion.confidenceReason,
  }
}

function normalizeSuggestionRecord(suggestion: SuggestionRecord): SuggestionRecord {
  return {
    ...suggestion,
    recommendedActions: stringList(suggestion.recommendedActions),
    hypotheses: stringList(suggestion.hypotheses),
    evidenceBundle: {
      topology: suggestion.evidenceBundle?.topology ?? {},
      device: suggestion.evidenceBundle?.device ?? {},
      change: suggestion.evidenceBundle?.change ?? {},
      historical: suggestion.evidenceBundle?.historical ?? {},
    },
    hypothesisSet: normalizeHypothesisSet(suggestion),
    runbookDraft: normalizeRunbookDraft(suggestion),
    reviewVerdict: normalizeReviewVerdict(suggestion),
    projectionBasis: suggestion.projectionBasis ?? {},
    timeline: suggestion.timeline ?? [],
    stageTelemetry: suggestion.stageTelemetry ?? [],
  }
}

function normalizeSnapshot(snapshot: ControlLoopSnapshot): ControlLoopSnapshot {
  return {
    ...snapshot,
    suggestions: snapshot.suggestions.map((suggestion) =>
      normalizeSuggestionRecord(suggestion),
    ),
  }
}

function activeSuggestion(snapshot: ControlLoopSnapshot) {
  return (
    snapshot.suggestions.find(
      (suggestion) => suggestion.id === snapshot.defaultSuggestionId,
    ) ??
    snapshot.suggestions[0] ??
    null
  )
}

function snapshotIntegrityIssue(snapshot: ControlLoopSnapshot) {
  const suggestion = activeSuggestion(snapshot)

  if (!suggestion) {
    return 'Live snapshot returned no suggestion slice, so the console stayed on the guarded local model.'
  }

  const missingTimeline = (suggestion.timeline?.length ?? 0) === 0
  const missingTelemetry = (suggestion.stageTelemetry?.length ?? 0) === 0

  if (missingTimeline || missingTelemetry) {
    return `Live snapshot for ${suggestion.id} is missing timeline/stageTelemetry, so the console kept the last known good slice instead of swapping into drift.`
  }

  return null
}

export function useControlLoopSnapshot() {
  const [snapshot, setSnapshot] = useState<ControlLoopSnapshot>(
    normalizeSnapshot(fallbackSnapshot),
  )
  const [latestDelta, setLatestDelta] = useState<ControlLoopStreamDelta | null>(null)
  const [connectionState, setConnectionState] =
    useState<ControlLoopConnectionState>('connecting')
  const [transportIssue, setTransportIssue] = useState<string | null>(null)

  useEffect(() => {
    let isMounted = true
    let didTimeout = false
    const controller = new AbortController()
    const timeoutId = window.setTimeout(() => {
      didTimeout = true
      controller.abort()
    }, SNAPSHOT_TIMEOUT_MS)

    async function hydrate() {
      try {
        const response = await fetch(SNAPSHOT_ENDPOINT, {
          signal: controller.signal,
          headers: { Accept: 'application/json' },
        })
        if (!response.ok) {
          throw new Error(`snapshot request failed: ${response.status}`)
        }
        const nextSnapshot = normalizeSnapshot(
          (await response.json()) as ControlLoopSnapshot,
        )
        const issue = snapshotIntegrityIssue(nextSnapshot)
        if (issue) {
          setTransportIssue(issue)
          setConnectionState('degraded')
          return
        }
        startTransition(() => setSnapshot(nextSnapshot))
        setTransportIssue(null)
        setConnectionState('live')
      } catch {
        if (!isMounted) {
          return
        }
        if (!controller.signal.aborted) {
          setConnectionState('fallback')
          setTransportIssue(
            'Initial live snapshot request failed, so the console stayed on the guarded local model.',
          )
        } else if (didTimeout) {
          setConnectionState('fallback')
          setTransportIssue(
            `Initial live snapshot request timed out after ${SNAPSHOT_TIMEOUT_MS / 1000}s, so the console stayed on the guarded local model.`,
          )
        }
      } finally {
        window.clearTimeout(timeoutId)
      }
    }

    hydrate()
    return () => {
      isMounted = false
      controller.abort()
      window.clearTimeout(timeoutId)
    }
  }, [])

  useEffect(() => {
    let isMounted = true
    let retryTimer: number | undefined
    let eventSource: EventSource | undefined
    let handshakeTimer: number | undefined

    const connect = () => {
      if (!isMounted) {
        return
      }
      setConnectionState((previousState) =>
        previousState === 'live' ? 'live' : 'connecting',
      )
      eventSource = new EventSource(STREAM_ENDPOINT)
      handshakeTimer = window.setTimeout(() => {
        eventSource?.close()
        if (!isMounted) {
          return
        }
        setTransportIssue(
          `Live stream did not emit snapshot/delta/heartbeat inside ${STREAM_HANDSHAKE_TIMEOUT_MS / 1000}s, so the console kept the last known good slice.`,
        )
        setConnectionState((previousState) =>
          previousState === 'live' ? 'degraded' : 'fallback',
        )
        retryTimer = window.setTimeout(connect, 5000)
      }, STREAM_HANDSHAKE_TIMEOUT_MS)

      const handleEnvelope = (
        event: MessageEvent<string>,
        expectedType: ControlLoopStreamEnvelope['type'],
      ) => {
        try {
          const envelope = JSON.parse(event.data) as ControlLoopStreamEnvelope
          if (envelope.type !== expectedType) {
            return
          }
          if (!isMounted) {
            return
          }
          if (handshakeTimer) {
            window.clearTimeout(handshakeTimer)
            handshakeTimer = undefined
          }
          if (envelope.type === 'heartbeat') {
            return
          }
          const nextSnapshot = normalizeSnapshot(envelope.snapshot)
          const issue = snapshotIntegrityIssue(nextSnapshot)
          if (issue) {
            setTransportIssue(issue)
            setConnectionState('degraded')
            return
          }
          startTransition(() => setSnapshot(nextSnapshot))
          if (envelope.type === 'delta') {
            setLatestDelta(envelope.delta)
          } else {
            setLatestDelta(null)
          }
          setTransportIssue(null)
          setConnectionState('live')
        } catch {
          if (isMounted) {
            setTransportIssue(
              'Live stream emitted an unreadable envelope, so the console kept the last known good slice.',
            )
            setConnectionState('degraded')
          }
        }
      }

      eventSource.addEventListener('snapshot', (event) =>
        handleEnvelope(event as MessageEvent<string>, 'snapshot'),
      )
      eventSource.addEventListener('delta', (event) =>
        handleEnvelope(event as MessageEvent<string>, 'delta'),
      )
      eventSource.addEventListener('heartbeat', (event) =>
        handleEnvelope(event as MessageEvent<string>, 'heartbeat'),
      )

      eventSource.onerror = () => {
        if (handshakeTimer) {
          window.clearTimeout(handshakeTimer)
          handshakeTimer = undefined
        }
        eventSource?.close()
        if (!isMounted) {
          return
        }
        setTransportIssue(
          'Live stream connection degraded, so the console kept the last known good slice while retrying.',
        )
        setConnectionState((previousState) =>
          previousState === 'live' ? 'degraded' : 'fallback',
        )
        retryTimer = window.setTimeout(connect, 5000)
      }
    }

    connect()

    return () => {
      isMounted = false
      if (handshakeTimer) {
        window.clearTimeout(handshakeTimer)
      }
      eventSource?.close()
      if (retryTimer) {
        window.clearTimeout(retryTimer)
      }
    }
  }, [])

  return { snapshot, latestDelta, connectionState, transportIssue }
}
