import { describe, expect, it } from 'vitest'
import type { SuggestionRecord } from '../../types'
import {
  buildIncidentConvergenceModel,
  type IncidentQueueLike,
} from '../liveFlowConsoleEventField'

function makeSuggestion(): SuggestionRecord {
  return {
    id: 'sg-1',
    alertId: 'alert-1',
    suggestionTs: '2026-04-05T18:03:18.303384+00:00',
    scope: 'alert',
    ruleId: 'deny_burst_v1',
    severity: 'warning',
    priority: 'medium',
    summary: 'LEON-PC needs attention on udp/10004',
    context: {
      service: 'udp/10004',
      srcDeviceKey: '20:7b:d2:ac:75:4e',
      clusterSize: 1,
      clusterWindowSec: 600,
      clusterFirstAlertTs: '2026-04-05T18:00:00+00:00',
      clusterLastAlertTs: '2026-04-05T18:03:18+00:00',
      clusterSampleAlertIds: ['alert-1'],
      recentSimilar1h: 4,
      provider: 'template',
    },
    evidenceBundle: {
      topology: {
        srcip: '192.168.16.152',
        dstip: '192.168.16.48',
        zone: 'lan',
      },
      device: {
        device_name: 'LEON-PC',
        vendor: 'fortinet',
      },
      change: {
        suspected_change: false,
      },
      historical: {
        similar_count_1h: 4,
      },
    },
    hypotheses: [
      'The repeated deny pattern is concentrated on one local broadcast tuple.',
    ],
    hypothesisSet: {
      setId: 'hs-1',
      primaryHypothesisId: 'h-1',
      suggestionScope: 'alert',
      items: [
        {
          hypothesisId: 'h-1',
          rank: 1,
          statement:
            'The repeated deny pattern is concentrated on one local broadcast tuple.',
          confidenceScore: 0.82,
          confidenceLabel: 'medium',
          supportEvidenceRefs: ['topology_context.service'],
          contradictEvidenceRefs: [],
          missingEvidenceRefs: [],
          nextBestAction: 'Review the last 15 minutes in ClickHouse for the same tuple.',
          reviewState: 'pending_review',
        },
      ],
      summary: {
        totalHypotheses: 1,
        directRefCount: 1,
        supportingRefCount: 1,
        contradictoryRefCount: 0,
        missingRefCount: 0,
      },
    },
    recommendedActions: [
      'Review the last 15 minutes in ClickHouse for the same tuple.',
    ],
    reviewVerdict: {
      verdictId: 'rv-1',
      suggestionScope: 'alert',
      verdictStatus: 'operator_review',
      recommendedDisposition: 'project_with_operator_boundary',
      approvalRequired: true,
      blockingIssues: [],
      checks: {
        evidenceSufficiency: { status: 'sufficient', detail: 'direct=5, supporting=1, missing=0' },
        temporalFreshness: { status: 'fresh', detail: 'control-loop snapshot attached' },
        topologyConsistency: { status: 'consistent', detail: 'path and service attached' },
        overreachRisk: { status: 'guarded', detail: 'operator boundary retained' },
        remediationExecutability: { status: 'bounded', detail: 'template provider path' },
        rollbackReadiness: { status: 'ready', detail: 'rollback outline attached' },
      },
      reviewSummary: 'operator review retained for this suggestion',
    },
    runbookDraft: {
      planId: 'rb-1',
      planScope: 'alert',
      planStatus: 'draft_ready',
      title: 'Runbook draft for udp/10004 on LEON-PC',
      applicability: {
        ruleId: 'deny_burst_v1',
        service: 'udp/10004',
        pathSignature: 'lan->lan',
      },
      hypothesisRef: 'h-1',
      hypothesisStatement:
        'The repeated deny pattern is concentrated on one local broadcast tuple.',
      prechecks: ['event window 18:00:00 - 18:03:18', 'cluster gate 1/3 in 600s'],
      operatorActions: ['Review the last 15 minutes in ClickHouse for the same tuple.'],
      boundaries: ['guidance only'],
      rollbackGuidance: ['rerun the tuple check if the path widens beyond the current slice'],
      approvalBoundary: {
        approvalRequired: true,
        executionMode: 'human_gated',
        writePathAllowed: false,
      },
      evidenceRefs: ['topology_context.service'],
      changeSummary: {
        suspectedChange: false,
        changeRefs: [],
      },
    },
    confidence: 0.82,
    confidenceLabel: 'medium',
    confidenceReason: 'threshold hit plus single-path concentration',
    projectionBasis: {},
    timeline: [],
    stageTelemetry: [],
  }
}

