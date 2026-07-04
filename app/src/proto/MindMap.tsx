import { useEffect, useRef } from "react";
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
import {
  MIND_NODES,
  MIND_LINKS,
  SCOPES,
  SCOPE_COLOR,
  YOU_ID,
  mindNeighbors,
  type MindNode,
} from "./mindmap-data";

/**
 * "Map of your mind" — a dark warm planetarium where memories are points of light.
 * Canvas 2D + d3-force. Three layouts that morph into each other:
 *   atlas         — entities cluster into scope regions (the shape of your life)
 *   radial        — you at the center, domains → entities radiating out
 *   constellation — a free, crafted force-graph; gravity toward what matters
 *
 * Seed/throwaway (design A/B). Ports to the functional app by swapping the
 * fixture for the client-derived graph (lib/vault/graph.ts).
 */
export type MindMode = "atlas" | "radial" | "constellation";

interface SimNode extends MindNode, SimulationNodeDatum {}

const INK = "#211E1B"; // planetarium bg (deep warm, on the Keeper ink ramp)
const WARM = "#FBFAF8";
const CLAY = "#C16240";
const CENTROID_R = 165;

const scopeAngle = new Map(SCOPES.map((s, i) => [s.id, (i / SCOPES.length) * Math.PI * 2 - Math.PI / 2]));
function centroidOf(n: SimNode): { x: number; y: number } {
  if (n.kind === "you" || !n.scope) return { x: 0, y: 0 };
  const a = scopeAngle.get(n.scope) ?? 0;
  return { x: Math.cos(a) * CENTROID_R, y: Math.sin(a) * CENTROID_R };
}
function nodeRadius(n: MindNode): number {
  if (n.kind === "you") return 21;
  if (n.kind === "scope") return 13;
  return 5 + n.weight * 0.95;
}
function colorOf(n: MindNode): string {
  if (n.kind === "you") return "#F2C9A6";
  return n.scope ? SCOPE_COLOR[n.scope] : CLAY;
}

function applyForces(sim: Simulation<SimNode, undefined>, nodes: SimNode[], mode: MindMode) {
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

  // reset positional forces each switch
  sim.force("x", null).force("y", null).force("radial", null).force("center", null);

  if (mode === "constellation") {
    sim
      .force("center", forceCenter(0, 0))
      .force(
        "link",
        forceLink<SimNode, (typeof MIND_LINKS)[number]>(MIND_LINKS.map((l) => ({ ...l })))
          .id((d) => d.id)
          .distance((l) => ((l.source as unknown as SimNode).kind === "you" ? 90 : 52))
          .strength(0.5),
      );
  } else if (mode === "atlas") {
    sim
      .force("x", forceX<SimNode>((d) => centroidOf(d).x).strength((d) => (d.kind === "you" ? 0.04 : 0.55)))
      .force("y", forceY<SimNode>((d) => centroidOf(d).y).strength((d) => (d.kind === "you" ? 0.04 : 0.55)))
      .force(
        "link",
        forceLink<SimNode, (typeof MIND_LINKS)[number]>(MIND_LINKS.map((l) => ({ ...l })))
          .id((d) => d.id)
          .distance(46)
          .strength(0.04),
      );
  } else {
    // radial
    sim
      .force(
        "radial",
        forceRadial<SimNode>((d) => (d.kind === "you" ? 0 : d.kind === "scope" ? 118 : 232), 0, 0).strength(
          (d) => (d.kind === "you" ? 0 : 0.82),
        ),
      )
      .force(
        "link",
        forceLink<SimNode, (typeof MIND_LINKS)[number]>(MIND_LINKS.map((l) => ({ ...l })))
          .id((d) => d.id)
          .distance(92)
          .strength(0.035),
      );
  }
  sim.alpha(0.9).restart();
}

interface Props {
  mode: MindMode;
  selectedId: string | null;
  onSelect: (n: MindNode | null) => void;
}

