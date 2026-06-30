import { lazy, Suspense, useCallback, useMemo, useState, type ReactNode } from "react";
import { clsx } from "clsx";
import { useCrypto } from "../contexts/CryptoContext";
import { useVault } from "../hooks/useVault";
import { buildTimeline, type SessionGroup } from "../lib/vault/timeline";
import { buildGraph } from "../lib/vault/graph";
import { AppHeader } from "../components/AppHeader";
import { SessionCard } from "../components/SessionCard";
import { ClaimCard } from "../components/ClaimCard";
import { Segmented } from "../components/memory/Segmented";
import { SidePanel, type PanelView } from "../components/memory/SidePanel";
import { RailTimeline, ActivityTimeline } from "../components/memory/Timeline";
import { TopicsTree } from "../components/memory/TopicsTree";
import { sourceShort, cap } from "../lib/presentation";
import { count } from "../lib/format";
import type { VaultItem } from "../lib/types";

// Keeps @xyflow/react out of the Memory landing bundle — only pulled on Graph mode.
const EntityGraph = lazy(() =>
  import("../components/memory/EntityGraph").then((m) => ({ default: m.EntityGraph })),
);

type Mode = "list" | "facts" | "timeline" | "graph";
type TimelineVariant = "rail" | "activity";
type GraphVariant = "entities" | "topics";

