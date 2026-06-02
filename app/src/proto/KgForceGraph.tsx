import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { forceCollide } from "d3-force";
import { KG_NODES, KG_LINKS, neighborsOf } from "./graph-data";

interface FGNode {
  id: string;
  label: string;
  kind: "topic" | "entity";
  x?: number;
  y?: number;
}

function idOf(end: unknown): string {
  return typeof end === "object" && end !== null ? (end as FGNode).id : (end as string);
}

/** "Cinematic" engine: canvas force-simulation. Drag, zoom/pan, hover focus,
 *  moving link particles on the hovered cluster. React-18 safe. */
export function KgForceGraph() {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [hoverId, setHoverId] = useState<string | null>(null);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Tighter clusters + node collision so labels don't pile up on small viewports.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg || size.w === 0) return;
    fg.d3Force("charge")?.strength(-160);
    fg.d3Force("link")?.distance(46);
    fg.d3Force("collide", forceCollide(18));
    fg.d3ReheatSimulation();
  }, [size.w]);

  const data = useMemo(
    () => ({
      nodes: KG_NODES.map((n) => ({ ...n })) as FGNode[],
      links: KG_LINKS.map((l) => ({ ...l })),
    }),
    [],
  );

  const near = hoverId ? neighborsOf(hoverId) : null;
  const isActive = (id: string) => !near || id === hoverId || near.has(id);
  const linkHot = (l: { source: unknown; target: unknown }) =>
    !!hoverId && (idOf(l.source) === hoverId || idOf(l.target) === hoverId);

  return (
    <div ref={wrapRef} className="h-full w-full">
      {size.w > 0 && (
        <ForceGraph2D
          ref={fgRef}
          width={size.w}
          height={size.h}
          graphData={data}
          backgroundColor="#FFFFFF"
          warmupTicks={60}
          cooldownTicks={120}
          onEngineStop={() => fgRef.current?.zoomToFit(500, 48)}
          enableNodeDrag
          onNodeHover={(node: FGNode | null) => setHoverId(node ? node.id : null)}
          linkColor={(l: { source: unknown; target: unknown }) =>
            linkHot(l) ? "#C16240" : "#E7E3DF"
          }
          linkWidth={(l: { source: unknown; target: unknown }) => (linkHot(l) ? 2 : 1)}
          linkDirectionalParticles={(l: { source: unknown; target: unknown }) =>
            linkHot(l) ? 3 : 0
          }
          linkDirectionalParticleWidth={2}
          linkDirectionalParticleColor={() => "#C16240"}
          nodeCanvasObjectMode={() => "replace"}
          nodeCanvasObject={(node: FGNode, ctx: CanvasRenderingContext2D, scale: number) => {
            const topic = node.kind === "topic";
            const r = topic ? 7 : 4.5;
            const x = node.x ?? 0;
            const y = node.y ?? 0;
            ctx.globalAlpha = isActive(node.id) ? 1 : 0.18;
            ctx.beginPath();
            ctx.arc(x, y, r, 0, 2 * Math.PI);
            ctx.fillStyle = topic ? "#C16240" : "#CFC6BE";
            ctx.fill();
            if (topic) {
              ctx.lineWidth = 1.5;
              ctx.strokeStyle = "#A54B2E";
              ctx.stroke();
            }
            // Zoom-gate entity labels: topics always; entities only when zoomed in.
            if (topic || scale > 1.1) {
              const fontSize = (topic ? 12 : 10) / scale;
              ctx.font = `${topic ? 600 : 500} ${fontSize}px Figtree, ui-sans-serif, sans-serif`;
              ctx.textAlign = "center";
              ctx.textBaseline = "top";
              ctx.fillStyle = "#2B2824";
              ctx.fillText(node.label, x, y + r + 2 / scale);
            }
            ctx.globalAlpha = 1;
          }}
          nodePointerAreaPaint={(node: FGNode, color: string, ctx: CanvasRenderingContext2D) => {
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(node.x ?? 0, node.y ?? 0, node.kind === "topic" ? 9 : 7, 0, 2 * Math.PI);
            ctx.fill();
          }}
        />
      )}
    </div>
  );
}
