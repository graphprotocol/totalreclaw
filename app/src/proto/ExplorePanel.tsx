import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { clsx } from "clsx";
import { SEED_SESSIONS, relativeDate, type SeedFact, type SeedSession } from "./seed";
import { kindOf, labelOf, neighborRefs, sessionsForNode } from "./explore-data";
import { sourceLabel, typeBucket } from "./presentation";

const BUCKET_TONE: Record<string, string> = {
  rule: "bg-clay-tint text-clay-deep",
  todo: "bg-type-commitment text-type-commitment-ink",
  pref: "bg-type-preference text-type-preference-ink",
};

function FactRow({ fact }: { fact: SeedFact }) {
  const bucket = typeBucket(fact.type);
  return (
    <li className="rounded-control bg-warm-white p-3">
      <div className="mb-1 flex flex-wrap items-center gap-2 text-xs font-semibold text-ink-muted">
        <span className="inline-flex items-center gap-1.5">
          <span
            className={clsx("h-1.5 w-1.5 rounded-full", fact.source === "user" ? "bg-clay" : "bg-ink-muted")}
            aria-hidden
          />
          {sourceLabel(fact.source)}
        </span>
        {bucket && (
          <span className={clsx("rounded-pill px-2 py-0.5", BUCKET_TONE[bucket.tone])}>{bucket.label}</span>
        )}
      </div>
      <p className="font-display text-[0.95rem] leading-snug text-ink">{fact.text}</p>
    </li>
  );
}

/** Drill view: a single session's Crystal + memories, replacing the session list in place. */
function SessionDrill({ session, onBack }: { session: SeedSession; onBack: () => void }) {
  const c = session.crystal;
  return (
    <div className="animate-fade-up p-5">
      <button
        type="button"
        onClick={onBack}
        className="mb-3 inline-flex items-center gap-1 text-sm font-semibold text-ink-muted transition hover:text-ink"
      >
        ← Sessions
      </button>
      <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
        <span className="h-1.5 w-1.5 rounded-full bg-clay" aria-hidden /> Crystal · {relativeDate(session.date)}
      </div>
      <p className="font-display text-lg leading-snug text-ink">{c.narrative}</p>

      {c.openThreads.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-muted">Open threads</div>
          <ul className="space-y-1">
            {c.openThreads.map((t) => (
              <li key={t} className="flex gap-2 text-sm leading-snug text-ink">
                <span className="mt-2 h-2 w-2 shrink-0 rounded-full border border-clay" aria-hidden />
                <span>{t}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-ink-muted">
        {session.facts.length} memories
      </div>
      <ul className="space-y-2.5">
        {session.facts.map((f) => (
          <FactRow key={f.id} fact={f} />
        ))}
      </ul>

      <Link
        to={`/proto/session/${session.id}`}
        className="mt-4 inline-block text-sm font-semibold text-clay-deep hover:underline"
      >
        Open full session →
      </Link>
    </div>
  );
}

interface Props {
  nodeId: string | null;
  onSelectNode: (id: string) => void;
}

/** Right panel: node → its sessions (summaries first); open a session to drill into its memories. */
export function ExplorePanel({ nodeId, onSelectNode }: Props) {
  const [openId, setOpenId] = useState<string | null>(null);
  useEffect(() => setOpenId(null), [nodeId]); // reset drill when the selected node changes

  if (!nodeId) {
    return (
      <div className="p-5 text-sm leading-relaxed text-ink-muted">
        Tap a topic or entity in the graph to see its sessions.
      </div>
    );
  }

  if (openId) {
    const session = SEED_SESSIONS.find((s) => s.id === openId);
    if (session) return <SessionDrill session={session} onBack={() => setOpenId(null)} />;
  }

  const neighbors = neighborRefs(nodeId);
  const sessions = sessionsForNode(nodeId);

  return (
    <div className="animate-fade-up p-5">
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-muted">{kindOf(nodeId)}</div>
      <h3 className="font-display text-xl text-ink">{labelOf(nodeId)}</h3>

      {neighbors.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {neighbors.map((n) => (
            <button
              key={n.id}
              type="button"
              onClick={() => onSelectNode(n.id)}
              className="rounded-pill border border-hairline bg-surface px-2.5 py-1 text-xs font-semibold text-ink-muted transition duration-150 ease-keeper hover:border-clay/40 hover:bg-clay-tint hover:text-clay-deep"
            >
              {n.label}
            </button>
          ))}
        </div>
      )}

      <div className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-ink-muted">
        {sessions.length} {sessions.length === 1 ? "session" : "sessions"}
      </div>
      <div className="space-y-2.5">
        {sessions.map((s) => (
          <button
            key={s.id}
            type="button"
            onClick={() => setOpenId(s.id)}
            className="block w-full rounded-card bg-warm-white p-3.5 text-left shadow-soft transition duration-150 ease-keeper hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
          >
            <div className="mb-1 flex items-center justify-between gap-2 text-xs text-ink-muted">
              <span className="font-mono">{relativeDate(s.date)}</span>
              <span>
                {s.facts.length} facts · {s.crystal.openThreads.length} threads
              </span>
            </div>
            <p className="font-display text-[1.05rem] leading-snug text-ink">{s.crystal.narrative}</p>
          </button>
        ))}
        {sessions.length === 0 && <p className="text-sm text-ink-muted">No sessions touch this yet.</p>}
      </div>
    </div>
  );
}
