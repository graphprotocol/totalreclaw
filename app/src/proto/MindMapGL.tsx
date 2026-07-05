import { useEffect, useRef, type ReactNode } from "react";
import * as THREE from "three";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";
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
import { SCOPE_COLOR, YOU_ID, type MindNode, type MindLink, type Scope } from "./mindmap-data";
import type { MindMode } from "./MindMap";

/**
 * WebGL "Glow" renderer for the Map — the A/B wow variant of MindMap.
 * three.js + UnrealBloom: entities are luminous point-sprites, light pulses
 * travel along the links, all on the dark Keeper-ink field. Same data, same
 * three layouts (d3-force), same interaction model as the canvas renderer;
 * nodes stay constant screen-size, zoom spreads the map. Lazy-loaded.
 */
interface SimNode extends MindNode, SimulationNodeDatum {}

const INK = 0x1c1a17;
const CENTROID_R = 165;
const MISC = "#8A7F76";

const nodeRadius = (n: MindNode): number => (n.kind === "you" ? 21 : n.kind === "scope" ? 13 : 5 + n.weight * 0.95);
const colorHex = (n: MindNode): string => (n.kind === "you" ? "#FFE0C0" : n.scope ? (SCOPE_COLOR[n.scope] ?? MISC) : "#C16240");

function centroidOf(n: SimNode, angle: Map<string, number>): [number, number] {
  if (n.kind === "you" || !n.scope) return [0, 0];
  const a = angle.get(n.scope) ?? 0;
  return [Math.cos(a) * CENTROID_R, Math.sin(a) * CENTROID_R];
}

function applyForces(sim: Simulation<SimNode, undefined>, nodes: SimNode[], links: MindLink[], mode: MindMode, angle: Map<string, number>) {
  const you = nodes.find((n) => n.id === YOU_ID);
  if (you) {
    you.fx = mode === "radial" ? 0 : null;
    you.fy = mode === "radial" ? 0 : null;
  }
  sim
    .force("collide", forceCollide<SimNode>((d) => nodeRadius(d) + 7).strength(0.9))
    .force("charge", forceManyBody<SimNode>().strength((d) => -34 - d.weight * (mode === "constellation" ? 20 : 7)));
  sim.force("x", null).force("y", null).force("radial", null).force("center", null);
  const mk = () => forceLink<SimNode, MindLink>(links.map((l) => ({ ...l }))).id((d) => d.id);
  if (mode === "constellation") {
    sim.force("center", forceCenter(0, 0)).force("link", mk().distance((l) => ((l.source as unknown as SimNode).kind === "you" ? 90 : 52)).strength(0.5));
  } else if (mode === "atlas") {
    sim
      .force("x", forceX<SimNode>((d) => centroidOf(d, angle)[0]).strength((d) => (d.kind === "you" ? 0.04 : 0.55)))
      .force("y", forceY<SimNode>((d) => centroidOf(d, angle)[1]).strength((d) => (d.kind === "you" ? 0.04 : 0.55)))
      .force("link", mk().distance(46).strength(0.04));
  } else {
    sim
      .force("radial", forceRadial<SimNode>((d) => (d.kind === "you" ? 0 : d.kind === "scope" ? 118 : 232), 0, 0).strength((d) => (d.kind === "you" ? 0 : 0.82)))
      .force("link", mk().distance(92).strength(0.035));
  }
  sim.alpha(0.9).restart();
}

const VERT = `
  attribute float aSize;
  attribute vec3 aColor;
  attribute float aAlpha;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    vColor = aColor; vAlpha = aAlpha;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = aSize;
  }`;
const FRAG = `
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    vec2 uv = gl_PointCoord - vec2(0.5);
    float d = length(uv) * 2.0;
    float core = smoothstep(0.5, 0.0, d);
    float glow = smoothstep(1.0, 0.25, d) * 0.45;
    float a = (core + glow) * vAlpha;
    if (a < 0.01) discard;
    gl_FragColor = vec4(vColor, a);
  }`;

