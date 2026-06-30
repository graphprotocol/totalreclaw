import { useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import {
  forceSimulation,
  forceManyBody,
  forceLink,
  forceCenter,
  forceCollide,
  type SimulationNodeDatum,
} from "d3-force";
import type { KGNode, KGLink } from "../../lib/vault/graph";

/**
 * Force-directed entity/topic graph over the decrypted vault. Ported from the
 * Keeper prototype's ExploreGraph, generalized to take derived nodes/links as
 * props (the proto read a static fixture). Lazy-loaded so @xyflow/react stays
 * out of the Memory landing bundle.
 */
interface SimNode extends KGNode, SimulationNodeDatum {}

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

function computeLayout(nodes: KGNode[], links: KGLink[]): SimNode[] {
  const simNodes: SimNode[] = nodes.map((n) => ({ ...n }));
  const simLinks = links.map((l) => ({ ...l }));
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

interface Props {
  nodes: KGNode[];
  links: KGLink[];
  neighborsOf: (id: string) => Set<string>;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}

export function EntityGraph({ nodes: kgNodes, links: kgLinks, neighborsOf, selectedId, onSelect }: Props) {
  const laidOut = useMemo(() => computeLayout(kgNodes, kgLinks), [kgNodes, kgLinks]);
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
      kgLinks.map((l, i) => ({
        id: `e${i}`,
        source: l.source,
        target: l.target,
        style: { stroke: "#E7E3DF", strokeWidth: 1.5 },
      })),
    [kgLinks],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [hoverId, setHoverId] = useState<string | null>(null);

  // Re-seed when the derived graph changes (filters, new data).
  useEffect(() => setNodes(initialNodes), [initialNodes, setNodes]);
  useEffect(() => setEdges(initialEdges), [initialEdges, setEdges]);

  useEffect(() => {
    const focus = hoverId ?? selectedId;
    const near = focus ? neighborsOf(focus) : null;
    setNodes((ns) =>
      ns.map((n) => {
        const active = !near || n.id === focus || near.has(n.id);
        const isSel = n.id === selectedId;
        return {
          ...n,
          style: {
            ...n.style,
            opacity: active ? 1 : 0.18,
            borderRadius: 9999,
            boxShadow: isSel
              ? "0 0 0 2px #FBFAF8, 0 0 0 4.5px #C16240, 0 0 0 8px rgba(193,98,64,0.15)"
              : undefined,
            transition: "opacity 160ms ease, box-shadow 160ms ease",
          },
        };
      }),
    );
    setEdges((es) =>
      es.map((e) => {
        const on = !!focus && (e.source === focus || e.target === focus);
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
  }, [hoverId, selectedId, neighborsOf, setNodes, setEdges]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodeTypes={nodeTypes}
      onNodeClick={(_, n) => onSelect(n.id)}
      onPaneClick={() => onSelect(null)}
      onNodeMouseEnter={(_, n) => setHoverId(n.id)}
      onNodeMouseLeave={() => setHoverId(null)}
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
