import { describe, expect, it } from 'vitest'
import { buildIncidentNarrative } from './incident-narrative'

describe('incident narrative', () => {
  it('turns one recalled procedure into a six-stage causal explanation', () => {
    const narrative = buildIncidentNarrative({
      caseRecord: {
        caseId: 'case-warm', status: 'resolved', severity: 'high',
        subject: 'autopoiesis-acceptance-memory-one.service', service: 'service_health',
        ruleId: 'failed_units', summary: '服务失败', firstSeenAt: '2026-09-10T10:00:00Z',
        lastSeenAt: '2026-09-10T10:00:30Z', updatedAt: '2026-09-10T10:00:30Z',
        occurrenceCount: 1, timeline: [],
        sourcePayload: { dataClassification: 'controlled_test', demoScenario: 'memory_trajectory' },
      },
      session: {
        session_id: 'session-warm', case_id: 'case-warm', subject: 'autopoiesis-acceptance-memory-one.service',
        evidence: [{ evidence_id: 'ev-1', command: 'systemctl --failed --no-legend', output: 'failed unit', ok: true }],
        memory_intervention: {
          memory_applied: true, confirmed: true, memory_ids: ['proc-one'],
          no_memory_shadow_order: ['systemctl', 'df', 'curl', 'free'],
          executed_order: ['systemctl'], saved_probe_count: 3,
        },
        retrieval_results: [{
          item_id: 'proc-one', kind: 'indexed_memory', summary: '已验证服务失败检查方法',
          selected_for_context: true, source_trace_ids: ['session-cold'], matched_on: ['subject', 'family'],
        }],
        trace_events: [{ kind: 'memory_candidates_ranked' }, { kind: 'memory_shortcut' }, { kind: 'business_decision_recorded' }],
      },
      chain: null,
      lang: 'zh',
    })

    expect(narrative.stages.map(stage => stage.id)).toEqual([
      'detect', 'confirm', 'investigate', 'memory', 'action', 'verify',
    ])
    expect(narrative.stages.find(stage => stage.id === 'memory')).toMatchObject({
      state: 'complete',
      title: '记忆介入',
    })
    expect(narrative.memory).toMatchObject({
      applied: true,
      memoryId: 'proc-one',
      sourceSessionId: 'session-cold',
      baselineChecks: 4,
      executedChecks: 1,
      savedChecks: 3,
    })
  })
})
