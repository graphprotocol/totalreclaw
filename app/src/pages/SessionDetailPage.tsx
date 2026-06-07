import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { useCrypto } from "../contexts/CryptoContext";
import { useVault } from "../hooks/useVault";
import { buildTimeline, sessionSlug } from "../lib/vault/timeline";
import { AppHeader } from "../components/AppHeader";
import { ClaimCard } from "../components/ClaimCard";
import { EntityChip } from "../components/EntityChip";
import { relativeDate } from "../lib/format";

function CrystalList({ title, items }: { title: string; items: string[] }) {
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

export function SessionDetailPage() {
  const { slug } = useParams();
  const { keys } = useCrypto();
  const { data: items = [], isLoading } = useVault(keys);

  const group = useMemo(
    () => buildTimeline(items).find((g) => sessionSlug(g) === slug) ?? null,
    [items, slug],
  );

  const crystalMeta = group?.crystal?.claim.metadata;

  return (
    <div className="min-h-screen bg-warm-white">
      <AppHeader />
      <main className="mx-auto max-w-2xl px-5 py-6">
        <Link to="/memory" className="text-sm font-semibold text-ink-muted hover:text-ink">
          ← Memory
        </Link>

        {isLoading && <p className="mt-8 text-sm text-ink-muted">Decrypting…</p>}

        {!isLoading && !group && (
          <p className="mt-8 text-sm text-ink-muted">That session couldn’t be found.</p>
        )}

        {group && (
          <>
            <time className="mt-4 block font-mono text-xs text-ink-muted">
              {relativeDate(group.date)}
            </time>
            <h1
              className="mt-1 font-display text-2xl font-semibold leading-snug text-ink"
              style={{ textWrap: "pretty" }}
            >
              {group.crystal?.claim.text ?? group.headline}
            </h1>

            {crystalMeta && (
              <>
                <CrystalList title="Key outcomes" items={crystalMeta.key_outcomes ?? []} />
                <CrystalList title="Open threads" items={crystalMeta.open_threads ?? []} />
                <CrystalList title="Lessons" items={crystalMeta.lessons ?? []} />
              </>
            )}

            {group.entityNames.length > 0 && (
              <div className="mt-5 flex flex-wrap gap-2">
                {group.entityNames.map((e) => (
                  <EntityChip key={e} label={e} />
                ))}
              </div>
            )}

            <h2 className="mt-8 text-xs font-semibold uppercase tracking-wide text-ink-muted">
              {group.facts.length > 0 ? "Memories from this session" : "No atomic memories"}
            </h2>
            <div className="mt-3 space-y-3">
              {group.facts.map((item, i) => (
                <ClaimCard key={item.id} item={item} style={{ animationDelay: `${Math.min(i, 8) * 25}ms` }} />
              ))}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
