import { useMemo } from 'react'
import {
  Background,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { DataStats } from '../types'
import { makeT, type Lang } from '../i18n'

type TopoData = { title: string; sub: string; kind: 'attacker' | 'fw' | 'host' | 'port' | 'sink' | 'console' }

function TopoNode({ data }: NodeProps) {
  const d = data as TopoData
  return (
    <div className={`topo-node topo-${d.kind}`}>
      <Handle type="target" position={Position.Left} />
      <div className="topo-title">{d.title}</div>
      <div className="topo-sub">{d.sub}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  )
}

const nodeTypes = { topo: TopoNode }
const fmt = (n: number) => n.toLocaleString('en-US')

export function NetworkTopology({ stats, lang }: { stats: DataStats; lang: Lang }) {
  const t = makeT(lang)

  const { nodes, edges } = useMemo(() => {
    const attackers = stats.topAttackerSrc.slice(0, 3)
    const hosts = stats.topDenySrc.slice(0, 3)
    const ports = stats.topDenyPorts.slice(0, 3)
    const ns: Node[] = []
    const es: Edge[] = []

    attackers.forEach(([ip, n], i) => {
      ns.push({
        id: `atk${i}`,
        type: 'topo',
        position: { x: 0, y: i * 70 },
        data: { title: ip, sub: `${fmt(n)} ${t('evidenceVol')}`, kind: 'attacker' },
        draggable: false,
      })
      es.push({ id: `ea${i}`, source: `atk${i}`, target: 'fw', animated: true, style: { stroke: '#ff6b6b', strokeWidth: 1.5 } })
    })

    ns.push({
      id: 'fw',
      type: 'topo',
      position: { x: 300, y: 70 },
      data: { title: 'FortiGate 192.168.1.1', sub: `DAHUA · ${fmt(stats.lockouts)} lockouts`, kind: 'fw' },
      draggable: false,
    })

    hosts.forEach(([ip, n], i) => {
      ns.push({
        id: `host${i}`,
        type: 'topo',
        position: { x: 0, y: 230 + i * 70 },
        data: { title: ip, sub: `${fmt(n)} ${t('denyVol')}`, kind: 'host' },
        draggable: false,
      })
      es.push({ id: `eh${i}`, source: `host${i}`, target: 'fw', animated: true, style: { stroke: '#ffc46b', strokeWidth: 1.5 } })
    })

    ports.forEach(([p, n], i) => {
      ns.push({
        id: `port${i}`,
        type: 'topo',
        position: { x: 600, y: 230 + i * 70 },
        data: { title: `:${p}`, sub: `${fmt(n)} deny`, kind: 'port' },
        draggable: false,
      })
      es.push({ id: `ep${i}`, source: 'fw', target: `port${i}`, animated: true, style: { stroke: '#ff6b6b', strokeWidth: 1.3 } })
    })

    ns.push({
      id: 'sink',
      type: 'topo',
      position: { x: 600, y: 36 },
      data: { title: 'R230 192.168.1.23', sub: t('syslogSink'), kind: 'sink' },
      draggable: false,
    })
    ns.push({
      id: 'console',
      type: 'topo',
      position: { x: 880, y: 100 },
      data: { title: 'selfevo', sub: t('consoleNode'), kind: 'console' },
      draggable: false,
    })
    es.push({ id: 'efs', source: 'fw', target: 'sink', animated: true, style: { stroke: '#5db4ff', strokeWidth: 2 } })
    es.push({ id: 'esc', source: 'sink', target: 'console', animated: true, style: { stroke: '#3ad29f', strokeWidth: 2 } })

    return { nodes: ns, edges: es }
  }, [stats, lang])

  return (
    <div className="topo-flow">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag={false}
        zoomOnScroll={false}
        zoomOnPinch={false}
        preventScrolling={false}
      >
        <Background color="#1a1e26" gap={26} />
      </ReactFlow>
    </div>
  )
}
