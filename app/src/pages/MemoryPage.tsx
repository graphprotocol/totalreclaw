import { useMemo, useState, type ReactNode } from "react";
import { clsx } from "clsx";
import { useCrypto } from "../contexts/CryptoContext";
import { useVault } from "../hooks/useVault";
import { buildTimeline, sessionSlug, type SessionGroup } from "../lib/vault/timeline";
import { AppHeader } from "../components/AppHeader";
import { SessionCard } from "../components/SessionCard";
import { sourceShort, cap } from "../lib/presentation";
import type { VaultItem } from "../lib/types";

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

  const [q, setQ] = useState("");
  const [scope, setScope] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [openOnly, setOpenOnly] = useState(false);
  const [entity, setEntity] = useState<string | null>(null);

  const groups = useMemo(() => buildTimeline(items), [items]);

  const scopes = useMemo(
    () =>
      [...new Set(items.map((i) => i.claim.scope).filter((s): s is string => !!s && s !== "unspecified"))].sort(),
    [items],
  );
  const sources = useMemo(
    () => [...new Set(items.map((i) => i.claim.source).filter((s): s is string => !!s))].sort(),
    [items],
  );

  const query = q.trim().toLowerCase();
  const shown = useMemo(
    () =>
      groups.filter((g) => {
        if (openOnly && g.openThreads === 0) return false;
        if (entity && !g.entityNames.includes(entity)) return false;
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
    [groups, openOnly, entity, scope, source, query],
  );

  const anyFilter = Boolean(scope || source || openOnly || entity || query);
  const clearAll = () => {
    setScope(null);
    setSource(null);
    setOpenOnly(false);
    setEntity(null);
    setQ("");
  };

  return (
    <div className="min-h-screen bg-warm-white">
      <AppHeader />
      <main className="mx-auto max-w-2xl px-5 py-6">
        <h1 className="font-display text-2xl font-semibold text-ink">Memory</h1>
        {!isLoading && groups.length > 0 && (
          <p className="mt-1 text-sm text-ink-muted">
            {shown.length} of {groups.length}
            {entity ? ` · about ${entity}` : ""}. Only you can read this.
          </p>
        )}

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

            {entity && (
              <span className="inline-flex items-center gap-1.5 rounded-pill bg-clay px-3 py-1 text-xs font-semibold text-warm-white">
                {entity}
                <button
                  type="button"
                  onClick={() => setEntity(null)}
                  aria-label={`Clear ${entity} filter`}
                  className="text-warm-white/80 transition hover:text-warm-white"
                >
                  ✕
                </button>
              </span>
            )}

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

        {!isLoading && shown.length > 0 && (
          <div className="mt-6 space-y-4">
            {shown.map((g, i) => (
              <SessionCard
                key={g.key}
                group={g}
                href={`/memory/session/${sessionSlug(g)}`}
                onEntityClick={setEntity}
                style={{ animationDelay: `${Math.min(i, 8) * 30}ms` }}
              />
            ))}
          </div>
        )}

        {!isLoading && groups.length > 0 && shown.length === 0 && (
          <div className="mt-8 rounded-card bg-surface p-8 text-center shadow-soft">
            <p className="text-sm text-ink-muted">No memories match these filters.</p>
            <button
              type="button"
              onClick={clearAll}
              className="mt-2 text-sm font-semibold text-clay-deep hover:underline"
            >
              Clear all filters
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
