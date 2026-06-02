import { useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  forceSimulation,
  forceManyBody,
  forceLink,
  forceCenter,
  forceCollide,
  type SimulationNodeDatum,
} from "d3-force";
import { KG_NODES, KG_LINKS, neighborsOf, type KGNode } from "./graph-data";

interface SimNode extends KGNode, SimulationNodeDatum {}

// Hidden handle pinned to node center → edges route center-to-center.
const HANDLE_STYLE = {
  opacity: 0,
  left: "50%",
  top: "50%",
  width: 1,
  height: 1,
  minWidth: 0,
  minHeight: 0,
  border: "none",
  background: "transparent",
} as const;

function TopicNode({ data }: NodeProps) {
  return (
    <div className="rounded-pill bg-clay px-3.5 py-1.5 font-display text-sm font-medium text-warm-white shadow-soft transition-transform duration-150 ease-keeper hover:scale-105">
      {data.label as string}
      <Handle type="target" position={Position.Top} style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Bottom} style={HANDLE_STYLE} />
    </div>
  );
}

function EntityNode({ data }: NodeProps) {
  return (
    <div className="rounded-pill border border-hairline bg-surface px-2.5 py-1 font-sans text-xs font-semibold text-ink shadow-soft transition-transform duration-150 ease-keeper hover:scale-105">
      {data.label as string}
      <Handle type="target" position={Position.Top} style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Bottom} style={HANDLE_STYLE} />
    </div>
  );
}

const nodeTypes = { topic: TopicNode, entity: EntityNode };

// One-shot d3-force layout (deterministic phyllotaxis seed → stable positions).
function computeLayout(): SimNode[] {
  const simNodes: SimNode[] = KG_NODES.map((n) => ({ ...n }));
  const simLinks = KG_LINKS.map((l) => ({ ...l }));
  const sim = forceSimulation(simNodes)
    .force("charge", forceManyBody().strength(-240))
    .force(
      "link",
      forceLink<SimNode, { source: string; target: string }>(simLinks)
        .id((d) => d.id)
        .distance(62)
        .strength(0.6),
    )
    .force("center", forceCenter(0, 0))
    .force("collide", forceCollide(34))
    .stop();
  for (let i = 0; i < 320; i++) sim.tick();
  return simNodes;
}

export function KgFlow() {
  const laidOut = useMemo(computeLayout, []);

  const initialNodes: Node[] = useMemo(
    () =>
      laidOut.map((n) => ({
        id: n.id,
        type: n.kind,
        position: { x: n.x ?? 0, y: n.y ?? 0 },
        data: { label: n.label },
      })),
    [laidOut],
  );
  const initialEdges: Edge[] = useMemo(
    () =>
      KG_LINKS.map((l, i) => ({
        id: `e${i}`,
        source: l.source,
        target: l.target,
        style: { stroke: "#E7E3DF", strokeWidth: 1.5 },
      })),
    [],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  useEffect(() => {
    const near = hoveredId ? neighborsOf(hoveredId) : null;
    setNodes((ns) =>
      ns.map((n) => {
        const active = !near || n.id === hoveredId || near.has(n.id);
        return {
          ...n,
          style: { ...n.style, opacity: active ? 1 : 0.18, transition: "opacity 160ms ease" },
        };
      }),
    );
    setEdges((es) =>
      es.map((e) => {
        const on = !!hoveredId && (e.source === hoveredId || e.target === hoveredId);
        return {
          ...e,
          animated: on,
          style: {
            ...e.style,
            stroke: on ? "#C16240" : "#E7E3DF",
            strokeWidth: on ? 2 : 1.5,
            opacity: !near || on ? 1 : 0.12,
            transition: "stroke 160ms ease, opacity 160ms ease",
          },
        };
      }),
    );
  }, [hoveredId, setNodes, setEdges]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodeTypes={nodeTypes}
      onNodeMouseEnter={(_, n) => setHoveredId(n.id)}
      onNodeMouseLeave={() => setHoveredId(null)}
      fitView
      fitViewOptions={{ padding: 0.35 }}
      minZoom={0.4}
      maxZoom={2.5}
      nodesConnectable={false}
      proOptions={{ hideAttribution: true }}
    >
      <Background color="#E7E3DF" gap={22} size={1} />
      <Controls showInteractive={false} position="bottom-right" />
    </ReactFlow>
  );
}
