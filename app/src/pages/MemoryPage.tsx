import { useMemo, useState } from "react";
import { useCrypto } from "../contexts/CryptoContext";
import { useVault } from "../hooks/useVault";
import { buildTimeline, sessionSlug } from "../lib/vault/timeline";
import { AppHeader } from "../components/AppHeader";
import { SessionCard } from "../components/SessionCard";

export function MemoryPage() {
  const { keys } = useCrypto();
  const { data, isLoading, isError, error } = useVault(keys);
  const items = data?.items ?? [];
  const fetched = data?.fetched ?? 0;
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

        {/* Fetched ciphertext but none decrypted → wrong key / older format / wrong chain. */}
        {!isLoading && !isError && groups.length === 0 && fetched > 0 && (
          <div className="mt-10 rounded-card bg-surface p-8 text-center shadow-soft">
            <h2 className="font-display text-xl font-semibold text-ink">
              Couldn’t read these memories
            </h2>
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