function makeQueue(): IncidentQueueLike[] {
  return [
    {
      id: 'incident-a',
      dedupeKey: 'deny_burst::alert::udp/10004::20:7b:d2:ac:75:4e',
      stamp: '2026-04-05T17:48:00+00:00',
      title: 'older tuple refresh',
      service: 'udp/10004',
      device: 'LEON-PC',
      scope: 'alert',
    },
    {
      id: 'incident-b',
      dedupeKey: 'deny_burst::alert::udp/10004::20:7b:d2:ac:75:4e',
      stamp: '2026-04-05T18:03:18+00:00',
      title: 'selected tuple',
      service: 'udp/10004',
      device: 'LEON-PC',
      scope: 'alert',
    },
    {
      id: 'incident-c',
      dedupeKey: 'deny_burst::alert::udp/5355::20:7b:d2:ac:75:4e',
      stamp: '2026-04-05T17:52:00+00:00',
      title: 'same device other service',
      service: 'udp/5355',
      device: 'LEON-PC',
      scope: 'alert',
    },
    {
      id: 'incident-d',
      dedupeKey: 'bytes_spike::alert::tcp/443::edge-node',
      stamp: '2026-04-05T17:55:00+00:00',
      title: 'ambient noise',
      service: 'tcp/443',
      device: 'edge-node',
      scope: 'alert',
    },
  ]
}

describe('buildIncidentConvergenceModel', () => {
  it('marks the active event as primary and links related incidents into anchors', () => {
    const queue = makeQueue()
    const model = buildIncidentConvergenceModel({
      locale: 'en',
      queueEvents: queue,
      activeEvent: queue[1],
      linkedSuggestion: makeSuggestion(),
      clusterGateValue: '1/3 in 600s',
      selectedInference:
        'The repeated deny pattern is concentrated on one local broadcast tuple.',
      selectedRecommendation:
        'Review the last 15 minutes in ClickHouse for the same tuple.',
      selectedWindowSummary: '18:00:00 - 18:03:18',
      selectedScopeMeaning:
        'The evidence is still concentrated on one service/device path.',
      selectedRefreshSummary: 'merged 2 suggestion refreshes',
    })

    const primaryPoint = model.points.find((point) => point.eventId === 'incident-b')
    const sameKeyPoint = model.points.find((point) => point.eventId === 'incident-a')
    const sameDevicePoint = model.points.find((point) => point.eventId === 'incident-c')
    const ambientPoint = model.points.find((point) => point.eventId === 'incident-d')

    expect(primaryPoint?.size).toBe('primary')
    expect(primaryPoint?.targetAnchorId).toBe('hypothesis-anchor')
    expect(sameKeyPoint?.targetAnchorId).toBe('cluster-anchor')
    expect(sameDevicePoint?.targetAnchorId).toBe('context-anchor')
    expect(ambientPoint?.targetAnchorId).toBeNull()
    expect(
      model.links.some(
        (link) =>
          link.sourceKind === 'point' &&
          link.sourceId === 'incident-a' &&
          link.targetId === 'cluster-anchor',
      ),
    ).toBe(true)
  })

  it('builds a runbook draft with prechecks, action lines, and boundaries', () => {
    const queue = makeQueue()
    const model = buildIncidentConvergenceModel({
      locale: 'en',
      queueEvents: queue,
      activeEvent: queue[1],
      linkedSuggestion: makeSuggestion(),
      clusterGateValue: '1/3 in 600s',
      selectedInference:
        'The repeated deny pattern is concentrated on one local broadcast tuple.',
      selectedRecommendation:
        'Review the last 15 minutes in ClickHouse for the same tuple.',
      selectedWindowSummary: '18:00:00 - 18:03:18',
      selectedScopeMeaning:
        'The evidence is still concentrated on one service/device path.',
      selectedRefreshSummary: 'merged 2 suggestion refreshes',
    })

    expect(model.runbook.title).toBe('Runbook draft for udp/10004 on LEON-PC')
    expect(model.runbook.prechecks[0]).toContain('event window 18:00:00 - 18:03:18')
    expect(model.runbook.operatorActions[0]).toContain('ClickHouse')
    expect(model.runbook.boundaries[0]).toContain('guidance only')
    expect(model.runbook.evidenceLabels).toEqual(
      expect.arrayContaining(['topology', 'device', 'change', 'historical']),
    )
  })

  it('falls back cleanly when a historical suggestion has no structured runbook draft', () => {
    const queue = makeQueue()
    const suggestion = {
      ...makeSuggestion(),
      runbookDraft: undefined,
    } as unknown as SuggestionRecord

    const model = buildIncidentConvergenceModel({
      locale: 'en',
      queueEvents: queue,
      activeEvent: queue[1],
      linkedSuggestion: suggestion as SuggestionRecord,
      clusterGateValue: '1/3 in 600s',
      selectedInference:
        'The repeated deny pattern is concentrated on one local broadcast tuple.',
      selectedRecommendation:
        'Review the last 15 minutes in ClickHouse for the same tuple.',
      selectedWindowSummary: '18:00:00 - 18:03:18',
      selectedScopeMeaning:
        'The evidence is still concentrated on one service/device path.',
      selectedRefreshSummary: 'merged 2 suggestion refreshes',
    })

    expect(model.runbook.title).toBe('Runbook draft')
    expect(model.runbook.operatorActions[0]).toContain('ClickHouse')
    expect(model.runbook.boundaries[0]).toContain('guidance only')
  })
})
