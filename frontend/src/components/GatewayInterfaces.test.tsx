import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { GatewayInterfaces, type GatewayDevice } from './GatewayInterfaces'

const device: GatewayDevice = {
  id: 'serial-1', name: 'FortiGate FG100E', serial: 'serial-1', interfaces: [
    { id: 'wan1', name: 'wan1', role: 'wan', status: 'up', members: [], addresses: [
      { ip: '77.236.99.122', cidr: '77.236.99.122/29', network: '77.236.99.120/29', public: true },
      { ip: '77.236.99.125', cidr: '77.236.99.125/29', network: '77.236.99.120/29', public: true },
    ] },
    { id: 'LACP', name: 'LACP', role: 'lan', kind: 'aggregate', status: 'up', members: ['port3', 'port4'], addresses: [
      { ip: '192.168.16.1', cidr: '192.168.16.1/20', network: '192.168.16.0/20', public: false },
    ] },
    { id: 'port6', name: 'port6', role: 'lan', status: 'down', members: [], addresses: [] },
  ],
}

describe('in-canvas gateway ownership', () => {
  it('groups secondary addresses under one interface and one configured network', () => {
    const html = renderToStaticMarkup(<svg><GatewayInterfaces device={device} lang="zh" subnets={[]} onSelect={() => {}} onSubnet={() => {}} /></svg>)
    expect(html.match(/class="gateway-interface-node"/g)).toHaveLength(2)
    expect(html).toContain('77.236.99.122/29')
    expect(html).toContain('77.236.99.125/29')
    expect(html.match(/77\.236\.99\.120\/29/g)).toHaveLength(1)
    expect(html).not.toContain('animateMotion')
    expect(html).toContain('配置网段 · 暂无终端观测')
  })

  it('offers subnet drilldown only for a matching observed interface/network', () => {
    const html = renderToStaticMarkup(<svg><GatewayInterfaces device={device} selected="LACP" lang="zh" subnets={[{ cidr: '192.168.16.0/20', intf: 'LACP', hosts: 4, flows: 0, accept: 0 }]} onSelect={() => {}} onSubnet={() => {}} /></svg>)
    expect(html).toContain('aria-label="展开网段 192.168.16.0/20"')
    expect(html).toContain('4 台已知设备')
    expect(html).toContain('成员: port3 + port4')
    expect(html.match(/gateway-relation selected/g)).toHaveLength(1)
  })
})