function membersOf(g: SessionGroup): VaultItem[] {
  return g.crystal ? [g.crystal, ...g.facts] : g.facts;
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
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
  const [timelineVariant, setTimelineVariant] = useState<TimelineVariant>("rail");
  const [graphVariant, setGraphVariant] = useState<GraphVariant>("entities");
  const [graphSel, setGraphSel] = useState<string | null>(null);
  const [panel, setPanel] = useState<PanelView | null>(null);

  const [q, setQ] = useState("");
  const [scope, setScope] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [openOnly, setOpenOnly] = useState(false);

  const close = useCallback(() => setPanel(null), []);
  const openSession = useCallback((g: SessionGroup) => setPanel({ kind: "session", group: g }), []);
  const openEntity = useCallback((name: string) => setPanel({ kind: "entity", name }), []);

  const groups = useMemo(() => buildTimeline(items), [items]);

  const scopes = useMemo(
    () =>
      [
        ...new Set(
          items.map((i) => i.claim.scope).filter((s): s is string => !!s && s !== "unspecified"),
        ),
      ].sort(),
    [items],
  );
  const sources = useMemo(
    () => [...new Set(items.map((i) => i.claim.source).filter((s): s is string => !!s))].sort(),
    [items],
  );

  const query = q.trim().toLowerCase();

  const matchesItem = useCallback(
    (m: VaultItem) => {
      if (scope && m.claim.scope !== scope) return false;
      if (source && m.claim.source !== source) return false;
      if (query) {
        const hay = [m.claim.text, ...(m.claim.entities ?? []).map((e) => e.name)]
          .join(" ")
          .toLowerCase();
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
          const hay = [
            g.headline,
            ...g.entityNames,
            ...members.map((m) => m.claim.text),
            ...(g.crystal?.claim.metadata?.key_outcomes ?? []),
          ]
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

  const graph = useMemo(() => buildGraph(shownGroups), [shownGroups]);
  const nodeById = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph]);

  const anyFilter = Boolean(scope || source || openOnly || query);
  const clearAll = () => {
    setScope(null);
    setSource(null);
    setOpenOnly(false);
    setQ("");
  };

  const subtitle = (() => {
    if (mode === "facts") return `${shownItems.length} of ${items.length} memories`;
    if (mode === "graph") {
      const parts = [
        count(graph.entityCount, "entity", "entities"),
        count(graph.topicCount, "topic"),
      ];
      return parts.join(" · ");
    }
    return `${shownGroups.length} of ${groups.length} sessions`;
  })();

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
                { value: "list", label: "List" },
                { value: "facts", label: "Facts" },
                { value: "timeline", label: "Timeline" },
                { value: "graph", label: "Graph" },
              ]}
            />
          )}
        </div>
        {!isLoading && groups.length > 0 && (
          <p className="mt-1 text-sm text-ink-muted">{subtitle}. Only you can read this.</p>
        )}

        {/* Facets persist across modes. */}
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
              <p className="mt-1.5 px-1 text-xs text-ink-muted">
                Narrows what’s shown. For a written answer, ask your agent.
              </p>
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
                  <button
                    type="button"
                    onClick={clearAll}
                    className="ml-1 rounded-pill px-2.5 py-1 text-xs font-semibold text-clay-deep transition hover:bg-clay-tint"
                  >
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

        {/* Fetched ciphertext but none decrypted → wrong key / older format / wrong chain. */}
        {!isLoading && !isError && groups.length === 0 && fetched > 0 && (
          <div className="mt-10 rounded-card bg-surface p-8 text-center shadow-soft">
            <h2 className="font-display text-xl font-semibold text-ink">Couldn’t read these memories</h2>
            <p className="mx-auto mt-2 max-w-md text-ink-muted">
              Found {fetched} encrypted {fetched === 1 ? "entry" : "entries"} on-chain, but couldn’t
              decrypt them with this key. That usually means a different recovery phrase, or memories
              written in an older format or on another chain than this vault (this app reads Gnosis).
            </p>
          </div>
        )}

        {/* Genuinely nothing on-chain for this account. */}
        {!isLoading && !isError && groups.length === 0 && fetched === 0 && (
          <div className="mt-10 rounded-card bg-surface p-8 text-center shadow-soft">
            <h2 className="font-display text-xl font-semibold text-ink">No memories yet</h2>
            <p className="mx-auto mt-2 max-w-md text-ink-muted">
              Your agent fills this as you chat with it — it extracts and encrypts each memory; only
              you can read them. You don’t need to pair anything here to browse; your recovery phrase
              is enough.
            </p>
            <p className="mx-auto mt-3 max-w-md text-sm text-ink-muted">
              Expecting memories? Make sure your agent uses the <em>same recovery phrase</em> and is
              on <em>Gnosis</em> (chain 100) — older Base Sepolia vaults won’t appear here.
            </p>
          </div>
        )}

        {/* ── List: gallery of session cards → panel ── */}
        {!isLoading && groups.length > 0 && mode === "list" && (
          <>
            {shownGroups.length > 0 ? (
              <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
                {shownGroups.map((g, i) => (
                  <SessionCard
                    key={g.key}
                    group={g}
                    onOpen={() => openSession(g)}
                    onEntityClick={openEntity}
                    style={{ animationDelay: `${Math.min(i, 8) * 30}ms` }}
                  />
                ))}
              </div>
            ) : (
              <NoMatch onClear={clearAll} />
            )}
          </>
        )}

        {/* ── Facts: flat "everything you remember" lens ── */}
        {!isLoading && groups.length > 0 && mode === "facts" && (
          <>
            {shownItems.length > 0 ? (
              <div className="mt-6 space-y-3">
                {shownItems.map((it, i) => (
                  <ClaimCard key={it.id} item={it} style={{ animationDelay: `${Math.min(i, 8) * 20}ms` }} />
                ))}
              </div>
            ) : (
              <NoMatch onClear={clearAll} />
            )}
          </>
        )}

        {/* ── Timeline: Rail · Activity ── */}
        {!isLoading && groups.length > 0 && mode === "timeline" && (
          <div className="mt-6">
            <Segmented
              size="sm"
              ariaLabel="Timeline variant"
              value={timelineVariant}
              onChange={setTimelineVariant}
              options={[
                { value: "rail", label: "Rail" },
                { value: "activity", label: "Activity" },
              ]}
            />
            {shownGroups.length === 0 ? (
              <NoMatch onClear={clearAll} />
            ) : timelineVariant === "rail" ? (
              <RailTimeline groups={shownGroups} onOpen={openSession} />
            ) : (
              <ActivityTimeline groups={shownGroups} onOpen={openSession} />
            )}
          </div>
        )}

        {/* ── Graph: Entities · Topics ── */}
        {!isLoading && groups.length > 0 && mode === "graph" && (
          <div className="mt-6">
            <Segmented
              size="sm"
              ariaLabel="Graph variant"
              value={graphVariant}
              onChange={setGraphVariant}
              options={[
                { value: "entities", label: "Entities" },
                { value: "topics", label: "Topics" },
              ]}
            />

            {graphVariant === "entities" ? (
              graph.nodes.length > 0 ? (
                <>
                  <div className="mt-6 h-[460px] overflow-hidden rounded-card bg-surface shadow-soft">
                    <Suspense
                      fallback={
                        <div className="flex h-full items-center justify-center text-sm text-ink-muted">
                          Drawing your graph…
                        </div>
                      }
                    >
                      <EntityGraph
                        nodes={graph.nodes}
                        links={graph.links}
                        neighborsOf={graph.neighborsOf}
                        selectedId={graphSel}
                        onSelect={(id) => {
                          setGraphSel(id);
                          if (!id) return;
                          const node = nodeById.get(id);
                          if (node?.kind === "entity") openEntity(node.label);
                        }}
                      />
                    </Suspense>
                  </div>
                  {graph.cappedEntities > 0 && (
                    <p className="mt-2 text-xs text-ink-muted">
                      Showing the {graph.entityCount - graph.cappedEntities} most-connected of{" "}
                      {graph.entityCount} entities. Use Facts or filters to reach the rest.
                    </p>
                  )}
                </>
              ) : (
                <EmptyHint>No entities to map yet. They appear once your memories name people, projects, or places.</EmptyHint>
              )
            ) : graph.topicCount > 0 ? (
              <TopicsTree nodes={graph.nodes} links={graph.links} onOpenEntity={openEntity} />
            ) : (
              <EmptyHint>
                No topics yet. Topics come from session Crystals (written by agents like Hermes at
                session end) — imported memories don’t carry them.
              </EmptyHint>
            )}
          </div>
        )}
      </main>

      <SidePanel
        view={panel}
        onClose={close}
        onOpenSession={openSession}
        onOpenEntity={openEntity}
        groups={groups}
        items={items}
      />
    </div>
  );
}

function NoMatch({ onClear }: { onClear: () => void }) {
  return (
    <div className="mt-8 rounded-card bg-surface p-8 text-center shadow-soft">
      <p className="text-sm text-ink-muted">Nothing matches these filters.</p>
      <button
        type="button"
        onClick={onClear}
        className="mt-2 text-sm font-semibold text-clay-deep hover:underline"
      >
        Clear all filters
      </button>
    </div>
  );
}

function EmptyHint({ children }: { children: ReactNode }) {
  return (
    <div className="mt-6 rounded-card bg-surface p-8 text-center shadow-soft">
      <p className="mx-auto max-w-md text-sm text-ink-muted">{children}</p>
    </div>
  );
}
