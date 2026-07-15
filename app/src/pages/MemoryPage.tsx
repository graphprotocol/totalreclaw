import { lazy, Suspense, useCallback, useMemo, useState, type ReactNode } from "react";
import { clsx } from "clsx";
import { useCrypto } from "../contexts/CryptoContext";
import { useVault, useDeleteFact, usePinFact } from "../hooks/useVault";
import { buildTimeline, importSourceOf, type SessionGroup } from "../lib/vault/timeline";
import { buildMindGraph, SCOPES, type Scope } from "../lib/vault/mindmap";
import { AppHeader } from "../components/AppHeader";
import { ClaimCard } from "../components/ClaimCard";
import { Segmented } from "../components/memory/Segmented";
import { SidePanel, type PanelView } from "../components/memory/SidePanel";
import { sourceShort, cap } from "../lib/presentation";
import { count, relativeDate } from "../lib/format";
import type { VaultItem, MemoryScope, MemorySource } from "../lib/types";

// Canvas + d3-force stay out of the Memory landing bundle until the Map opens.
const MindMap = lazy(() =>
  import("../components/memory/MindMap").then((m) => ({ default: m.MindMap })),
);
type Mode = "list" | "facts" | "map";

function membersOf(g: SessionGroup): VaultItem[] {
  return g.crystal ? [g.crystal, ...g.facts] : g.facts;
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={clsx(
        "rounded-pill px-3 py-1 text-xs font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-1",
        active
          ? "bg-clay-tint text-clay-deep ring-1 ring-clay/40"
          : "border border-hairline bg-surface text-ink-muted hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

export function MemoryPage() {
  const { keys } = useCrypto();
  const { data, isLoading, isError, error } = useVault(keys);
  const items = data?.items ?? [];
  const fetched = data?.fetched ?? 0;

  const [mode, setMode] = useState<Mode>("list");
  const [panel, setPanel] = useState<PanelView | null>(null);
  // A.2 curation: on-chain tombstone + pin/unpin. Wired into the Facts lens.
  const del = useDeleteFact(keys);
  const pin = usePinFact(keys);

  const [q, setQ] = useState("");
  const [scope, setScope] = useState<MemoryScope | null>(null);
  const [source, setSource] = useState<MemorySource | null>(null);
  const [openOnly, setOpenOnly] = useState(false);

  const close = useCallback(() => setPanel(null), []);
  const openSession = useCallback((g: SessionGroup) => setPanel({ kind: "session", group: g }), []);
  const openEntity = useCallback((name: string) => setPanel({ kind: "entity", name }), []);

  const groups = useMemo(() => buildTimeline(items), [items]);

  const scopes = useMemo(
    () => [...new Set(items.map((i) => i.claim.scope).filter((s): s is MemoryScope => !!s && s !== "unspecified"))].sort(),
    [items],
  );
  const sources = useMemo(
    () => [...new Set(items.map((i) => i.claim.source).filter((s): s is MemorySource => !!s))].sort(),
    [items],
  );

  const query = q.trim().toLowerCase();
  const matchesItem = useCallback(
    (m: VaultItem) => {
      if (scope && m.claim.scope !== scope) return false;
      if (source && m.claim.source !== source) return false;
      if (query) {
        const hay = [m.claim.text, ...(m.claim.entities ?? []).map((e) => e.name)].join(" ").toLowerCase();
        if (!hay.includes(query)) return false;
      }
      return true;
    },
    [scope, source, query],
  );

  const shownGroups = useMemo(
    () =>
      groups.filter((g) => {
        if (openOnly && g.openThreads === 0) return false;
        const members = membersOf(g);
        if (scope && !members.some((m) => m.claim.scope === scope)) return false;
        if (source && !members.some((m) => m.claim.source === source)) return false;
        if (query) {
          const hay = [g.headline, ...g.entityNames, ...members.map((m) => m.claim.text), ...(g.crystal?.claim.metadata?.key_outcomes ?? [])]
            .join(" ")
            .toLowerCase();
          if (!hay.includes(query)) return false;
        }
        return true;
      }),
    [groups, openOnly, scope, source, query],
  );

  const shownItems = useMemo(
    () => items.filter(matchesItem).sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime()),
    [items, matchesItem],
  );

  const mind = useMemo(() => buildMindGraph(shownItems), [shownItems]);
  const scopesPresent = useMemo(() => {
    const present = new Set(mind.nodes.filter((n) => n.kind === "scope").map((n) => n.scope as Scope));
    return SCOPES.filter((s) => present.has(s.id));
  }, [mind]);

  const anyFilter = Boolean(scope || source || openOnly || query);
  const clearAll = () => {
    setScope(null);
    setSource(null);
    setOpenOnly(false);
    setQ("");
  };

  const subtitle =
    mode === "facts"
      ? `${shownItems.length} of ${items.length} memories`
      : mode === "map"
        ? `${count(mind.entityCount, "entity", "entities")} · ${count(scopesPresent.length, "domain")}`
        : `${shownGroups.length} of ${groups.length} sessions`;

  return (
    <div className="min-h-screen bg-warm-white">
      <AppHeader />
      <main className="mx-auto max-w-2xl px-5 py-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-display text-2xl font-semibold text-ink">Memory</h1>
          {!isLoading && groups.length > 0 && (
            <Segmented
              ariaLabel="Memory view"
              value={mode}
              onChange={setMode}
              options={[
                { value: "list", label: "Journal" },
                { value: "facts", label: "Facts" },
                { value: "map", label: "Map" },
              ]}
            />
          )}
        </div>
        {!isLoading && groups.length > 0 && <p className="mt-1 text-sm text-ink-muted">{subtitle}. Only you can read this.</p>}

        {/* Facets persist across List + Facts (the Map derives from the same filtered set). */}
        {!isLoading && groups.length > 0 && (
          <div className="mt-4 space-y-3">
            <div>
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Filter your memory…"
                aria-label="Filter your memory by keyword"
                className="w-full rounded-control bg-surface px-4 py-2.5 text-ink ring-1 ring-hairline focus:outline-none focus:ring-2 focus:ring-clay"
              />
              <p className="mt-1.5 px-1 text-xs text-ink-muted">Narrows what’s shown. For a written answer, ask your agent.</p>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <Chip active={openOnly} onClick={() => setOpenOnly(!openOnly)}>
                Open threads
              </Chip>
              {scopes.length > 0 && <span className="mx-1 h-4 w-px bg-hairline" aria-hidden />}
              {scopes.map((sc) => (
                <Chip key={sc} active={scope === sc} onClick={() => setScope(scope === sc ? null : sc)}>
                  {cap(sc)}
                </Chip>
              ))}
            </div>
            {sources.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="mr-0.5 text-xs font-semibold text-ink-muted">Source</span>
                {sources.map((sc) => (
                  <Chip key={sc} active={source === sc} onClick={() => setSource(source === sc ? null : sc)}>
                    {sourceShort(sc)}
                  </Chip>
                ))}
                {anyFilter && (
                  <button type="button" onClick={clearAll} className="ml-1 rounded-pill px-2.5 py-1 text-xs font-semibold text-clay-deep transition hover:bg-clay-tint">
                    Clear all
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {isLoading && <p className="mt-8 text-sm text-ink-muted">Decrypting your vault…</p>}
        {isError && (
          <p className="mt-8 rounded-control bg-clay-tint px-3 py-2 text-sm text-clay-deep">
            Couldn’t load your vault: {error instanceof Error ? error.message : String(error)}
          </p>
        )}

        {!isLoading && !isError && groups.length === 0 && fetched > 0 && (
          <div className="mt-10 rounded-card bg-surface p-8 text-center shadow-soft">
            <h2 className="font-display text-xl font-semibold text-ink">Couldn’t read these memories</h2>
            <p className="mx-auto mt-2 max-w-md text-ink-muted">
              Found {fetched} encrypted {fetched === 1 ? "entry" : "entries"} on-chain, but couldn’t decrypt them with this
              key. That usually means a different recovery phrase, or memories written in an older format or on another chain
              than this vault (this app reads Gnosis).
            </p>
          </div>
        )}
        {!isLoading && !isError && groups.length === 0 && fetched === 0 && (
          <div className="mt-10 rounded-card bg-surface p-8 text-center shadow-soft">
            <h2 className="font-display text-xl font-semibold text-ink">No memories yet</h2>
            <p className="mx-auto mt-2 max-w-md text-ink-muted">
              Your agent fills this as you chat with it — it extracts and encrypts each memory; only you can read them. You
              don’t need to pair anything here to browse; your recovery phrase is enough.
            </p>
          </div>
        )}

        {/* List: crystals as prominent hero objects; loose facts sit quietly */}
        {!isLoading && groups.length > 0 && mode === "list" && (
          <>
            {shownGroups.length > 0 ? (
              <div className="mt-6 space-y-4">
                {shownGroups.map((g, i) =>
                  g.crystal ? (
                    <CrystalCard key={g.key} group={g} onOpen={() => openSession(g)} onEntity={openEntity} delay={Math.min(i, 8) * 30} />
                  ) : (
                    <QuietRow key={g.key} group={g} onOpen={() => openSession(g)} />
                  ),
                )}
              </div>
            ) : (
              <NoMatch onClear={clearAll} />
            )}
          </>
        )}

        {/* Facts: flat lens */}
        {!isLoading && groups.length > 0 && mode === "facts" && (
          <>
            {del.isError && (
              <p className="mt-4 rounded-control bg-clay-tint px-3 py-2 text-sm text-clay-deep">
                Couldn’t forget that memory: {del.error instanceof Error ? del.error.message : String(del.error)}
              </p>
            )}
            {pin.isError && (
              <p className="mt-4 rounded-control bg-clay-tint px-3 py-2 text-sm text-clay-deep">
                Couldn’t update that pin: {pin.error instanceof Error ? pin.error.message : String(pin.error)}
              </p>
            )}
            {shownItems.length > 0 ? (
              <div className="mt-6 space-y-3">
                {shownItems.map((it, i) => (
                  <ClaimCard
                    key={it.id}
                    item={it}
                    style={{ animationDelay: `${Math.min(i, 8) * 20}ms` }}
                    onForget={() => del.mutate(it.id)}
                    forgetPending={del.isPending && del.variables === it.id}
                    onTogglePin={() =>
                      pin.mutate({ item: it, target: it.pinned ? "unpinned" : "pinned" })
                    }
                    pinPending={pin.isPending && pin.variables?.item.id === it.id}
                  />
                ))}
              </div>
            ) : (
              <NoMatch onClear={clearAll} />
            )}
          </>
        )}
      </main>

      {/* Map: a full-bleed dark planetarium inset (real derived graph) */}
      {!isLoading && groups.length > 0 && mode === "map" && (
        <div className="mx-auto w-full max-w-4xl px-4 pb-24">
          <p className="mb-4 max-w-2xl text-sm text-ink-muted">
            Your memory as a map. Entities are points of light; the layout shows the shape of what you talk about.
          </p>
          {mind.entityCount === 0 ? (
            <div className="rounded-card bg-surface p-8 text-center shadow-soft">
              <p className="mx-auto max-w-md text-sm text-ink-muted">
                No entities to map yet. They appear once your memories name people, projects, or places.
              </p>
            </div>
          ) : (
            <div className="relative h-[72vh] min-h-[540px] w-full overflow-hidden rounded-[24px] bg-[#211E1B] shadow-overlay ring-1 ring-black/20">
              <Suspense fallback={<div className="flex h-full items-center justify-center text-sm text-warm-white/60">Drawing your map…</div>}>
                <MindMap
                  mode="atlas"
                  nodes={mind.nodes}
                  links={mind.links}
                  neighborsOf={mind.neighborsOf}
                  selectedId={null}
                  onSelect={(n) => {
                    if (n && n.kind === "entity") openEntity(n.label);
                  }}
                />
              </Suspense>

              <div className="pointer-events-none absolute left-4 top-4 max-w-[15rem]">
                <span className="rounded-pill bg-white/10 px-3 py-1 text-sm font-semibold text-warm-white backdrop-blur">Atlas</span>
                <p className="mt-2 pl-1 text-[0.7rem] leading-snug text-warm-white/55">
                  Your entities, clustered into life domains and sized by how often they come up.
                </p>
              </div>

              <div className="pointer-events-none absolute bottom-4 left-4 flex flex-wrap gap-x-3 gap-y-1">
                {scopesPresent.map((sc) => (
                  <span key={sc.id} className="flex items-center gap-1.5 text-[0.7rem] font-medium text-warm-white/75">
                    <span className="h-2 w-2 rounded-full" style={{ backgroundColor: sc.color, boxShadow: `0 0 6px ${sc.color}` }} />
                    {sc.label}
                  </span>
                ))}
              </div>
              <div className="pointer-events-none absolute bottom-4 right-4 hidden text-[0.7rem] text-warm-white/45 sm:block">
                drag · scroll or ± to zoom · tap a star
              </div>
              {mind.cappedEntities > 0 && (
                <div className="pointer-events-none absolute right-4 top-16 max-w-[13rem] text-right text-[0.7rem] text-warm-white/55">
                  Showing the {mind.entityCount - mind.cappedEntities} most-connected of {mind.entityCount} entities.
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <SidePanel view={panel} onClose={close} onOpenSession={openSession} onOpenEntity={openEntity} groups={groups} items={items} />
    </div>
  );
}

/** A Crystal, set as a prominent hero object (distinct from ordinary items). */
function CrystalCard({
  group,
  onOpen,
  onEntity,
  delay,
}: {
  group: SessionGroup;
  onOpen: () => void;
  onEntity: (name: string) => void;
  delay: number;
}) {
  const outcomes = group.crystal?.claim.metadata?.key_outcomes ?? [];
  const imported = importSourceOf(group);
  const shown = group.entityNames.slice(0, 3);
  return (
    <article
      className="animate-fade-up relative overflow-hidden rounded-[20px] bg-surface p-6 shadow-soft ring-1 ring-clay/15 transition duration-200 ease-keeper hover:-translate-y-0.5 hover:shadow-raised"
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="pointer-events-none absolute -right-10 -top-10 h-40 w-40 rounded-full bg-clay/[0.07] blur-2xl" aria-hidden />
      <button onClick={onOpen} className="block w-full text-left focus:outline-none">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-[0.66rem] font-bold uppercase tracking-[0.12em] text-clay-deep">Crystal</span>
          <span className="h-1 w-1 rounded-full bg-hairline" aria-hidden />
          <time className="font-mono text-xs text-ink-muted">{relativeDate(group.date)}</time>
          {imported && (
            <span className="rounded-pill bg-clay-tint px-2 py-0.5 text-[0.62rem] font-bold uppercase tracking-wide text-clay-deep">
              Imported · {imported}
            </span>
          )}
        </div>
        <h2 className="mt-2.5 font-display text-[1.5rem] font-medium leading-[1.2] text-ink" style={{ textWrap: "balance" }}>
          {group.crystal?.claim.text ?? group.headline}
        </h2>
        {outcomes.length > 0 && (
          <ul className="mt-3.5 space-y-2">
            {outcomes.slice(0, 2).map((o) => (
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
          {count(group.facts.length, "memory", "memories")} · {count(group.openThreads, "thread")}
        </span>
        {shown.map((e) => (
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

/** A session with no Crystal (loose facts / singletons) — quiet + secondary. */
function QuietRow({ group, onOpen }: { group: SessionGroup; onOpen: () => void }) {
  const imported = importSourceOf(group);
  return (
    <button
      onClick={onOpen}
      className="animate-fade-up block w-full rounded-2xl border border-hairline bg-surface p-4 text-left shadow-soft transition hover:-translate-y-0.5 hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
    >
      <div className="flex flex-wrap items-center gap-2">
        <time className="font-mono text-xs text-ink-muted">{relativeDate(group.date)}</time>
        {imported && (
          <span className="rounded-pill bg-clay-tint px-2 py-0.5 text-[0.62rem] font-bold uppercase tracking-wide text-clay-deep">
            Imported · {imported}
          </span>
        )}
      </div>
      <p className="mt-1.5 font-display text-[1.05rem] leading-snug text-ink line-clamp-2" style={{ textWrap: "pretty" }}>
        {group.headline}
      </p>
      <span className="mt-2 inline-block font-mono text-[0.7rem] text-ink-muted">{count(group.facts.length, "memory", "memories")}</span>
    </button>
  );
}

function NoMatch({ onClear }: { onClear: () => void }) {
  return (
    <div className="mt-8 rounded-card bg-surface p-8 text-center shadow-soft">
      <p className="text-sm text-ink-muted">Nothing matches these filters.</p>
      <button type="button" onClick={onClear} className="mt-2 text-sm font-semibold text-clay-deep hover:underline">
        Clear all filters
      </button>
    </div>
  );
}
