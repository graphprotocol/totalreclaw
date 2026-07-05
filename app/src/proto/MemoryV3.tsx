import { useEffect, useMemo, useState, useCallback } from "react";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";
import { SEED_SESSIONS, relativeDate, type SeedSession, type SeedFact } from "./seed";
import { count } from "./format";
import { sourceShort } from "./presentation";
import { TypeBadge } from "../components/TypeBadge";
import { MindMap, type MindMode } from "./MindMap";
import { MindMapGL } from "./MindMapGL";
import { SCOPES, MIND_NODES, MIND_LINKS, mindNeighbors } from "./mindmap-data";

/**
 * Memory, reframed (design A/B, seed data). Three surfaces instead of four equal
 * tabs: a crystal-hero JOURNAL, a dark-planetarium MAP (Atlas/Radial/Constellation),
 * and the flat FACTS lens. Crystals are the hero; facts stay one tap away.
 */
type Surface = "journal" | "map" | "facts";

const isImport = (s: SeedSession) => !!s.importSource;

// ── Surface switcher ────────────────────────────────────────────────
function Switch<T extends string>({
  value,
  onChange,
  options,
  tone = "light",
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
  tone?: "light" | "dark";
}) {
  return (
    <div
      role="tablist"
      className={clsx(
        "inline-flex items-center gap-0.5 rounded-pill p-0.5",
        tone === "dark" ? "bg-white/10 backdrop-blur" : "border border-hairline bg-surface",
      )}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(o.value)}
            className={clsx(
              "rounded-pill px-4 py-1.5 text-sm font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-1",
              active
                ? tone === "dark"
                  ? "bg-warm-white text-ink shadow-soft"
                  : "bg-clay text-warm-white shadow-soft"
                : tone === "dark"
                  ? "text-warm-white/70 hover:text-warm-white"
                  : "text-ink-muted hover:text-ink",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ── Journal: crystal as a distinct, prominent object ────────────────
function CrystalCard({
  session,
  onOpen,
  onEntity,
}: {
  session: SeedSession;
  onOpen: () => void;
  onEntity: (name: string) => void;
}) {
  const imported = isImport(session);
  return (
    <article
      className="animate-fade-up group relative overflow-hidden rounded-[20px] bg-surface p-6 shadow-soft ring-1 ring-clay/15 transition duration-200 ease-keeper hover:-translate-y-0.5 hover:shadow-raised"
    >
      {/* faint corner light — the crystal catching light, not a gem gimmick */}
      <div className="pointer-events-none absolute -right-10 -top-10 h-40 w-40 rounded-full bg-clay/[0.07] blur-2xl" aria-hidden />
      <button onClick={onOpen} className="block w-full text-left focus:outline-none">
        <div className="flex items-center gap-2.5">
          <span className="text-[0.66rem] font-bold uppercase tracking-[0.12em] text-clay-deep">Crystal</span>
          <span className="h-1 w-1 rounded-full bg-hairline" aria-hidden />
          <time className="font-mono text-xs text-ink-muted">{relativeDate(session.date)}</time>
          {imported && (
            <span className="rounded-pill bg-clay-tint px-2 py-0.5 text-[0.62rem] font-bold uppercase tracking-wide text-clay-deep">
              Imported · {session.importSource}
            </span>
          )}
        </div>
        <h2
          className="mt-2.5 font-display text-[1.5rem] font-medium leading-[1.2] text-ink"
          style={{ textWrap: "balance" }}
        >
          {session.crystal.narrative}
        </h2>
        {session.crystal.keyOutcomes.length > 0 && (
          <ul className="mt-3.5 space-y-2">
            {session.crystal.keyOutcomes.slice(0, 2).map((o) => (
              <li key={o} className="flex gap-2.5 text-[0.95rem] leading-snug text-ink-muted">
                <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rotate-45 bg-clay" aria-hidden />
                <span>{o}</span>
              </li>
            ))}
          </ul>
        )}
      </button>
      <footer className="mt-5 flex flex-wrap items-center gap-2">
        <span className="rounded-pill bg-warm-white px-3 py-1 font-mono text-xs text-ink-muted ring-1 ring-hairline">
          {count(session.facts.length, "memory", "memories")} · {count(session.crystal.openThreads.length, "thread")}
        </span>
        {session.entities.slice(0, 3).map((e) => (
          <button
            key={e}
            onClick={() => onEntity(e)}
            className="rounded-pill bg-warm-white px-2.5 py-1 font-mono text-xs text-ink-muted ring-1 ring-hairline transition hover:text-ink hover:ring-clay/40"
          >
            {e}
          </button>
        ))}
      </footer>
    </article>
  );
}

// ── Flat fact row (Facts lens + panel) ──────────────────────────────
function FactRow({ f }: { f: SeedFact }) {
  return (
    <article
      className={clsx(
        "animate-fade-up rounded-2xl p-4 shadow-soft",
        f.pinned ? "bg-clay-tint" : "bg-surface",
      )}
    >
      <p className="font-display text-[1.05rem] leading-snug text-ink" style={{ textWrap: "pretty" }}>
        {f.text}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <TypeBadge type={f.type} />
        <span className="text-xs text-ink-muted">
          {sourceShort(f.source)}
          {f.scope ? ` · ${f.scope}` : ""}
          {f.importSource ? ` · ${f.importSource}` : ""}
          {f.pinned ? " · pinned" : ""}
        </span>
      </div>
    </article>
  );
}

// ── Drill panel (session + entity) ──────────────────────────────────
type PanelView = { kind: "session"; s: SeedSession } | { kind: "entity"; name: string };

function PanelList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <section className="mt-5">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">{title}</h3>
      <ul className="mt-2 space-y-1.5">
        {items.map((it) => (
          <li key={it} className="flex gap-2 text-sm leading-snug text-ink">
            <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-clay" aria-hidden />
            <span>{it}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Panel({
  view,
  onClose,
  onOpenSession,
}: {
  view: PanelView | null;
  onClose: () => void;
  onOpenSession: (s: SeedSession) => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const open = !!view;
  const s = view?.kind === "session" ? view.s : null;
  const name = view?.kind === "entity" ? view.name : null;
  const lc = name?.toLowerCase() ?? null;
  const entitySessions = lc ? SEED_SESSIONS.filter((x) => x.entities.some((e) => e.toLowerCase() === lc)) : [];

  return (
    <>
      <div
        onClick={onClose}
        aria-hidden
        className={clsx(
          "fixed inset-0 z-40 bg-ink/25 transition-opacity duration-200 ease-keeper",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
      />
      <aside
        role="dialog"
        aria-modal="true"
        className={clsx(
          "fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col bg-warm-white shadow-overlay transition-transform duration-200 ease-keeper",
          open ? "translate-x-0" : "translate-x-full",
        )}
      >
        {view && (
          <div className="flex items-center justify-between border-b border-hairline px-5 py-3">
            <span className="font-mono text-xs text-ink-muted">{s ? relativeDate(s.date) : "Entity"}</span>
            <button onClick={onClose} aria-label="Close" className="rounded-pill p-1.5 text-ink-muted transition hover:bg-clay-tint hover:text-clay-deep">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden><path d="M18 6 6 18M6 6l12 12" /></svg>
            </button>
          </div>
        )}

        {name && (
          <div className="flex-1 overflow-y-auto px-5 py-5">
            <h2 className="font-display text-2xl leading-snug text-ink">{name}</h2>
            <p className="mt-1 text-sm text-ink-muted">
              {entitySessions.length ? count(entitySessions.length, "session") + " mention this." : "Nothing yet."}
            </p>
            <div className="mt-5 space-y-3">
              {entitySessions.map((x) => (
                <button
                  key={x.id}
                  onClick={() => onOpenSession(x)}
                  className="block w-full rounded-2xl bg-surface p-4 text-left shadow-soft transition hover:-translate-y-0.5 hover:shadow-raised"
                >
                  <time className="font-mono text-xs text-ink-muted">{relativeDate(x.date)}</time>
                  <p className="mt-1 font-display text-[0.95rem] leading-snug text-ink">{x.crystal.narrative}</p>
                </button>
              ))}
            </div>
          </div>
        )}

        {s && (
          <div className="flex-1 overflow-y-auto px-5 py-5">
            {isImport(s) && (
              <span className="mb-3 inline-flex rounded-pill bg-clay-tint px-2.5 py-0.5 text-xs font-semibold text-clay-deep">
                Imported · {s.importSource}
              </span>
            )}
            <h2 className="font-display text-xl leading-snug text-ink">{s.crystal.narrative}</h2>
            <PanelList title="Key outcomes" items={s.crystal.keyOutcomes} />
            <PanelList title="Open threads" items={s.crystal.openThreads} />
            <PanelList title="Lessons" items={s.crystal.lessons ?? []} />
            <h3 className="mt-7 text-xs font-semibold uppercase tracking-wide text-ink-muted">Memories from this session</h3>
            <div className="mt-3 space-y-3">
              {s.facts.map((f) => (
                <FactRow key={f.id} f={f} />
              ))}
            </div>
          </div>
        )}
      </aside>
    </>
  );
}

// ── Page ────────────────────────────────────────────────────────────
export function MemoryV3() {
  const [surface, setSurface] = useState<Surface>("journal");
  const [mapMode, setMapMode] = useState<MindMode>("atlas");
  const [renderer, setRenderer] = useState<"canvas" | "glow">("canvas");
  const [panel, setPanel] = useState<PanelView | null>(null);

  const openSession = useCallback((s: SeedSession) => setPanel({ kind: "session", s }), []);
  const openEntity = useCallback((name: string) => setPanel({ kind: "entity", name }), []);
  const close = useCallback(() => setPanel(null), []);

  const allFacts = useMemo(
    () => SEED_SESSIONS.flatMap((s) => s.facts.map((f) => ({ f, imp: s.importSource }))),
    [],
  );

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="mx-auto w-full max-w-2xl px-4 pb-24 pt-8">
        <div className="mb-1 flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-display text-[2rem] leading-tight text-ink">Your memory</h1>
          <Switch
            value={surface}
            onChange={setSurface}
            options={[
              { value: "journal", label: "Journal" },
              { value: "map", label: "Map" },
              { value: "facts", label: "Facts" },
            ]}
          />
        </div>

        {surface === "journal" && (
          <>
            <p className="mb-6 text-sm text-ink-muted">
              Your sessions, distilled into Crystals. Every underlying memory lives in{" "}
              <button onClick={() => setSurface("facts")} className="font-semibold text-clay-deep hover:underline">
                Facts
              </button>
              .
            </p>
            <div className="space-y-4">
              {SEED_SESSIONS.map((s) => (
                <CrystalCard key={s.id} session={s} onOpen={() => openSession(s)} onEntity={openEntity} />
              ))}
            </div>
          </>
        )}

        {surface === "facts" && (
          <>
            <p className="mb-6 text-sm text-ink-muted">
              Everything your agents remember, one by one. {count(allFacts.length, "memory", "memories")} — each
              individually yours to keep, retype, or delete.
            </p>
            <div className="space-y-3">
              {allFacts.map(({ f }) => (
                <FactRow key={f.id} f={f} />
              ))}
            </div>
          </>
        )}
      </main>

      {/* Map is a full-bleed dark planetarium inset. */}
      {surface === "map" && (
        <div className="mx-auto w-full max-w-4xl px-4 pb-24">
          <p className="mb-4 max-w-2xl text-sm text-ink-muted">
            Your memory as a map. Entities are points of light; the layout shows the shape of what you talk about.
          </p>
          <div className="relative h-[72vh] min-h-[540px] w-full overflow-hidden rounded-[24px] bg-[#211E1B] shadow-overlay ring-1 ring-black/20">
            {renderer === "glow" ? (
              <MindMapGL
                mode={mapMode}
                nodes={MIND_NODES}
                links={MIND_LINKS}
                neighborsOf={mindNeighbors}
                selectedId={null}
                onSelect={(n) => n && (n.kind === "entity" || n.kind === "scope") && openEntity(n.label)}
              />
            ) : (
              <MindMap
                mode={mapMode}
                nodes={MIND_NODES}
                links={MIND_LINKS}
                neighborsOf={mindNeighbors}
                selectedId={null}
                onSelect={(n) => n && (n.kind === "entity" || n.kind === "scope") && openEntity(n.label)}
              />
            )}

            {/* renderer toggle — Canvas ⇄ WebGL glow */}
            <div className="absolute right-4 top-4 inline-flex items-center gap-0.5 rounded-pill bg-white/10 p-0.5 backdrop-blur">
              {(["canvas", "glow"] as const).map((r) => (
                <button
                  key={r}
                  onClick={() => setRenderer(r)}
                  aria-pressed={renderer === r}
                  className={
                    "rounded-pill px-3 py-1 text-xs font-semibold transition " +
                    (renderer === r ? "bg-warm-white text-ink shadow-soft" : "text-warm-white/70 hover:text-warm-white")
                  }
                >
                  {r === "glow" ? "✦ Glow" : "Canvas"}
                </button>
              ))}
            </div>

            {/* layout switcher */}
            <div className="pointer-events-auto absolute left-4 top-4 flex flex-col gap-2">
              <Switch
                tone="dark"
                value={mapMode}
                onChange={setMapMode}
                options={[
                  { value: "atlas", label: "Atlas" },
                  { value: "radial", label: "Radial" },
                  { value: "constellation", label: "Constellation" },
                ]}
              />
              <p className="max-w-[15rem] pl-1 text-[0.7rem] leading-snug text-warm-white/55">
                {mapMode === "atlas" && "Clustered into your life’s domains, sized by how often they come up."}
                {mapMode === "radial" && "You at the center; domains, then the people, projects and places under each."}
                {mapMode === "constellation" && "A free star-map — everything pulled together by what connects."}
              </p>
            </div>

            {/* legend */}
            <div className="pointer-events-none absolute bottom-4 left-4 flex flex-wrap gap-x-3 gap-y-1">
              {SCOPES.map((sc) => (
                <span key={sc.id} className="flex items-center gap-1.5 text-[0.7rem] font-medium text-warm-white/75">
                  <span className="h-2 w-2 rounded-full" style={{ backgroundColor: sc.color, boxShadow: `0 0 6px ${sc.color}` }} />
                  {sc.label}
                </span>
              ))}
            </div>

            {/* hint */}
            <div className="pointer-events-none absolute bottom-4 right-4 hidden text-[0.7rem] text-warm-white/45 sm:block">
              drag to explore · scroll to zoom · tap a star
            </div>
          </div>
        </div>
      )}

      <Panel view={panel} onClose={close} onOpenSession={openSession} />
    </div>
  );
}
