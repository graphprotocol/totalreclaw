import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useCrypto } from "../contexts/CryptoContext";
import { useVaultHistory } from "../hooks/useVault";
import { listChangedChains } from "../lib/vault/lineage";
import { AppHeader } from "../components/AppHeader";
import { relativeDate } from "../lib/format";
import type { VaultItem } from "../lib/types";

const STILL_TRUE_AGE_DAYS = 30;

function ageDays(d: Date): number {
  return (Date.now() - d.getTime()) / 86_400_000;
}

function NeedsYouCard({ item }: { item: VaultItem }) {
  return (
    <article className="animate-fade-up rounded-card bg-surface p-5 shadow-soft">
      <span className="text-xs font-semibold uppercase tracking-wide text-clay-deep">Still true?</span>
      <p className="mt-2 font-display text-lg leading-snug text-ink" style={{ textWrap: "pretty" }}>
        {item.claim.text}
      </p>
      <p className="mt-1 text-xs text-ink-muted">
        Last touched {relativeDate(item.createdAt)} · marked {item.claim.volatility ?? "updatable"}
      </p>
      <div className="mt-3 flex gap-2">
        <button
          disabled
          title="Curation arrives in the next phase"
          className="cursor-not-allowed rounded-control bg-warm-white px-3 py-1.5 text-sm font-semibold text-ink-muted ring-1 ring-hairline"
        >
          Still true
        </button>
        <button
          disabled
          title="Curation arrives in the next phase"
          className="cursor-not-allowed rounded-control px-3 py-1.5 text-sm font-semibold text-ink-muted"
        >
          Needs updating
        </button>
      </div>
    </article>
  );
}

function ChangedCard({ chain }: { chain: VaultItem[] }) {
  const latest = chain[chain.length - 1];
  const earlier = chain.length - 1;
  return (
    <article className="animate-fade-up rounded-card bg-surface p-5 shadow-soft">
      <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Updated for you</span>
      <p className="mt-2 font-display text-lg leading-snug text-ink" style={{ textWrap: "pretty" }}>
        {latest.claim.text}
      </p>
      <p className="mt-1 text-xs text-ink-muted">
        Replaced {earlier} earlier version{earlier > 1 ? "s" : ""} · {relativeDate(latest.createdAt)}
      </p>
      <Link
        to={`/lineage/${latest.claim.id}`}
        className="mt-3 inline-block text-sm font-semibold text-clay-deep hover:underline"
      >
        See how this belief evolved →
      </Link>
    </article>
  );
}

export function ReviewPage() {
  const { keys } = useCrypto();
  const { data: history = [], isLoading } = useVaultHistory(keys);

  const stale = useMemo(
    () =>
      history
        .filter((i) => i.isActive)
        .filter((i) => (i.claim.volatility ?? "updatable") !== "stable")
        .filter((i) => ageDays(i.createdAt) > STILL_TRUE_AGE_DAYS)
        .sort((a, b) => a.createdAt.getTime() - b.createdAt.getTime())
        .slice(0, 6),
    [history],
  );
  const changed = useMemo(() => listChangedChains(history).slice(0, 6), [history]);

  const nothing = !isLoading && stale.length === 0 && changed.length === 0;

  return (
    <div className="min-h-screen bg-warm-white">
      <AppHeader />
      <main className="mx-auto max-w-2xl px-5 py-6">
        <h1 className="font-display text-2xl font-semibold text-ink">Review</h1>
        <p className="mt-1 text-ink-muted">What your memory needs from you — and what was handled for you.</p>

        {isLoading && <p className="mt-8 text-sm text-ink-muted">Checking your memory…</p>}

        {nothing && (
          <div className="mt-10 rounded-card bg-surface p-8 text-center shadow-soft">
            <h2 className="font-display text-xl font-semibold text-ink">All clear</h2>
            <p className="mx-auto mt-2 max-w-sm text-ink-muted">
              Nothing needs your attention right now.
            </p>
          </div>
        )}

        {stale.length > 0 && (
          <section className="mt-8">
            <h2 className="text-sm font-semibold text-ink">Needs you</h2>
            <div className="mt-3 space-y-3">
              {stale.map((i) => (
                <NeedsYouCard key={i.id} item={i} />
              ))}
            </div>
          </section>
        )}

        {changed.length > 0 && (
          <section className="mt-8">
            <h2 className="text-sm font-semibold text-ink">Handled for you</h2>
            <div className="mt-3 space-y-3">
              {changed.map((c) => (
                <ChangedCard key={c[c.length - 1].id} chain={c} />
              ))}
            </div>
          </section>
        )}

        {!isLoading && (
          <p className="mt-10 border-t border-hairline pt-4 text-xs text-ink-muted">
            These cards ride memory you can decrypt: <span className="font-semibold">volatility</span>{" "}
            (still-true?) and <span className="font-semibold">supersession</span> (updated). Conflict
            detection is on the roadmap (backend #306) and isn’t shown yet. Curation actions arrive
            with writes.
          </p>
        )}
      </main>
    </div>
  );
}
