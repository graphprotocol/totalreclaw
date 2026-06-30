import type { KGNode, KGLink } from "../../lib/vault/graph";

/** Topics → entities outline. Ported from the proto, on derived graph data.
 *  Topics come only from session Crystals (metadata.topics_discussed), so this
 *  is sparse on import-heavy vaults — the empty state is handled by the caller. */
export function TopicsTree({
  nodes,
  links,
  onOpenEntity,
}: {
  nodes: KGNode[];
  links: KGLink[];
  onOpenEntity: (name: string) => void;
}) {
  const topics = nodes.filter((n) => n.kind === "topic");
  const byId = new Map(nodes.map((n) => [n.id, n]));

  return (
    <div className="mt-6 space-y-3">
      {topics.map((t) => {
        const linkedIds = links
          .filter((l) => l.source === t.id || l.target === t.id)
          .map((l) => (l.source === t.id ? l.target : l.source));
        const entities = linkedIds
          .map((id) => byId.get(id))
          .filter((n): n is KGNode => !!n && n.kind === "entity");
        return (
          <div key={t.id} className="animate-fade-up rounded-card bg-surface p-4 shadow-soft">
            <h3 className="font-display text-lg text-ink">{t.label}</h3>
            {entities.length > 0 ? (
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {entities.map((e) => (
                  <button
                    key={e.id}
                    onClick={() => onOpenEntity(e.label)}
                    className="rounded-pill bg-warm-white px-2.5 py-1 font-mono text-xs text-ink-muted ring-1 ring-hairline transition hover:text-ink hover:ring-clay/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
                  >
                    {e.label}
                  </button>
                ))}
              </div>
            ) : (
              <p className="mt-1.5 text-sm text-ink-muted">No linked entities.</p>
            )}
          </div>
        );
      })}
    </div>
  );
}
