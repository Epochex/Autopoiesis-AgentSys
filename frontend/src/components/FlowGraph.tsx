import { useMemo } from 'react'
import {
  ReactFlow, Handle, Position, getBezierPath, type Node, type Edge, type EdgeProps, type NodeProps, type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

const STEP_W = 190, STEP_H = 106, EVI_W = 234, EVI_H = 52

/* ── the causal bloodstream ──────────────────────────────────────────────────
   the 7-step reasoning spine + a persistent EVIDENCE lane that is born at PROBE
   and flows on to VERIFY and DIAGNOSE — the same "blood" traced end to end.
   capillary edges carry flowing cells; the flow only reaches as far as `reached`. */

type StepData = { no: string; name: string; desc: string; res: string; state: 'pending' | 'done' | 'active'; loadLabel?: string; onOpen?: () => void }
type EviData = { id: string; sum: string; state: 'pending' | 'live' }
type CapData = { active?: boolean; evi?: boolean; label?: string }

function StepNode({ data }: NodeProps<Node<StepData>>) {
  return (
    <div className={`fn-step ${data.state} ${data.loadLabel ? 'load' : ''}`} onClick={data.onOpen}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <Handle type="target" position={Position.Top} id="tt" isConnectable={false} />
      <div className="fn-step-top"><span className="fn-step-no">{data.no}</span><span className="fn-step-name">{data.name}</span></div>
      <span className="fn-step-desc">{data.desc}</span>
      <span className="fn-step-res">{data.res}</span>
      {data.loadLabel ? <span className="fn-step-load">{data.loadLabel}</span> : null}
      <span className="fn-step-open">＋ 展开</span>
      <Handle type="source" position={Position.Right} isConnectable={false} />
      <Handle type="source" position={Position.Bottom} id="b" isConnectable={false} />
      <Handle type="target" position={Position.Bottom} id="btl" isConnectable={false} style={{ left: '30%' }} />
      <Handle type="target" position={Position.Bottom} id="btr" isConnectable={false} style={{ left: '70%' }} />
    </div>
  )
}

type MemData = { code: string; count: number; state: 'pending' | 'live' }
function MemNode({ data }: NodeProps<Node<MemData>>) {
  return (
    <div className={`fn-mem ${data.state}`}>
      <span className="fn-mem-code">{data.code}</span>
      <span className="fn-mem-ct">{data.count}</span>
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  )
}

function EviNode({ data }: NodeProps<Node<EviData>>) {
  return (
    <div className={`fn-evi ${data.state}`}>
      <Handle type="target" position={Position.Top} id="t" isConnectable={false} style={{ left: '26%' }} />
      <span className="fn-evi-dot" />
      <span className="fn-evi-id">{data.id}</span>
      <span className="fn-evi-sum">{data.sum}</span>
      <Handle type="source" position={Position.Top} id="s" isConnectable={false} style={{ left: '74%' }} />
    </div>
  )
}

function Capillary({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition, data }: EdgeProps<Edge<CapData>>) {
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition, curvature: data?.evi ? 0.7 : 0.4 })
  const active = data?.active
  const evi = data?.evi
  const cells = evi ? 3 : 2
  const dur = evi ? 1.7 : 2.6
  return (
    <g className={`cap ${active ? 'on' : ''} ${evi ? 'evi' : ''}`}>
      <path d={path} className="cap-vessel" fill="none" />
      {active ? <path d={path} className="cap-flow" fill="none" /> : null}
      {active ? Array.from({ length: cells }).map((_, i) => (
        <circle key={i} r={evi ? 4 : 3} className="cap-cell">
          <animateMotion dur={`${dur}s`} begin={`${(i * dur) / cells}s`} repeatCount="indefinite" path={path} rotate="auto" />
        </circle>
      )) : null}
      {data?.label ? <text className={`cap-label ${active ? 'on' : ''}`} x={labelX} y={labelY} textAnchor="middle" dominantBaseline="middle">{data.label}</text> : null}
    </g>
  )
}

const nodeTypes = { step: StepNode, evi: EviNode, mem: MemNode }
const edgeTypes = { cap: Capillary }

export type FlowStep = { no: string; name: string; desc: string; res: string; kind: string; loadLabel?: string }