interface Props {
  mode: MindMode;
  nodes: MindNode[];
  links: MindLink[];
  neighborsOf: (id: string) => Set<string>;
  selectedId: string | null;
  onSelect: (n: MindNode | null) => void;
}

export function MindMapGL({ mode, nodes: dataNodes, links: dataLinks, neighborsOf, selectedId, onSelect }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const glRef = useRef<HTMLCanvasElement>(null);
  const labelRef = useRef<HTMLCanvasElement>(null);
  const simRef = useRef<Simulation<SimNode, undefined> | null>(null);
  const nodesRef = useRef<SimNode[]>([]);
  const angleRef = useRef<Map<string, number>>(new Map());
  const modeRef = useRef<MindMode>(mode);
  const selRef = useRef<string | null>(selectedId);
  const hoverRef = useRef<string | null>(null);
  const zoomApiRef = useRef<{ zoomBy: (f: number) => void; fit: () => void } | null>(null);

  selRef.current = selectedId;

  useEffect(() => {
    const container = containerRef.current!;
    const glCanvas = glRef.current!;
    const labelCanvas = labelRef.current!;
    const lctx = labelCanvas.getContext("2d")!;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const scopeIds = dataNodes.filter((n) => n.kind === "scope" && n.scope).map((n) => n.scope!);
    const angle = new Map(scopeIds.map((s, i) => [s, (i / Math.max(1, scopeIds.length)) * Math.PI * 2 - Math.PI / 2]));
    angleRef.current = angle;

    const nodes: SimNode[] = dataNodes.map((n, i) => ({ ...n, x: Math.cos(i * 1.9) * (30 + i * 5), y: Math.sin(i * 1.9) * (30 + i * 5) }));
    nodesRef.current = nodes;
    const idIndex = new Map(nodes.map((n, i) => [n.id, i]));

    const sim = forceSimulation<SimNode>(nodes).stop();
    simRef.current = sim;
    applyForces(sim, nodes, dataLinks, modeRef.current, angle);
    for (let i = 0; i < 240; i++) sim.tick();

    // three.js setup
    const renderer = new THREE.WebGLRenderer({ canvas: glCanvas, antialias: true, alpha: false });
    renderer.setPixelRatio(dpr);
    renderer.setClearColor(INK, 1);
    const scene = new THREE.Scene();
    let w = container.clientWidth || 1;
    let h = container.clientHeight || 1;
    const VIEW = 340; // world half-height mapped to the viewport
    const camera = new THREE.OrthographicCamera(-VIEW, VIEW, VIEW, -VIEW, -1000, 1000);
    camera.position.z = 10;

    // node points
    const nGeo = new THREE.BufferGeometry();
    const nPos = new Float32Array(nodes.length * 3);
    const nColor = new Float32Array(nodes.length * 3);
    const nSize = new Float32Array(nodes.length);
    const nAlpha = new Float32Array(nodes.length);
    const col = new THREE.Color();
    nodes.forEach((n, i) => {
      col.set(colorHex(n));
      nColor[i * 3] = col.r; nColor[i * 3 + 1] = col.g; nColor[i * 3 + 2] = col.b;
      nSize[i] = (nodeRadius(n) * 2 + 16) * dpr;
      nAlpha[i] = 1;
    });
    nGeo.setAttribute("position", new THREE.BufferAttribute(nPos, 3));
    nGeo.setAttribute("aColor", new THREE.BufferAttribute(nColor, 3));
    nGeo.setAttribute("aSize", new THREE.BufferAttribute(nSize, 1));
    nGeo.setAttribute("aAlpha", new THREE.BufferAttribute(nAlpha, 1));
    const nMat = new THREE.ShaderMaterial({ vertexShader: VERT, fragmentShader: FRAG, transparent: true, depthTest: false, depthWrite: false, blending: THREE.AdditiveBlending });
    const points = new THREE.Points(nGeo, nMat);
    scene.add(points);

    // link lines
    const lGeo = new THREE.BufferGeometry();
    const lPos = new Float32Array(dataLinks.length * 6);
    lGeo.setAttribute("position", new THREE.BufferAttribute(lPos, 3));
    const lMat = new THREE.LineBasicMaterial({ color: 0x8a7a6c, transparent: true, opacity: 0.22, blending: THREE.AdditiveBlending, depthTest: false });
    const lines = new THREE.LineSegments(lGeo, lMat);
    scene.add(lines);

    // pulse sparks travelling along links
    const pGeo = new THREE.BufferGeometry();
    const pPos = new Float32Array(dataLinks.length * 3);
    const pColor = new Float32Array(dataLinks.length * 3);
    const pSize = new Float32Array(dataLinks.length);
    const pAlpha = new Float32Array(dataLinks.length);
    col.set("#F2C9A6");
    for (let i = 0; i < dataLinks.length; i++) {
      pColor[i * 3] = col.r; pColor[i * 3 + 1] = col.g; pColor[i * 3 + 2] = col.b;
      pSize[i] = 3.5 * dpr;
      pAlpha[i] = 0.9;
    }
    pGeo.setAttribute("position", new THREE.BufferAttribute(pPos, 3));
    pGeo.setAttribute("aColor", new THREE.BufferAttribute(pColor, 3));
    pGeo.setAttribute("aSize", new THREE.BufferAttribute(pSize, 1));
    pGeo.setAttribute("aAlpha", new THREE.BufferAttribute(pAlpha, 1));
    const pMat = new THREE.ShaderMaterial({ vertexShader: VERT, fragmentShader: FRAG, transparent: true, depthTest: false, depthWrite: false, blending: THREE.AdditiveBlending });
    const pulses = new THREE.Points(pGeo, pMat);
    scene.add(pulses);

    // bloom composer
    const composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    const bloom = new UnrealBloomPass(new THREE.Vector2(w, h), 0.9, 0.55, 0.12);
    composer.addPass(bloom);

    const resize = () => {
      w = container.clientWidth || 1;
      h = container.clientHeight || 1;
      const aspect = w / h;
      camera.left = -VIEW * aspect;
      camera.right = VIEW * aspect;
      camera.top = VIEW;
      camera.bottom = -VIEW;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h, false);
      composer.setSize(w, h);
      bloom.setSize(w, h);
      labelCanvas.width = w * dpr;
      labelCanvas.height = h * dpr;
      labelCanvas.style.width = `${w}px`;
      labelCanvas.style.height = `${h}px`;
    };

    const fitView = () => {
      camera.position.x = 0;
      camera.position.y = 0;
      camera.zoom = 1;
      camera.updateProjectionMatrix();
    };
    const zoomBy = (f: number) => {
      camera.zoom = Math.max(0.4, Math.min(4, camera.zoom * f));
      camera.updateProjectionMatrix();
    };
    zoomApiRef.current = { zoomBy, fit: fitView };

    const toScreen = (x: number, y: number): [number, number] => {
      const v = new THREE.Vector3(x, y, 0).project(camera);
      return [(v.x * 0.5 + 0.5) * w, (-v.y * 0.5 + 0.5) * h];
    };
    const toWorld = (sx: number, sy: number): [number, number] => {
      const v = new THREE.Vector3((sx / w) * 2 - 1, -(sy / h) * 2 + 1, 0).unproject(camera);
      return [v.x, v.y];
    };
    const nodeAt = (sx: number, sy: number): SimNode | null => {
      let best: SimNode | null = null;
      let bestD = Infinity;
      for (const n of nodes) {
        const [px, py] = toScreen(n.x!, n.y!);
        const d = (px - sx) ** 2 + (py - sy) ** 2;
        const rr = (nodeRadius(n) + 8) ** 2;
        if (d < rr && d < bestD) { bestD = d; best = n; }
      }
      return best;
    };

    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(container);

    let raf = 0;
    let t = 0;
    const animate = () => {
      t += 1;
      if (sim.alpha() > sim.alphaMin()) sim.tick();
      const focus = hoverRef.current ?? selRef.current;
      const near = focus ? neighborsOf(focus) : null;

      // update node buffers
      nodes.forEach((n, i) => {
        nPos[i * 3] = n.x!; nPos[i * 3 + 1] = n.y!; nPos[i * 3 + 2] = 0;
        const active = !near || n.id === focus || near.has(n.id);
        nAlpha[i] = active ? (n.id === selRef.current ? 1 : n.id === focus ? 1 : 0.95) : 0.12;
      });
      nGeo.attributes.position.needsUpdate = true;
      (nGeo.attributes.aAlpha as THREE.BufferAttribute).needsUpdate = true;

      // links + pulses
      for (let i = 0; i < dataLinks.length; i++) {
        const a = nodes[idIndex.get(dataLinks[i].source)!];
        const b = nodes[idIndex.get(dataLinks[i].target)!];
        if (!a || !b) continue;
        lPos[i * 6] = a.x!; lPos[i * 6 + 1] = a.y!; lPos[i * 6 + 2] = 0;
        lPos[i * 6 + 3] = b.x!; lPos[i * 6 + 4] = b.y!; lPos[i * 6 + 5] = 0;
        const on = !!focus && (dataLinks[i].source === focus || dataLinks[i].target === focus);
        const frac = reduced ? 0.5 : ((t * 0.006 + i * 0.137) % 1);
        pPos[i * 3] = a.x! + (b.x! - a.x!) * frac;
        pPos[i * 3 + 1] = a.y! + (b.y! - a.y!) * frac;
        pPos[i * 3 + 2] = 0;
        pAlpha[i] = on ? 1 : near ? 0.15 : 0.75;
      }
      lGeo.attributes.position.needsUpdate = true;
      pGeo.attributes.position.needsUpdate = true;
      (pGeo.attributes.aAlpha as THREE.BufferAttribute).needsUpdate = true;
      lMat.opacity = 0.22;

      composer.render();

      // 2D label overlay (semantic zoom)
      lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      lctx.clearRect(0, 0, w, h);
      const zoomA = Math.max(0, Math.min(1, (camera.zoom - 1.1) / 0.7));
      lctx.textAlign = "center";
      lctx.textBaseline = "top";
      for (const n of nodes) {
        const active = !near || n.id === focus || (near ? near.has(n.id) : false);
        if (!active) continue;
        const forced = n.kind === "you" || n.kind === "scope" || n.id === focus || (near ? near.has(n.id) : false);
        const la = forced ? 1 : n.kind === "entity" ? zoomA : 0;
        if (la < 0.04) continue;
        const [px, py] = toScreen(n.x!, n.y!);
        const isBig = n.kind === "you" || n.kind === "scope";
        lctx.globalAlpha = la;
        lctx.font = `${n.kind === "you" ? 700 : isBig ? 600 : 500} ${n.kind === "you" ? 15 : isBig ? 13 : 11}px Figtree, ui-sans-serif, system-ui, sans-serif`;
        lctx.fillStyle = isBig ? "#FBFAF8" : "rgba(251,250,248,0.85)";
        lctx.fillText(n.label, px, py + nodeRadius(n) + 5);
      }
      lctx.globalAlpha = 1;

      raf = requestAnimationFrame(animate);
    };
    raf = requestAnimationFrame(animate);

    // interaction
    const drag = { node: null as SimNode | null, panning: false, lastX: 0, lastY: 0 };
    const xy = (e: PointerEvent) => {
      const r = glCanvas.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top] as [number, number];
    };
    const onDown = (e: PointerEvent) => {
      const [x, y] = xy(e);
      const n = nodeAt(x, y);
      glCanvas.setPointerCapture(e.pointerId);
      if (n) { drag.node = n; drag.panning = false; drag.lastX = x; drag.lastY = y; sim.alphaTarget(0.2).restart(); }
      else { drag.node = null; drag.panning = true; drag.lastX = x; drag.lastY = y; }
    };
    const onMove = (e: PointerEvent) => {
      const [x, y] = xy(e);
      if (drag.node) {
        const [wx, wy] = toWorld(x, y);
        drag.node.fx = wx; drag.node.fy = wy;
      } else if (drag.panning) {
        const [wx0, wy0] = toWorld(drag.lastX, drag.lastY);
        const [wx1, wy1] = toWorld(x, y);
        camera.position.x -= wx1 - wx0;
        camera.position.y -= wy1 - wy0;
        camera.updateProjectionMatrix();
        drag.lastX = x; drag.lastY = y;
      } else {
        const n = nodeAt(x, y);
        hoverRef.current = n?.id ?? null;
        glCanvas.style.cursor = n ? "pointer" : "grab";
      }
    };
    const onUp = (e: PointerEvent) => {
      const [x, y] = xy(e);
      if (drag.node) {
        const moved = Math.hypot(x - drag.lastX, y - drag.lastY);
        if (drag.node.id !== YOU_ID) { drag.node.fx = null; drag.node.fy = null; }
        if (moved < 4) onSelect(drag.node.kind === "entity" || drag.node.kind === "scope" ? drag.node : null);
        sim.alphaTarget(0);
      }
      drag.node = null; drag.panning = false;
    };
    const onWheel = (e: WheelEvent) => { e.preventDefault(); zoomBy(Math.exp(-e.deltaY * 0.0016)); };
    const onDbl = () => zoomBy(1.6);
    const onLeave = () => { hoverRef.current = null; };
    glCanvas.addEventListener("pointerdown", onDown);
    glCanvas.addEventListener("pointermove", onMove);
    glCanvas.addEventListener("pointerup", onUp);
    glCanvas.addEventListener("pointerleave", onLeave);
    glCanvas.addEventListener("wheel", onWheel, { passive: false });
    glCanvas.addEventListener("dblclick", onDbl);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      sim.stop();
      glCanvas.removeEventListener("pointerdown", onDown);
      glCanvas.removeEventListener("pointermove", onMove);
      glCanvas.removeEventListener("pointerup", onUp);
      glCanvas.removeEventListener("pointerleave", onLeave);
      glCanvas.removeEventListener("wheel", onWheel);
      glCanvas.removeEventListener("dblclick", onDbl);
      composer.dispose();
      nGeo.dispose(); nMat.dispose(); lGeo.dispose(); lMat.dispose(); pGeo.dispose(); pMat.dispose();
      renderer.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataNodes, dataLinks, neighborsOf, onSelect]);

  useEffect(() => {
    modeRef.current = mode;
    const sim = simRef.current;
    if (sim) applyForces(sim, nodesRef.current, dataLinks, mode, angleRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  return (
    <div ref={containerRef} className="relative h-full w-full overflow-hidden">
      <canvas ref={glRef} className="absolute inset-0 block h-full w-full touch-none" style={{ cursor: "grab" }} />
      <canvas ref={labelRef} className="pointer-events-none absolute inset-0 block h-full w-full" />
      <div className="absolute right-3 top-1/2 flex -translate-y-1/2 flex-col gap-1.5">
        <GlBtn label="Zoom in" onClick={() => zoomApiRef.current?.zoomBy(1.4)}><path d="M12 5v14M5 12h14" /></GlBtn>
        <GlBtn label="Zoom out" onClick={() => zoomApiRef.current?.zoomBy(1 / 1.4)}><path d="M5 12h14" /></GlBtn>
        <GlBtn label="Reset view" onClick={() => zoomApiRef.current?.fit()}><path d="M4 9V4h5M20 15v5h-5M4 15v5h5M20 9V4h-5" /></GlBtn>
      </div>
    </div>
  );
}

function GlBtn({ children, label, onClick }: { children: ReactNode; label: string; onClick: () => void }) {
  return (
    <button onClick={onClick} aria-label={label} className="flex h-9 w-9 items-center justify-center rounded-full bg-white/10 text-warm-white backdrop-blur transition hover:bg-white/20 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">{children}</svg>
    </button>
  );
}

// keep Scope import referenced for the shared type surface
export type { Scope };
