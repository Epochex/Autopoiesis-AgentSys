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
import type { RcaCase } from '../types'
import { makeT, type Lang } from '../i18n'

type StageData = {
  label: string
  detail: string
  tone: 'alert' | 'accent' | 'good' | 'bad' | 'strong'
}

function StageNode({ data }: NodeProps) {
  const d = data as StageData
  return (
    <div className={`flow-node tone-${d.tone}`}>
      <Handle type="target" position={Position.Left} />
      <div className="flow-node-label">{d.label}</div>
      <div className="flow-node-detail">{d.detail}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  )
}

const nodeTypes = { stage: StageNode }

function payloadOf(rcaCase: RcaCase, kind: string): Record<string, unknown> {
  return rcaCase.trace.find((e) => e.kind === kind)?.payload ?? {}
}

export function RcaFlow({ rcaCase, lang }: { rcaCase: RcaCase; lang: Lang }) {
  const t = makeT(lang)

  const { nodes, edges } = useMemo(() => {
    const mem = payloadOf(rcaCase, 'memory_read')
    const skills = payloadOf(rcaCase, 'skills_exposed').skills as string[] | undefined
    const evIds = rcaCase.diagnosis.evidence.map((e) => e.evidenceId)
    const memCount = Object.values(mem).reduce<number>(
      (acc, v) => acc + (Array.isArray(v) ? v.length : 0),
      0,
    )

    const stages: StageData[] = [
      { label: t('st_alert'), detail: rcaCase.assets[0] ?? '', tone: 'alert' },
      { label: t('st_memory'), detail: `${memCount}`, tone: 'accent' },
      { label: t('st_skills'), detail: (skills ?? []).join(' · ') || '—', tone: 'accent' },
      { label: t('st_tools'), detail: evIds.join(' · ') || '—', tone: 'accent' },
      { label: t('st_context'), detail: 'token budget', tone: 'accent' },
      {
        label: t('st_verify'),
        detail: rcaCase.verifier.passed ? '✓' : '✕',
        tone: rcaCase.verifier.passed ? 'good' : 'bad',
      },
      { label: t('st_diagnosis'), detail: rcaCase.diagnosis.rootCauseKey, tone: 'strong' },
    ]

    const ns: Node[] = stages.map((s, i) => ({
      id: `s${i}`,
      type: 'stage',
      position: { x: i * 178, y: (i % 2) * 26 },
      data: s,
      draggable: false,
      connectable: false,
    }))
    const es: Edge[] = stages.slice(1).map((_, i) => ({
      id: `e${i}`,
      source: `s${i}`,
      target: `s${i + 1}`,
      animated: true,
      style: { stroke: '#5db4ff', strokeWidth: 2 },
    }))
    return { nodes: ns, edges: es }
  }, [rcaCase, lang])

  return (
    <div className="rca-flow">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag={false}
        zoomOnScroll={false}
        zoomOnPinch={false}
        preventScrolling={false}
      >
        <Background color="#1a1e26" gap={22} />
      </ReactFlow>
    </div>
  )
}
