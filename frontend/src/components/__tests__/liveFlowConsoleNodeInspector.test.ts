import { describe, expect, it } from 'vitest'
import { buildNodeInspectorSurfaceModel } from '../liveFlowConsoleNodeInspector'

describe('buildNodeInspectorSurfaceModel', () => {
  it('condenses node data into a compact inspector surface model', () => {
    const model = buildNodeInspectorSurfaceModel({
      title: 'Trigger',
      role: 'raw event lock',
      state: 'focused review',
      token: 'GMT+2 23:02:01',
      caption: 'service lane locked',
      facts: [
        { label: 'service', value: 'udp/28689' },
        { label: 'device', value: 'a8:16:9d:ed:56:da' },
        { label: 'identity', value: '192.168.16.48' },
        { label: 'scope', value: 'alert' },
        { label: 'overflow', value: 'ignored' },
      ],
      detail:
        'Deny Burst just marked udp/28689 as the current incident. The rest of the chain now explains that path.',
      transition:
        'The locked tuple is handed to aggregate for repeat and spread checking.',
      nextTitle: 'Aggregate',
      sources: [
        {
          label: 'deny threshold',
          section: 'rule_context',
          field: 'metrics.deny_count',
          value: '200/200 in 60s',
          reason: 'deterministic threshold hit',
        },
        {
          label: 'service',
          section: 'topology_context',
          field: 'service',
          value: 'udp/28689',
          reason: 'alert service lane',
        },
      ],
    })

    expect(model.caption).toBe('service lane locked')
    expect(model.readingLine).toBe(
      'Deny Burst just marked udp/28689 as the current incident',
    )
    expect(model.localEvidence).toHaveLength(3)
    expect(model.transitionNote).toBe(
      'The locked tuple is handed to aggregate for repeat and spread checking',
    )
    expect(model.nextDependency).toBe('Aggregate')
    expect(model.basisMarkers[0].ref).toBe('rule_context.metrics.deny_count')
  })
})
