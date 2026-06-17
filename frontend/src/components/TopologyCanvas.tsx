import { useMemo } from 'react'
import type { CSSProperties } from 'react'
import {
  Background,
  BackgroundVariant,
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  type ReactFlowInstance,
  type Edge as FlowEdge,
  type EdgeProps,
  type Node as FlowNode,
  type NodeProps,
  getSmoothStepPath,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { StageLink, StageNode } from '../types'

interface TopologyCanvasProps {
  nodes: StageNode[]
  links: StageLink[]
  compact?: boolean
  fitPadding?: number
  nodeWidth?: number
  showEdgeLabels?: boolean
  showMiniMap?: boolean
  showLegend?: boolean
  showControls?: boolean
}

function OperationalNode({ data }: NodeProps<OpsNode>) {
  return (
    <div className={`ops-node state-${data.status}`}>
      <Handle
        type="target"
        position={Position.Left}
        style={{ opacity: 0, width: 8, height: 8, pointerEvents: 'none' }}
      />
      <div className="ops-node-header">
        <strong>{data.title}</strong>
        <span>{data.subtitle}</span>
      </div>
      <ul className="ops-node-metrics">
        {data.metrics.map((metric) => (
          <li key={`${data.title}-${metric.label}`}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
          </li>
        ))}
      </ul>
      <Handle
        type="source"
        position={Position.Right}
        style={{ opacity: 0, width: 8, height: 8, pointerEvents: 'none' }}
      />
    </div>
  )
}

type NodeData = Record<string, unknown> & {
  title: string
  subtitle: string
  status: StageNode['status']
  metrics: StageNode['metrics']
}

type OpsNode = FlowNode<NodeData, 'opsNode'>

type EdgeData = Record<string, unknown> & {
  state: StageLink['state']
  showLabel?: boolean
}

type OpsEdge = FlowEdge<EdgeData, 'opsEdge'>

function OperationalEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
}: EdgeProps<OpsEdge>) {
  const [edgePath, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 0,
  })

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        style={{
          stroke:
            data?.state === 'active'
              ? '#69f9ff'
              : data?.state === 'planned'
                ? 'rgba(255,255,255,0.14)'
                : 'rgba(255,122,32,0.68)',
          strokeWidth: data?.state === 'active' ? 2.2 : 1.7,
        }}
        markerEnd={MarkerType.ArrowClosed}
      />
      {data?.showLabel !== false ? (
        <EdgeLabelRenderer>
          <div
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: 'none',
            }}
            className="edge-label"
          >
            {String(data?.state || 'steady')}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  )
}

const nodeTypes = { opsNode: OperationalNode }
const edgeTypes = { opsEdge: OperationalEdge }

export function TopologyCanvas({
  nodes,
  links,
  compact = false,
  fitPadding,
  nodeWidth,
  showEdgeLabels = true,
  showMiniMap = !compact,
  showLegend = !compact,
  showControls = !compact,
}: TopologyCanvasProps) {
  const flowNodes = useMemo<OpsNode[]>(
    () =>
      nodes.map((node) => ({
        id: node.id,
        type: 'opsNode',
        draggable: false,
        selectable: false,
        position: { x: node.x, y: node.y },
        data: {
          title: node.title,
          subtitle: node.subtitle,
          status: node.status,
          metrics: node.metrics,
        },
        style: {
          width: nodeWidth ?? (compact ? 210 : 228),
        } satisfies CSSProperties,
      })),
    [compact, nodeWidth, nodes],
  )

  const flowEdges = useMemo<OpsEdge[]>(
    () =>
      links.map((link) => ({
        id: link.id,
        source: link.source,
        target: link.target,
        type: 'opsEdge',
        selectable: false,
        data: { state: link.state, showLabel: showEdgeLabels },
      })),
    [links, showEdgeLabels],
  )
  const resolvedFitPadding = fitPadding ?? (compact ? 0.12 : 0.18)
  const handleInit = (instance: ReactFlowInstance<OpsNode, OpsEdge>) => {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        void instance.fitView({
          padding: resolvedFitPadding,
          duration: 320,
        })
      })
    })
  }

  return (
    <div className={compact ? 'flow-frame compact' : 'flow-frame'}>
      <div className="flow-surface">
        <ReactFlow<OpsNode, OpsEdge>
          nodes={flowNodes}
          edges={flowEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          fitViewOptions={{ padding: resolvedFitPadding }}
          onInit={handleInit}
          proOptions={{ hideAttribution: true }}
          nodesDraggable={false}
          zoomOnDoubleClick={false}
          panOnDrag
          minZoom={0.28}
        >
          <Background
            color="rgba(152, 181, 211, 0.12)"
            gap={28}
            size={1.6}
            variant={BackgroundVariant.Lines}
          />
          {showMiniMap || showLegend || showControls ? (
            <>
              {showMiniMap ? (
                <MiniMap
                  pannable={false}
                  zoomable={false}
                  position="bottom-left"
                  style={{
                    backgroundColor: 'rgba(7,11,16,0.9)',
                    border: '1px solid rgba(152,181,211,0.14)',
                  }}
                  nodeColor={(node) =>
                    (node.data as NodeData | undefined)?.status === 'flowing'
                      ? '#69f9ff'
                      : (node.data as NodeData | undefined)?.status === 'planned'
                        ? '#738699'
                        : '#ff7a20'
                  }
                />
              ) : null}
              {showLegend ? (
                <Panel position="top-right" className="flow-legend">
                  active / steady / planned
                </Panel>
              ) : null}
              {showControls ? (
                <Controls position="bottom-right" showInteractive={false} />
              ) : null}
            </>
          ) : null}
        </ReactFlow>
      </div>
    </div>
  )
}
