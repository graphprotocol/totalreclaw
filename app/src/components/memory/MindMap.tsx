import { useEffect, useRef, type ReactNode } from "react";
import {
  forceSimulation,
  forceManyBody,
  forceLink,
  forceCenter,
  forceCollide,
  forceX,
  forceY,
  forceRadial,
  type Simulation,
  type SimulationNodeDatum,
} from "d3-force";
import { SCOPE_COLOR, YOU_ID, type MindNode, type MindLink, type Scope } from "../../lib/vault/mindmap";

/**
 * "Map of your mind" — a dark warm planetarium where memories are points of light.
 * Canvas 2D + d3-force, data-driven. Three layouts that morph into each other:
 * atlas · radial · constellation. Zoom is real (buttons + wheel + double-click)
 * and entity labels fade in as you zoom (semantic zoom).
 */
export type MindMode = "atlas" | "radial" | "constellation";

interface SimNode extends MindNode, SimulationNodeDatum {}

const INK = "#211E1B";
const WARM = "#FBFAF8";
const CLAY = "#C16240";
const CENTROID_R = 165;
const MISC = "#8A7F76";

const colorOf = (n: MindNode): string =>
  n.kind === "you" ? "#F2C9A6" : n.scope ? (SCOPE_COLOR[n.scope] ?? MISC) : CLAY;
const nodeRadius = (n: MindNode): number =>
  n.kind === "you" ? 21 : n.kind === "scope" ? 13 : 5 + n.weight * 0.95;

function centroidOf(n: SimNode, angle: Map<string, number>): { x: number; y: number } {
  if (n.kind === "you" || !n.scope) return { x: 0, y: 0 };
  const a = angle.get(n.scope) ?? 0;
  return { x: Math.cos(a) * CENTROID_R, y: Math.sin(a) * CENTROID_R };
}

function applyForces(
  sim: Simulation<SimNode, undefined>,
  nodes: SimNode[],
  links: MindLink[],
  mode: MindMode,
  angle: Map<string, number>,
) {
  const you = nodes.find((n) => n.id === YOU_ID);
  if (you) {
    if (mode === "radial") {
      you.fx = 0;
      you.fy = 0;
    } else {
      you.fx = null;
      you.fy = null;
    }
  }

  sim
    .force("collide", forceCollide<SimNode>((d) => nodeRadius(d) + 7).strength(0.9))
    .force(
      "charge",
      forceManyBody<SimNode>().strength((d) => -34 - d.weight * (mode === "constellation" ? 20 : 7)),
    );

  sim.force("x", null).force("y", null).force("radial", null).force("center", null);
  const mk = () => forceLink<SimNode, MindLink>(links.map((l) => ({ ...l }))).id((d) => d.id);

  if (mode === "constellation") {
    sim.force("center", forceCenter(0, 0)).force(
      "link",
      mk()
        .distance((l) => ((l.source as unknown as SimNode).kind === "you" ? 90 : 52))
        .strength(0.5),
    );
  } else if (mode === "atlas") {
    sim
      .force("x", forceX<SimNode>((d) => centroidOf(d, angle).x).strength((d) => (d.kind === "you" ? 0.04 : 0.55)))
      .force("y", forceY<SimNode>((d) => centroidOf(d, angle).y).strength((d) => (d.kind === "you" ? 0.04 : 0.55)))
      .force("link", mk().distance(46).strength(0.04));
  } else {
    sim
      .force(
        "radial",
        forceRadial<SimNode>((d) => (d.kind === "you" ? 0 : d.kind === "scope" ? 118 : 232), 0, 0).strength(
          (d) => (d.kind === "you" ? 0 : 0.82),
        ),
      )
      .force("link", mk().distance(92).strength(0.035));
  }
  sim.alpha(0.9).restart();
}

interface Props {
  mode: MindMode;
  nodes: MindNode[];
  links: MindLink[];
  neighborsOf: (id: string) => Set<string>;
  selectedId: string | null;
  onSelect: (n: MindNode | null) => void;
}

