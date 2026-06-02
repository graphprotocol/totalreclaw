import { TypeBadge } from "../components/TypeBadge";
import { factsForNode, kindOf, labelOf, neighborRefs } from "./explore-data";

interface Props {
  nodeId: string | null;
  onSelect: (id: string) => void;
}

export function NodeDetail({ nodeId, onSelect }: Props) {
  if (!nodeId) {
    return (
      <div className="p-5 text-sm leading-relaxed text-ink-muted">
        Tap a topic or entity in the graph to see what you know about it.
      </div>
    );
  }

  const label = labelOf(nodeId);
  const kind = kindOf(nodeId);
  const neighbors = neighborRefs(nodeId);
  const facts = factsForNode(nodeId);

  return (
    <div className="p-5">
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-muted">{kind}</div>
      <h3 className="font-display text-xl text-ink">{label}</h3>

      {neighbors.length > 0 && (
        <div className="mt-3">
          <div className="mb-1.5 text-xs font-semibold text-ink-muted">Connected</div>
          <div className="flex flex-wrap gap-1.5">
            {neighbors.map((n) => (
              <button
                key={n.id}
                onClick={() => onSelect(n.id)}
                className="rounded-pill border border-hairline bg-surface px-2.5 py-1 text-xs font-semibold text-ink-muted transition duration-150 ease-keeper hover:border-clay/40 hover:bg-clay-tint hover:text-clay-deep"
              >
                {n.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="mt-4">
        <div className="mb-2 text-xs font-semibold text-ink-muted">
          {facts.length} {facts.length === 1 ? "fact" : "facts"}
        </div>
        <ul className="space-y-2.5">
          {facts.map((f) => (
            <li key={f.id} className="rounded-control bg-warm-white p-3">
              <div className="mb-1.5">
                <TypeBadge type={f.type} />
              </div>
              <p className="font-display text-[0.95rem] leading-snug text-ink">{f.text}</p>
            </li>
          ))}
          {facts.length === 0 && (
            <li className="text-sm text-ink-muted">No facts captured for this yet.</li>
          )}
        </ul>
      </div>
    </div>
  );
}