export function MindMap({ mode, selectedId, onSelect }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const simRef = useRef<Simulation<SimNode, undefined> | null>(null);
  const nodesRef = useRef<SimNode[]>([]);
  const camRef = useRef({ x: 0, y: 0, scale: 1 });
  const hoverRef = useRef<string | null>(null);
  const selRef = useRef<string | null>(selectedId);
  const modeRef = useRef<MindMode>(mode);
  const dragRef = useRef<{ node: SimNode | null; panning: boolean; lastX: number; lastY: number }>({
    node: null,
    panning: false,
    lastX: 0,
    lastY: 0,
  });
  const starsRef = useRef<{ x: number; y: number; r: number; a: number }[]>([]);

  selRef.current = selectedId;

  // ── mount: build sim + render loop ──────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current!;
    const container = containerRef.current!;
    const ctx = canvas.getContext("2d")!;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const nodes: SimNode[] = MIND_NODES.map((n, i) => ({
      ...n,
      // seed positions on a spiral so the first settle is graceful, not a big bang
      x: Math.cos(i * 1.9) * (30 + i * 5),
      y: Math.sin(i * 1.9) * (30 + i * 5),
    }));
    nodesRef.current = nodes;

    const sim = forceSimulation<SimNode>(nodes).stop();
    simRef.current = sim;
    applyForces(sim, nodes, modeRef.current);
    for (let i = 0; i < 240; i++) sim.tick(); // settle before first paint

    let w = 0;
    let h = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const resize = () => {
      w = container.clientWidth;
      h = container.clientHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      // starfield in screen space
      const stars: { x: number; y: number; r: number; a: number }[] = [];
      const count = Math.round((w * h) / 5200);
      for (let i = 0; i < count; i++) {
        // deterministic-ish scatter (no Math.random dependency on layout)
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

    const fitView = () => {
      let minX = Infinity,
        minY = Infinity,
        maxX = -Infinity,
        maxY = -Infinity;
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

    const toWorld = (sx: number, sy: number) => {
      const cam = camRef.current;
      return { x: (sx - w / 2 - cam.x) / cam.scale, y: (sy - h / 2 - cam.y) / cam.scale };
    };
    const nodeAt = (sx: number, sy: number): SimNode | null => {
      const p = toWorld(sx, sy);
      let best: SimNode | null = null;
      let bestD = Infinity;
      for (const n of nodes) {
        const dx = n.x! - p.x;
        const dy = n.y! - p.y;
        const d = dx * dx + dy * dy;
        const rr = (nodeRadius(n) + 6) ** 2;
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
      // background
      ctx.fillStyle = INK;
      ctx.fillRect(0, 0, w, h);
      // subtle radial vignette (lighter warm core)
      const vg = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.max(w, h) * 0.7);
      vg.addColorStop(0, "rgba(90,70,55,0.30)");
      vg.addColorStop(1, "rgba(20,17,15,0)");
      ctx.fillStyle = vg;
      ctx.fillRect(0, 0, w, h);
      // starfield
      for (const s of starsRef.current) {
        ctx.globalAlpha = s.a;
        ctx.fillStyle = "#EBD9C8";
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;

      ctx.translate(w / 2 + cam.x, h / 2 + cam.y);
      ctx.scale(cam.scale, cam.scale);

      const focus = hoverRef.current ?? selRef.current;
      const near = focus ? mindNeighbors(focus) : null;
      const isActive = (id: string) => !near || id === focus || near.has(id);
      const pulse = reduced ? 0 : (Math.sin(t / 34) + 1) / 2;

      // atlas region halos
      if (modeRef.current === "atlas") {
        for (const s of SCOPES) {
          const a = scopeAngle.get(s.id)!;
          const cx = Math.cos(a) * CENTROID_R;
          const cy = Math.sin(a) * CENTROID_R;
          const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 120);
          g.addColorStop(0, hexA(s.color, 0.22));
          g.addColorStop(1, hexA(s.color, 0));
          ctx.fillStyle = g;
          ctx.beginPath();
          ctx.arc(cx, cy, 120, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      // links
      ctx.lineWidth = 1;
      for (const l of MIND_LINKS) {
        const a = nodes.find((n) => n.id === l.source)!;
        const b = nodes.find((n) => n.id === l.target)!;
        if (!a || !b) continue;
        const on = !!focus && (l.source === focus || l.target === focus);
        const dim = near && !on;
        ctx.strokeStyle = on ? hexA(CLAY, 0.85) : hexA("#8A7A6C", dim ? 0.05 : 0.16);
        ctx.lineWidth = on ? 1.6 : 1;
        const mx = (a.x! + b.x!) / 2;
        const my = (a.y! + b.y!) / 2 - 14;
        ctx.beginPath();
        ctx.moveTo(a.x!, a.y!);
        ctx.quadraticCurveTo(mx, my, b.x!, b.y!);
        ctx.stroke();
      }

      // nodes
      for (const n of nodes) {
        const r = nodeRadius(n);
        const active = isActive(n.id);
        const col = colorOf(n);
        const sel = n.id === selRef.current;
        const isYou = n.kind === "you";
        const isScope = n.kind === "scope";
        ctx.globalAlpha = active ? 1 : 0.16;

        // glow
        ctx.shadowBlur = (isYou ? 34 : isScope ? 20 : 10 + n.weight) * (active ? 1 : 0.3);
        ctx.shadowColor = hexA(col, 0.9);
        ctx.beginPath();
        ctx.fillStyle = col;
        ctx.arc(n.x!, n.y!, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.shadowBlur = 0;

        // you / scope: pulsing ring
        if ((isYou || isScope) && active) {
          ctx.globalAlpha = (isYou ? 0.5 : 0.32) * (0.5 + pulse * 0.5);
          ctx.strokeStyle = hexA(col, 0.9);
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          ctx.arc(n.x!, n.y!, r + 6 + pulse * 5, 0, Math.PI * 2);
          ctx.stroke();
        }
        // selection ring
        if (sel) {
          ctx.globalAlpha = 1;
          ctx.strokeStyle = WARM;
          ctx.lineWidth = 2.5;
          ctx.beginPath();
          ctx.arc(n.x!, n.y!, r + 4, 0, Math.PI * 2);
          ctx.stroke();
          ctx.strokeStyle = hexA(CLAY, 0.9);
          ctx.lineWidth = 2.5;
          ctx.beginPath();
          ctx.arc(n.x!, n.y!, r + 6.5, 0, Math.PI * 2);
          ctx.stroke();
        }

        // label — anchors (you + scopes) always; entities reveal on hover/focus
        // so dense clusters stay a clean field of light at rest.
        const showLabel = isYou || isScope || n.id === focus || (near ? near.has(n.id) : false);
        if (showLabel && active) {
          ctx.globalAlpha = 1;
          ctx.font = `${isYou ? 700 : isScope ? 600 : 500} ${isYou ? 15 : isScope ? 13 : 11}px Figtree, ui-sans-serif, system-ui, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillStyle = isYou || isScope ? WARM : hexA(WARM, 0.82);
          ctx.fillText(n.label, n.x!, n.y! + r + 4);
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

    // ── interaction ──
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
      const { x, y } = { x: e.offsetX, y: e.offsetY };
      const cam = camRef.current;
      const factor = Math.exp(-e.deltaY * 0.0016);
      const ns = Math.max(0.35, Math.min(3, cam.scale * factor));
      // zoom around cursor
      const wx = (x - w / 2 - cam.x) / cam.scale;
      const wy = (y - h / 2 - cam.y) / cam.scale;
      cam.x = x - w / 2 - wx * ns;
      cam.y = y - h / 2 - wy * ns;
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

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      sim.stop();
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("pointerleave", onLeave);
      canvas.removeEventListener("wheel", onWheel);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── mode change: retarget forces, morph ──
  useEffect(() => {
    modeRef.current = mode;
    const sim = simRef.current;
    if (sim) applyForces(sim, nodesRef.current, mode);
  }, [mode]);

  return (
    <div ref={containerRef} className="relative h-full w-full overflow-hidden">
      <canvas ref={canvasRef} className="block h-full w-full touch-none" style={{ cursor: "grab" }} />
    </div>
  );
}

/** hex + alpha → rgba() string. */
function hexA(hex: string, a: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}