export function MindMap({ mode, nodes: dataNodes, links: dataLinks, neighborsOf, selectedId, onSelect }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const simRef = useRef<Simulation<SimNode, undefined> | null>(null);
  const nodesRef = useRef<SimNode[]>([]);
  const linksRef = useRef<MindLink[]>([]);
  const angleRef = useRef<Map<string, number>>(new Map());
  const camRef = useRef({ x: 0, y: 0, scale: 1 });
  const hoverRef = useRef<string | null>(null);
  const selRef = useRef<string | null>(selectedId);
  const modeRef = useRef<MindMode>(mode);
  const zoomApiRef = useRef<{ zoomBy: (f: number) => void; fit: () => void } | null>(null);
  const dragRef = useRef<{ node: SimNode | null; panning: boolean; lastX: number; lastY: number }>({
    node: null,
    panning: false,
    lastX: 0,
    lastY: 0,
  });
  const starsRef = useRef<{ x: number; y: number; r: number; a: number }[]>([]);

  selRef.current = selectedId;

  useEffect(() => {
    const canvas = canvasRef.current!;
    const container = containerRef.current!;
    const ctx = canvas.getContext("2d")!;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const scopeIds = dataNodes.filter((n) => n.kind === "scope" && n.scope).map((n) => n.scope!);
    const angle = new Map(scopeIds.map((s, i) => [s, (i / Math.max(1, scopeIds.length)) * Math.PI * 2 - Math.PI / 2]));
    angleRef.current = angle;

    const nodes: SimNode[] = dataNodes.map((n, i) => ({
      ...n,
      x: Math.cos(i * 1.9) * (30 + i * 5),
      y: Math.sin(i * 1.9) * (30 + i * 5),
    }));
    nodesRef.current = nodes;
    linksRef.current = dataLinks;

    const sim = forceSimulation<SimNode>(nodes).stop();
    simRef.current = sim;
    applyForces(sim, nodes, dataLinks, modeRef.current, angle);
    for (let i = 0; i < 240; i++) sim.tick();

    let w = 0;
    let h = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const fitView = () => {
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of nodes) {
        minX = Math.min(minX, n.x!);
        minY = Math.min(minY, n.y!);
        maxX = Math.max(maxX, n.x!);
        maxY = Math.max(maxY, n.y!);
      }
      const bw = maxX - minX || 1;
      const bh = maxY - minY || 1;
      const scale = Math.min((w * 0.86) / bw, (h * 0.86) / bh, 1.6);
      camRef.current = { x: -((minX + maxX) / 2) * scale, y: -((minY + maxY) / 2) * scale, scale };
    };

    const zoomBy = (f: number) => {
      const cam = camRef.current;
      const ns = Math.max(0.35, Math.min(3.5, cam.scale * f));
      cam.x *= ns / cam.scale;
      cam.y *= ns / cam.scale;
      cam.scale = ns;
    };
    zoomApiRef.current = { zoomBy, fit: fitView };

    const resize = () => {
      w = container.clientWidth;
      h = container.clientHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      const stars: { x: number; y: number; r: number; a: number }[] = [];
      const cnt = Math.round((w * h) / 5200);
      for (let i = 0; i < cnt; i++) {
        const sx = (Math.sin(i * 12.9898) * 43758.5453) % 1;
        const sy = (Math.sin(i * 78.233) * 12543.983) % 1;
        stars.push({
          x: Math.abs(sx) * w,
          y: Math.abs(sy) * h,
          r: Math.abs(Math.sin(i * 3.7)) * 1.1 + 0.3,
          a: Math.abs(Math.cos(i * 2.1)) * 0.5 + 0.12,
        });
      }
      starsRef.current = stars;
      fitView();
    };

    const toWorld = (sx: number, sy: number) => {
      const cam = camRef.current;
      return { x: (sx - w / 2 - cam.x) / cam.scale, y: (sy - h / 2 - cam.y) / cam.scale };
    };
    const nodeAt = (sx: number, sy: number): SimNode | null => {
      const cam = camRef.current;
      let best: SimNode | null = null;
      let bestD = Infinity;
      for (const n of nodes) {
        const px = w / 2 + cam.x + n.x! * cam.scale;
        const py = h / 2 + cam.y + n.y! * cam.scale;
        const dx = px - sx;
        const dy = py - sy;
        const d = dx * dx + dy * dy;
        const rr = (nodeRadius(n) + 6) ** 2; // constant screen-space hit radius
        if (d < rr && d < bestD) {
          bestD = d;
          best = n;
        }
      }
      return best;
    };

    let raf = 0;
    let t = 0;
    const draw = () => {
      t += 1;
      const cam = camRef.current;
      if (sim.alpha() > sim.alphaMin()) sim.tick();

      ctx.save();
      ctx.scale(dpr, dpr);
      ctx.fillStyle = INK;
      ctx.fillRect(0, 0, w, h);
      const vg = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.max(w, h) * 0.7);
      vg.addColorStop(0, "rgba(90,70,55,0.30)");
      vg.addColorStop(1, "rgba(20,17,15,0)");
      ctx.fillStyle = vg;
      ctx.fillRect(0, 0, w, h);
      for (const s of starsRef.current) {
        ctx.globalAlpha = s.a;
        ctx.fillStyle = "#EBD9C8";
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;

      // world → screen. Positions scale with zoom (the map spreads); node + text
      // sizes are drawn in constant screen px so stars never balloon.
      const S = (x: number, y: number): [number, number] => [
        w / 2 + cam.x + x * cam.scale,
        h / 2 + cam.y + y * cam.scale,
      ];

      const focus = hoverRef.current ?? selRef.current;
      const near = focus ? neighborsOf(focus) : null;
      const isActive = (id: string) => !near || id === focus || near.has(id);
      const pulse = reduced ? 0 : (Math.sin(t / 34) + 1) / 2;
      const zoomA = Math.max(0, Math.min(1, (cam.scale - 1.15) / 0.7));

      if (modeRef.current === "atlas") {
        for (const [scope, a] of angleRef.current) {
          const [cx, cy] = S(Math.cos(a) * CENTROID_R, Math.sin(a) * CENTROID_R);
          const rr = 120 * cam.scale; // halos are regions — they scale with the cluster
          const sc = SCOPE_COLOR[scope as Scope] ?? MISC;
          const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rr);
          g.addColorStop(0, hexA(sc, 0.22));
          g.addColorStop(1, hexA(sc, 0));
          ctx.fillStyle = g;
          ctx.beginPath();
          ctx.arc(cx, cy, rr, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      const byId = new Map(nodes.map((n) => [n.id, n]));
      ctx.lineWidth = 1;
      for (const l of linksRef.current) {
        const a = byId.get(l.source);
        const b = byId.get(l.target);
        if (!a || !b) continue;
        const [ax, ay] = S(a.x!, a.y!);
        const [bx, by] = S(b.x!, b.y!);
        const on = !!focus && (l.source === focus || l.target === focus);
        const dim = near && !on;
        ctx.strokeStyle = on ? hexA(CLAY, 0.85) : hexA("#8A7A6C", dim ? 0.05 : 0.16);
        ctx.lineWidth = on ? 1.6 : 1;
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.quadraticCurveTo((ax + bx) / 2, (ay + by) / 2 - 14, bx, by);
        ctx.stroke();
      }

      for (const n of nodes) {
        const [nx, ny] = S(n.x!, n.y!);
        const r = nodeRadius(n); // constant screen px
        const active = isActive(n.id);
        const col = colorOf(n);
        const sel = n.id === selRef.current;
        const isYou = n.kind === "you";
        const isScope = n.kind === "scope";
        ctx.globalAlpha = active ? 1 : 0.16;
        ctx.shadowBlur = (isYou ? 34 : isScope ? 20 : 10 + n.weight) * (active ? 1 : 0.3);
        ctx.shadowColor = hexA(col, 0.9);
        ctx.beginPath();
        ctx.fillStyle = col;
        ctx.arc(nx, ny, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.shadowBlur = 0;

        if ((isYou || isScope) && active) {
          ctx.globalAlpha = (isYou ? 0.5 : 0.32) * (0.5 + pulse * 0.5);
          ctx.strokeStyle = hexA(col, 0.9);
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          ctx.arc(nx, ny, r + 6 + pulse * 5, 0, Math.PI * 2);
          ctx.stroke();
        }
        if (sel) {
          ctx.globalAlpha = 1;
          ctx.strokeStyle = WARM;
          ctx.lineWidth = 2.5;
          ctx.beginPath();
          ctx.arc(nx, ny, r + 4, 0, Math.PI * 2);
          ctx.stroke();
          ctx.strokeStyle = hexA(CLAY, 0.9);
          ctx.beginPath();
          ctx.arc(nx, ny, r + 6.5, 0, Math.PI * 2);
          ctx.stroke();
        }

        const forced = isYou || isScope || n.id === focus || (near ? near.has(n.id) : false);
        const labelAlpha = forced ? 1 : n.kind === "entity" ? zoomA : 0;
        if (labelAlpha > 0.03 && active) {
          ctx.globalAlpha = labelAlpha;
          ctx.font = `${isYou ? 700 : isScope ? 600 : 500} ${isYou ? 15 : isScope ? 13 : 11}px Figtree, ui-sans-serif, system-ui, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillStyle = isYou || isScope ? WARM : hexA(WARM, 0.82);
          ctx.fillText(n.label, nx, ny + r + 4);
        }
        ctx.globalAlpha = 1;
      }
      ctx.restore();
      raf = requestAnimationFrame(draw);
    };

    const ro = new ResizeObserver(resize);
    ro.observe(container);
    resize();
    raf = requestAnimationFrame(draw);

    const getXY = (e: PointerEvent) => {
      const rect = canvas.getBoundingClientRect();
      return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    };
    const onDown = (e: PointerEvent) => {
      const { x, y } = getXY(e);
      const n = nodeAt(x, y);
      canvas.setPointerCapture(e.pointerId);
      if (n) {
        dragRef.current = { node: n, panning: false, lastX: x, lastY: y };
        sim.alphaTarget(0.2).restart();
      } else {
        dragRef.current = { node: null, panning: true, lastX: x, lastY: y };
      }
    };
    const onMove = (e: PointerEvent) => {
      const { x, y } = getXY(e);
      const d = dragRef.current;
      if (d.node) {
        const p = toWorld(x, y);
        d.node.fx = p.x;
        d.node.fy = p.y;
      } else if (d.panning) {
        camRef.current.x += x - d.lastX;
        camRef.current.y += y - d.lastY;
        d.lastX = x;
        d.lastY = y;
      } else {
        const n = nodeAt(x, y);
        hoverRef.current = n?.id ?? null;
        canvas.style.cursor = n ? "pointer" : "grab";
      }
    };
    const onUp = (e: PointerEvent) => {
      const { x, y } = getXY(e);
      const d = dragRef.current;
      if (d.node) {
        const moved = Math.hypot(x - d.lastX, y - d.lastY);
        if (d.node.id !== YOU_ID) {
          d.node.fx = null;
          d.node.fy = null;
        }
        if (moved < 4) onSelect(d.node.kind === "entity" || d.node.kind === "scope" ? d.node : null);
        sim.alphaTarget(0);
      }
      dragRef.current = { node: null, panning: false, lastX: x, lastY: y };
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const cam = camRef.current;
      const f = Math.exp(-e.deltaY * 0.0016);
      const ns = Math.max(0.35, Math.min(3.5, cam.scale * f));
      const wx = (e.offsetX - w / 2 - cam.x) / cam.scale;
      const wy = (e.offsetY - h / 2 - cam.y) / cam.scale;
      cam.x = e.offsetX - w / 2 - wx * ns;
      cam.y = e.offsetY - h / 2 - wy * ns;
      cam.scale = ns;
    };
    const onDbl = (e: MouseEvent) => {
      const cam = camRef.current;
      const ns = Math.min(3.5, cam.scale * 1.6);
      const wx = (e.offsetX - w / 2 - cam.x) / cam.scale;
      const wy = (e.offsetY - h / 2 - cam.y) / cam.scale;
      cam.x = e.offsetX - w / 2 - wx * ns;
      cam.y = e.offsetY - h / 2 - wy * ns;
      cam.scale = ns;
    };
    const onLeave = () => {
      hoverRef.current = null;
    };

    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointerleave", onLeave);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("dblclick", onDbl);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      sim.stop();
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("pointerleave", onLeave);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("dblclick", onDbl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataNodes, dataLinks, neighborsOf, onSelect]);

  useEffect(() => {
    modeRef.current = mode;
    const sim = simRef.current;
    if (sim) applyForces(sim, nodesRef.current, linksRef.current, mode, angleRef.current);
  }, [mode]);

  return (
    <div ref={containerRef} className="relative h-full w-full overflow-hidden">
      <canvas ref={canvasRef} className="block h-full w-full touch-none" style={{ cursor: "grab" }} />
      <div className="absolute right-3 top-1/2 flex -translate-y-1/2 flex-col gap-1.5">
        <ZoomBtn label="Zoom in" onClick={() => zoomApiRef.current?.zoomBy(1.4)}>
          <path d="M12 5v14M5 12h14" />
        </ZoomBtn>
        <ZoomBtn label="Zoom out" onClick={() => zoomApiRef.current?.zoomBy(1 / 1.4)}>
          <path d="M5 12h14" />
        </ZoomBtn>
        <ZoomBtn label="Reset view" onClick={() => zoomApiRef.current?.fit()}>
          <path d="M4 9V4h5M20 15v5h-5M4 15v5h5M20 9V4h-5" />
        </ZoomBtn>
      </div>
    </div>
  );
}

function ZoomBtn({ children, label, onClick }: { children: ReactNode; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      className="flex h-9 w-9 items-center justify-center rounded-full bg-white/10 text-warm-white backdrop-blur transition hover:bg-white/20 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        {children}
      </svg>
    </button>
  );
}

function hexA(hex: string, a: number): string {
  const h = hex.replace("#", "");
  return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
}
