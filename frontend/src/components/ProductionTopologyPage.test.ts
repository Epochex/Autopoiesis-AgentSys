import { describe, expect, it } from 'vitest'
import { classifyAsset, projectProductionOverview } from './ProductionTopologyPage'

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

  it('classifies devices from router fingerprint first, then hostname/NetBIOS evidence', () => {
    const base = { segment: 's', active24h: true }
    expect(classifyAsset({ ...base, ip: '10.0.0.1', name: 'whatever', deviceClass: 'camera' } as never)).toBe('camera')
    expect(classifyAsset({ ...base, ip: '10.0.0.2', name: 'iPhone-de-Leon' } as never)).toBe('mobile')
    expect(classifyAsset({ ...base, ip: '10.0.0.3', name: 'Redmi-Note-12' } as never)).toBe('mobile')
    expect(classifyAsset({ ...base, ip: '10.0.0.4', name: 'DESKTOP-BDRJLL5' } as never)).toBe('workstation')
    expect(classifyAsset({ ...base, ip: '10.0.0.5', name: 'DSS-ONEBOX' } as never)).toBe('server')
    expect(classifyAsset({ ...base, ip: '10.0.0.6', name: '10.0.0.6', activity: { flows24h: 1, bytes24h: 0, denied24h: 0, peers24h: 0, observedOutboundServices: ['udp/137'] } } as never)).toBe('workstation')
    expect(classifyAsset({ ...base, ip: '10.0.0.7', name: '10.0.0.7' } as never)).toBe('unknown')
  })

  it('clusters a segment by device class and infers shared-destination relations', () => {
    const overview = {
      ok: true,
      mode: 'production_observed',
      observedAt: '2026-09-01T12:00:00+00:00',
      freshness: { lagSeconds: 1 },
      inventory: {
        knownAssets: 4,
        active24h: 4,
        segments: [{ cidr: '192.168.16.0/20', name: 'LACP', role: 'lan', assetCount: 4, active24h: 4 }],
        assets: [
          { ip: '192.168.16.10', name: 'DESKTOP-A', segment: '192.168.16.0/20', active24h: true, activity: { flows24h: 10, bytes24h: 0, denied24h: 0, peers24h: 1 } },
          { ip: '192.168.16.11', name: 'LAPTOP-B', segment: '192.168.16.0/20', active24h: true, activity: { flows24h: 9, bytes24h: 0, denied24h: 0, peers24h: 1 } },
          { ip: '192.168.16.12', name: 'iPhone-C', segment: '192.168.16.0/20', active24h: true, activity: { flows24h: 3, bytes24h: 0, denied24h: 0, peers24h: 1 } },
          { ip: '192.168.16.13', name: 'IPC-D', segment: '192.168.16.0/20', active24h: true, identity: { vendor: 'Dahua', type: 'IP Camera' }, deviceClass: 'camera', activity: { flows24h: 2, bytes24h: 0, denied24h: 0, peers24h: 1 } },
        ],
      },
      changes: [],
      crossSegment: { records: [
        { source: '192.168.16.10', destination: '192.168.2.1', sourceSegment: '192.168.16.0/20', destinationSegment: '192.168.2.0/24', service: 'DNS', port: 53, action: 'deny', flows: 20, lastSeenAt: 't' },
        { source: '192.168.16.11', destination: '192.168.2.1', sourceSegment: '192.168.16.0/20', destinationSegment: '192.168.2.0/24', service: 'DNS', port: 53, action: 'deny', flows: 5, lastSeenAt: 't' },
      ] },
      riskFusion: [],
      externalSources: [],
      cases: [],
      funnel: { facts: 1, security_events: 0, alerts: 0, cases: 0 },
    } as Parameters<typeof projectProductionOverview>[0]

    const graph = projectProductionOverview(overview).graphs['192.168.16.0/20']
    const classHulls = graph.clusters.filter((cluster) => cluster.id.startsWith('class-'))
    expect(classHulls.map((cluster) => cluster.role).sort()).toEqual(['camera', 'mobile', 'workstation'])
    expect(classHulls.find((cluster) => cluster.role === 'workstation')?.members.sort()).toEqual(['192.168.16.10', '192.168.16.11'])
    expect(graph.stats.roles).toEqual({ workstation: 2, mobile: 1, camera: 1 })
    expect(graph.stats.vendors).toEqual({ Dahua: 1 })
    const inferred = graph.edges.filter((edge) => !edge.observed)
    expect(inferred).toHaveLength(1)
    expect([inferred[0].src, inferred[0].dst].sort()).toEqual(['192.168.16.10', '192.168.16.11'])
    expect(inferred[0].evidence).toContain('共同目的 192.168.2.1')
    expect(graph.stats.observedEdges).toBe(graph.edges.length - 1)
    for (const device of graph.devices) {
      expect(Math.abs(device.x)).toBeLessThan(1)
      expect(Math.abs(device.y)).toBeLessThan(1)
    }
  })
})
