import { describe, expect, it } from 'vitest'
import { projectProductionOverview } from './ProductionTopologyPage'

describe('production topology projection', () => {
  it('projects current assets and observed boundary records without held-out labels', () => {
    const overview = {
      ok: true,
      mode: 'production_observed',
      observedAt: '2026-09-01T12:00:00+00:00',
      freshness: { lagSeconds: 1 },
      inventory: {
        knownAssets: 2,
        active24h: 2,
        segments: [
          { cidr: '192.168.1.0/24', name: 'fortilink', role: 'lan', assetCount: 1, active24h: 1 },
          { cidr: '192.168.16.0/20', name: 'LACP', role: 'lan', assetCount: 1, active24h: 1 },
        ],
        assets: [
          { ip: '192.168.1.20', name: 'camera', segment: '192.168.1.0/24', active24h: true, activity: { flows24h: 8, bytes24h: 100, denied24h: 1, peers24h: 1 } },
          { ip: '192.168.16.56', name: 'workstation', segment: '192.168.16.0/20', active24h: true, activity: { flows24h: 10, bytes24h: 200, denied24h: 2, peers24h: 1 } },
        ],
      },
      changes: [{ id: 'change-1', asset: '192.168.16.56', severity: 'high', title: 'activity changed' }],
      crossSegment: { records: [{ source: '192.168.16.56', destination: '192.168.1.20', sourceSegment: '192.168.16.0/20', destinationSegment: '192.168.1.0/24', service: 'DNS', port: 53, action: 'deny', flows: 7, lastSeenAt: '2026-09-01T11:59:59Z' }] },
      riskFusion: [{ asset: '192.168.16.56', name: 'workstation', segment: '192.168.16.0/20', severity: 'high', reasons: ['deny rate changed'], caseIds: ['case-1'] }],
      externalSources: [{ ip: '203.0.113.7', events: 5, eventTypes: ['admin_login_failed'], ports: [443], lastSeenAt: '2026-09-01T11:59:58Z' }],
      cases: [{ caseId: 'case-1', status: 'investigating', severity: 'high', subject: '192.168.16.56', title: 'case' }],
      funnel: { facts: 100, security_events: 5, alerts: 1, cases: 1 },
    } as Parameters<typeof projectProductionOverview>[0]

    const projection = projectProductionOverview(overview)

    expect(projection.topology.subnets.map((item) => item.cidr)).toEqual(['192.168.1.0/24', '192.168.16.0/20'])
    expect(projection.graphs['192.168.16.0/20'].edges).toHaveLength(1)
    expect(projection.graphs['192.168.16.0/20'].edges[0].observed).toBe(true)
    expect(projection.context.profiles['192.168.16.56'].outbound[0].ip).toBe('192.168.1.20')
    expect(projection.context.activeCases).toBe(1)
    expect(JSON.stringify(projection)).not.toMatch(/R230|192\.168\.1\.23|2026-06-1[67]/)
  })
})
