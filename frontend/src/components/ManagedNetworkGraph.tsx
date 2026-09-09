import { useEffect, useMemo, useRef, useState } from 'react'
import type { ManagedDevice, ManagedInterface, ObservedAsset } from './network-model'
import { endpointDevices } from './network-model'
import type { Lang } from '../i18n'
import './managed-network-graph.css'

export function ManagedNetworkGraph({ devices, assets, lang, onDevice, onInterface, onEndpoint }: {
  devices: ManagedDevice[]; assets: ObservedAsset[]; lang: Lang
  onDevice: (device: ManagedDevice) => void
  onInterface: (device: ManagedDevice, intf: ManagedInterface) => void
  onEndpoint: (ip: string, segment: string) => void
}) {
  const zh = lang === 'zh'
  const [all, setAll] = useState(false)
  const [view, setView] = useState({ x: 0, y: 0, scale: 1 })
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  const svg = useRef<SVGSVGElement>(null)
  useEffect(() => {
    const node = svg.current
    if (!node) return
    const wheel = (event: WheelEvent) => {
      event.preventDefault()
      setView(v => ({ ...v, scale: Math.max(.45, Math.min(3, v.scale * (event.deltaY < 0 ? 1.08 : 1 / 1.08))) }))
    }
    node.addEventListener('wheel', wheel, { passive: false })
    return () => node.removeEventListener('wheel', wheel)
  }, [])
  const hosts = useMemo(() => endpointDevices(assets, devices), [assets, devices])
  const zoom = (factor: number) => setView(v => ({ ...v, scale: Math.max(.45, Math.min(3, v.scale * factor)) }))
  const activate = (fn: () => void) => { if (!drag.current?.moved) fn() }
  return <div className="managed-net">
    <svg ref={svg} viewBox="0 0 1600 1000" aria-label={zh ? '设备接口交互拓扑' : 'Interactive device interface topology'}
      onPointerDown={event => { if ((event.target as Element).closest('[role=button]')) return; drag.current = { x: event.clientX, y: event.clientY, moved: false }; event.currentTarget.setPointerCapture(event.pointerId) }}
      onPointerMove={event => { const d = drag.current; if (!d) return; const ratio = 1600 / (svg.current?.getBoundingClientRect().width || 1600); const dx = event.clientX - d.x, dy = event.clientY - d.y; if (Math.abs(dx) + Math.abs(dy) > 2) d.moved = true; setView(v => ({ ...v, x: v.x + dx * ratio, y: v.y + dy * ratio })); d.x = event.clientX; d.y = event.clientY }}
      onPointerUp={() => { drag.current = null }} onPointerCancel={() => { drag.current = null }}>
      <g transform={`translate(${800 + view.x} ${500 + view.y}) scale(${view.scale}) translate(-800 -500)`}>
        {devices.map((device, deviceIndex) => {
          const cx = 800 + deviceIndex * 1600, cy = 500
          const interfaces = device.interfaces.filter(i => i.addresses.length || all)
          const clusterCounts = new Map<string, number>()
          const placements = hosts.flatMap(host => {
            const memberships = interfaces.filter(intf => host.addresses.some(a => a.activity?.sourceInterface === intf.name || intf.addresses.some(ip => ip.network === a.segment)))
            const primary = memberships[0]
            if (!primary) return []
            const ordinal = clusterCounts.get(primary.id) ?? 0
            clusterCounts.set(primary.id, ordinal + 1)
            const angle = -Math.PI / 2 + interfaces.indexOf(primary) * Math.PI * 2 / interfaces.length
            const theta = ordinal * 2.399963, radius = 30 + Math.sqrt(ordinal) * 10
            return [{ host, memberships, primary,
              x: cx + Math.cos(angle) * 500 + Math.cos(theta) * radius,
              y: cy + Math.sin(angle) * 330 + Math.sin(theta) * radius }]
          })
          return <g key={device.id} data-device-id={device.id}>
            {interfaces.map((intf, index) => {
              const angle = -Math.PI / 2 + index * Math.PI * 2 / interfaces.length
              const x = cx + Math.cos(angle) * 500, y = cy + Math.sin(angle) * 330
              const attached = placements.filter(p => p.memberships.some(i => i.id === intf.id))
              const clusterRadius = attached.length ? 40 + Math.sqrt(attached.length) * 10 : 25
              return <g key={intf.id}>
                <path className="managed-net-ownership" d={`M${cx},${cy} Q${cx},${y} ${x},${y}`} />
                {attached.map(({ host, x: hx, y: hy, primary }) => {
                  const address = host.addresses.find(a => a.activity?.sourceInterface === intf.name || intf.addresses.some(ip => ip.network === a.segment))!
                  const mobile = address.deviceClass === 'mobile', camera = address.deviceClass === 'camera'
                  return <g key={host.id}>
                    <path className="managed-net-attachment" d={`M${x},${y} L${hx},${hy}`} />
                    {primary.id === intf.id && <g className="managed-net-host" role="button" tabIndex={0} data-device-id={host.id} aria-label={`${host.name} ${address.ip}`} transform={`translate(${hx} ${hy})`}
                      onClick={() => activate(() => onEndpoint(address.ip, address.segment))} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onEndpoint(address.ip, address.segment) } }}>
                      <title>{host.name} · {address.ip} · {intf.name}</title>
                      <circle r="11" fill="transparent" stroke="none" />
                      {mobile ? <path d="M-4 -8H4V8H-4Z M-2 5H2" /> : camera ? <path d="M-7 0A7 7 0 0 1 7 0Z M-3 2L0 6L3 2" /> : <path d="M-7 -6H7V3H-7Z M0 3V7 M-5 7H5" />}
                    </g>}
                  </g>
                })}
                <g className={`managed-net-port ${intf.status === 'down' ? 'is-down' : ''}`} role="button" tabIndex={0} aria-label={`${device.name} / ${intf.name}`} data-interface-id={intf.id}
                  onClick={() => activate(() => onInterface(device, intf))} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onInterface(device, intf) } }}>
                  <circle cx={x} cy={y} r="17" /><path d={`M${x-7} ${y-5}H${x+7}V${y+5}H${x-7}Z M${x-4} ${y+5}V${y+9} M${x+4} ${y+5}V${y+9}`} />
                  <text className="managed-net-port-name" x={x} y={y-clusterRadius-40} textAnchor="middle">{intf.name}{intf.role === 'wan' ? (zh ? ' · 公网出口' : ' · WAN') : ''}</text>
                  {intf.addresses.map((ip, ai) => <text key={ip.cidr} x={x} y={y-clusterRadius-20+ai*17} textAnchor="middle">{ip.cidr}</text>)}
                  <text className="managed-net-count" x={x} y={y+clusterRadius+24} textAnchor="middle">{attached.length ? `${attached.length} ${zh ? '终端 · 点击展开' : 'endpoints · expand'}` : (intf.status === 'down' ? (zh ? '配置停用' : 'Disabled') : (zh ? '点击查看接口' : 'Inspect interface'))}</text>
                </g>
              </g>
            })}
            <g className="managed-net-core" role="button" tabIndex={0} aria-label={device.name} onClick={() => activate(() => onDevice(device))} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onDevice(device) } }}>
              <circle cx={cx} cy={cy} r="51" /><path d={`M${cx-25} ${cy-17}H${cx+25}V${cy+17}H${cx-25}Z M${cx-17} ${cy-7}H${cx+17} M${cx-17} ${cy+5}H${cx-9} M${cx-3} ${cy+5}H${cx+5} M${cx+11} ${cy+5}H${cx+17}`} />
              <text x={cx} y={cy+78} textAnchor="middle">{device.name}</text><text className="managed-net-serial" x={cx} y={cy+99} textAnchor="middle">{device.serial}</text>
            </g>
          </g>
        })}
      </g>
    </svg>
    <div className="managed-net-caption">{zh ? '实线：设备拥有接口 · 虚线：接入归属（日志 / 网段）' : 'Solid: interface ownership · Dashed: observed / subnet attachment'}<small>{zh ? '点击接口展开原有终端关系图 · 滚轮缩放 · 拖拽平移' : 'Click an interface to expand its endpoint graph · Scroll to zoom · Drag to pan'}</small></div>
    <div className="managed-net-controls"><button aria-pressed={all} onClick={() => setAll(v => !v)}>{zh ? (all ? '收起无地址接口' : '全部接口') : 'All interfaces'}</button><button aria-label="缩小" onClick={() => zoom(1 / 1.2)}>−</button><span>{Math.round(view.scale * 100)}%</span><button aria-label="放大" onClick={() => zoom(1.2)}>+</button><button onClick={() => setView({ x: 0, y: 0, scale: 1 })}>{zh ? '复位' : 'Reset'}</button></div>
  </div>
}