export function FlowGraph({
  steps, evidence, memory, reached, cursor, zh, onSeek,
}: {
  steps: FlowStep[]
  evidence: { id: string; sum: string }[]
  memory: { code: string; count: number }[]
  reached: number
  cursor: number
  zh: boolean
  onSeek: (i: number) => void
}) {
  const { nodes, edges } = useMemo(() => {
    const DX = 250, SY = 60
    const probeIdx = steps.findIndex((s) => s.kind === 'tool_called')
    const verifyIdx = steps.findIndex((s) => s.kind === 'verifier_result')
    const diagIdx = steps.findIndex((s) => s.kind === 'diagnosis_completed')

    const nodes: Node[] = steps.map((s, i) => ({
      id: `n${i}`,
      type: 'step',
      position: { x: i * DX, y: SY },
      width: STEP_W, height: STEP_H,
      data: {
        no: s.no, name: s.name, desc: s.desc, res: i <= reached ? s.res : '·',
        state: i > reached ? 'pending' : i === cursor ? 'active' : 'done',
        loadLabel: s.loadLabel,
        onOpen: () => onSeek(i),
      } as StepData,
      draggable: false, selectable: false,
    }))

    // evidence lane, born under PROBE, pushed well below the spine for clean vessels
    const eviY = SY + 258
    const eviLive = reached >= probeIdx && probeIdx >= 0
    evidence.forEach((e, j) => {
      nodes.push({
        id: `e${j}`,
        type: 'evi',
        position: { x: (probeIdx >= 0 ? probeIdx * DX : 3 * DX) + 20 + j * 300, y: eviY },
        width: EVI_W, height: EVI_H,
        data: { id: e.id, sum: e.sum, state: eviLive ? 'live' : 'pending' } as EviData,
        draggable: false, selectable: false,
      })
    })

    // memory tributaries — recalled memories feeding INTO the reasoning at MEMORY
    const memIdx = steps.findIndex((s) => s.kind === 'memory_read')
    const memLive = memIdx >= 0 && reached >= memIdx
    const memBaseX = (memIdx >= 0 ? memIdx : 1) * DX + STEP_W / 2 - ((memory.length - 1) * 92) / 2 - 42
    memory.forEach((m, k) => {
      nodes.push({
        id: `m${k}`,
        type: 'mem',
        position: { x: memBaseX + k * 92, y: SY - 150 },
        width: 84, height: 34,
        data: { code: m.code, count: m.count, state: memLive ? 'live' : 'pending' } as MemData,
        draggable: false, selectable: false,
      })
    })

    const edges: Edge[] = []
    for (let i = 0; i < steps.length - 1; i++) {
      edges.push({ id: `s${i}`, source: `n${i}`, target: `n${i + 1}`, type: 'cap', data: { active: reached >= i + 1 } })
    }
    // semantic edge labels narrate what flows on each vessel — labelled once per
    // bundle (first strand) to name the blood without cluttering the capillaries.
    evidence.forEach((_, j) => {
      if (probeIdx >= 0) edges.push({ id: `pe${j}`, source: `n${probeIdx}`, sourceHandle: 'b', target: `e${j}`, targetHandle: 't', type: 'cap', data: { active: reached >= probeIdx, evi: true, label: j === 0 ? (zh ? '钉证据' : 'PIN') : undefined } })
      if (verifyIdx >= 0) edges.push({ id: `ev${j}`, source: `e${j}`, sourceHandle: 's', target: `n${verifyIdx}`, targetHandle: j === 0 ? 'btl' : 'btr', type: 'cap', data: { active: reached >= verifyIdx, evi: true, label: j === 0 ? (zh ? '核验' : 'VERIFY') : undefined } })
      if (diagIdx >= 0) edges.push({ id: `ed${j}`, source: `e${j}`, sourceHandle: 's', target: `n${diagIdx}`, targetHandle: j === 0 ? 'btl' : 'btr', type: 'cap', data: { active: reached >= diagIdx, evi: true, label: j === 0 ? (zh ? '引用' : 'CITE') : undefined } })
    })
    memory.forEach((_, k) => {
      if (memIdx >= 0) edges.push({ id: `me${k}`, source: `m${k}`, target: `n${memIdx}`, targetHandle: 'tt', type: 'cap', data: { active: reached >= memIdx, label: k === 0 ? (zh ? '先验' : 'PRIOR') : undefined } })
    })
    return { nodes, edges }
  }, [steps, evidence, memory, reached, cursor, zh, onSeek])

  return (
    <div className="fn-wrap">
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
        fitView fitViewOptions={{ padding: 0.14 }}
        onNodeClick={(_, node) => { const m = /^n(\d+)$/.exec(node.id); if (m) onSeek(Number(m[1])) }}
        onInit={(inst: ReactFlowInstance) => { setTimeout(() => inst.fitView({ padding: 0.14 }), 0) }}
        nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}
        panOnDrag={false} panOnScroll={false} zoomOnScroll={false} zoomOnPinch={false} zoomOnDoubleClick={false}
        preventScrolling={false} proOptions={{ hideAttribution: true }} minZoom={0.2} maxZoom={1.6}
      />
    </div>
  )
}
