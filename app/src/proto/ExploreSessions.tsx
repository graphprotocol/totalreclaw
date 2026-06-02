import { SessionCard } from "./SessionCard";
import { SEED_SESSIONS } from "./seed";
import { labelOf, sessionsForNode } from "./explore-data";

interface Props {
  nodeId: string | null;
  onClear: () => void;
  /** Small uppercase header to sit inside a detail panel, vs a standalone h3. */
  compact?: boolean;
}

export function ExploreSessions({ nodeId, onClear, compact }: Props) {
  const sessions = nodeId ? sessionsForNode(nodeId) : SEED_SESSIONS;

  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-2">
        {compact ? (
          <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
            {nodeId ? `Sessions · ${sessions.length}` : "All sessions"}
          </span>
        ) : (
          <h3 className="font-display text-lg text-ink">
            {nodeId ? `Sessions · ${labelOf(nodeId)}` : "All sessions"}
          </h3>
        )}
        {nodeId && (
          <button
            onClick={onClear}
            className="shrink-0 rounded-pill px-2.5 py-1 text-xs font-semibold text-clay-deep transition hover:bg-clay-tint"
          >
            Clear
          </button>
        )}
      </div>
      <div className="space-y-3">
        {sessions.map((s, i) => (
          <SessionCard
            key={s.id}
            session={s}
            style={{ animationDelay: `${i * 50}ms` }}
            href={`/proto/session/${s.id}`}
          />
        ))}
        {sessions.length === 0 && (
          <p className="text-sm text-ink-muted">No sessions mention this yet.</p>
        )}
      </div>
    </div>
  );
}
