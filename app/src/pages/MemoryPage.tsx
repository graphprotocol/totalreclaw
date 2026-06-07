import { useMemo, useState } from "react";
import { useCrypto } from "../contexts/CryptoContext";
import { useVault } from "../hooks/useVault";
import { buildTimeline, sessionSlug } from "../lib/vault/timeline";
import { AppHeader } from "../components/AppHeader";
import { SessionCard } from "../components/SessionCard";

export function MemoryPage() {
  const { keys } = useCrypto();
  const { data: items = [], isLoading, isError, error } = useVault(keys);
  const [filter, setFilter] = useState("");

  const groups = useMemo(() => buildTimeline(items), [items]);

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return groups;
    return groups.filter((g) => {
      if (g.headline.toLowerCase().includes(q)) return true;
      if (g.entityNames.some((e) => e.toLowerCase().includes(q))) return true;
      return g.facts.some((f) => f.claim.text.toLowerCase().includes(q));
    });
  }, [groups, filter]);

  return (
    <div className="min-h-screen bg-warm-white">
      <AppHeader />
      <main className="mx-auto max-w-2xl px-5 py-6">
        <h1 className="font-display text-2xl font-semibold text-ink">Memory</h1>

        {!isLoading && groups.length > 0 && (
          <div className="mt-4">
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter your memory…"
              className="w-full rounded-control bg-surface px-4 py-2.5 text-ink ring-1 ring-hairline focus:outline-none focus:ring-2 focus:ring-clay"
            />
            <p className="mt-1.5 px-1 text-xs text-ink-muted">
              Narrows what’s shown. For a written answer, ask your agent.
            </p>
          </div>
        )}

        {isLoading && <p className="mt-8 text-sm text-ink-muted">Decrypting your vault…</p>}

        {isError && (
          <p className="mt-8 rounded-control bg-clay-tint px-3 py-2 text-sm text-clay-deep">
            Couldn’t load your vault: {error instanceof Error ? error.message : String(error)}
          </p>
        )}

        {!isLoading && !isError && groups.length === 0 && (
          <div className="mt-10 rounded-card bg-surface p-8 text-center shadow-soft">
            <h2 className="font-display text-xl font-semibold text-ink">Your vault is empty</h2>
            <p className="mx-auto mt-2 max-w-sm text-ink-muted">
              Memories appear here once you pair an agent and start a conversation. Your agent
              extracts and encrypts them; only you can read them.
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
                style={{ animationDelay: `${Math.min(i, 8) * 30}ms` }}
              />
            ))}
          </div>
        )}

        {!isLoading && groups.length > 0 && shown.length === 0 && (
          <p className="mt-8 text-sm text-ink-muted">No memories match “{filter}”.</p>
        )}
      </main>
    </div>
  );
}
