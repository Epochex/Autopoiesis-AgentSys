import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { SubnetGraph } from '../types'
import { SubnetGraphLayer } from './SubnetGraph'

const graph: SubnetGraph = {
  cidr: '192.168.16.0/20',
  devices: [
    { ip: '192.168.16.49', name: 'host', mac: null, vendor: 'unknown', os: null, role: 'workstation', intf: 'LACP', flows: 10, deny: 0, accept: 10, leases: 0, topPorts: [], threat: 'ok', seenBy: 'traffic', x: -0.4, y: 0 },
    { ip: '192.168.16.1', name: 'gateway', mac: null, vendor: 'FortiGate', os: null, role: 'gateway-service', intf: 'LACP', flows: 10, deny: 0, accept: 10, leases: 0, topPorts: [], threat: 'ok', seenBy: 'traffic', x: 0.4, y: 0 },
  ],
  edges: [
    { src: '192.168.16.49', dst: '192.168.16.1', kind: 'flow', weight: 2, hits: 12, evidence: 'blocked DNS', observed: true, outcome: 'blocked' },
    { src: '192.168.16.49', dst: '192.168.16.1', kind: 'codst', weight: 1, hits: 2, evidence: 'shared destination', observed: false },
  ],
  clusters: [],
  anomalies: [],
  coverage: { acceptedCrossSegment: true, sameSegment: false, sameSegmentRequires: 'NetFlow/IPFIX' },
  stats: { devices: 2, withTraffic: 2, dhcpOnly: 0, edges: 2, observedEdges: 1, deny: 0, roles: {}, vendors: {} },
}

describe('subnet traffic relations', () => {
  it('draws measured directional traffic in the resting map and keeps inferred links on demand', () => {
    const html = renderToStaticMarkup(<svg><SubnetGraphLayer
      graph={graph} analysis={null} center={{ x: 500, y: 300 }} rx={300} ry={200} vbw={1000} vbh={600}
      lang="zh" hoverIp={null} selectedIp={null} focusIp={null} profile={null} marks={{}} showPanel={false}
      onHover={() => {}} onFocus={() => {}} onAnalyze={() => {}} onCloseAnalysis={() => {}}
    /></svg>)
    expect(html).toContain('sg-edge k-flow obs outcome-blocked')
    expect(html).toContain('marker-end="url(#sg-192-168-16-0-20-blocked)"')
    expect(html).not.toContain('sg-edge k-codst inf')
    expect(html).toContain('采集边界')
  })
})
