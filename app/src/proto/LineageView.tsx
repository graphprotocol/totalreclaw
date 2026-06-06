import { Link, useParams } from "react-router-dom";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";
import { LINEAGE_THREADS, EDGE_LABEL, type LineageNode } from "./lineage-data";

const EDGE_TONE: Record<string, string> = {
  contradicts: "text-clay-deep",
  supersedes: "text-ink-muted",
  "derived-from": "text-ink-muted",
};

function EdgeChip({ type }: { type: NonNullable<LineageNode["edgeFromPrev"]> }) {
  return (
    <span className={clsx("inline-flex items-center gap-1 text-xs font-semibold", EDGE_TONE[type])}>
      {type === "contradicts" ? (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M18 6 6 18M6 6l12 12" />
        </svg>
      ) : (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 5v14M19 12l-7 7-7-7" />
        </svg>
      )}
      {EDGE_LABEL[type]}
    </span>
  );
}

function Node({ node, first, last }: { node: LineageNode; first: boolean; last: boolean }) {
  const railTone =
    node.edgeFromPrev === "contradicts" ? "bg-clay/40" : "bg-hairline";
  const dotTone =
    node.state === "current"
      ? "bg-clay"
      : node.state === "rival"
        ? "bg-warm-white ring-2 ring-clay"
        : "bg-hairline";

  return (
    <li className="grid grid-cols-[1.5rem_1fr] gap-3">
      {/* rail */}
      <div className="relative flex justify-center">
        {!first && <span className={clsx("absolute left-1/2 top-0 h-6 w-px -translate-x-1/2", railTone)} />}
        {!last && <span className="absolute bottom-0 left-1/2 top-6 w-px -translate-x-1/2 bg-hairline" />}
        <span className={clsx("absolute top-[1.35rem] h-2.5 w-2.5 rounded-full", dotTone)} aria-hidden />
      </div>

      {/* content */}
      <div className="pb-5">
        {node.edgeFromPrev && (
          <div className="mb-1.5">
            <EdgeChip type={node.edgeFromPrev} />
          </div>
        )}
        <div
          className={clsx(
            "rounded-card p-4 shadow-soft transition",
            node.state === "rival"
              ? "border border-dashed border-clay/40 bg-clay-tint/40"
              : node.state === "past"
                ? "bg-surface opacity-70"
                : "bg-surface ring-1 ring-clay/15",
          )}
        >
          <p className="font-display text-[1.08rem] leading-snug text-ink">{node.text}</p>
          <p className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs text-ink-muted">
            {node.pinned && (
              <span className="inline-flex items-center gap-1 font-semibold text-clay-deep">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
                  <path d="M12 17v5M9 10.76V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v6.76a2 2 0 0 0 .5 1.32l1.7 1.92A1 1 0 0 1 17.5 16h-11a1 1 0 0 1-.7-1.99l1.7-1.93A2 2 0 0 0 9 10.76Z" />
                </svg>
                pinned ·
              </span>
            )}
            {node.source === "user" ? "from you" : node.source === "assistant" ? "your agent inferred this" : node.source} · {node.age}
          </p>
        </div>
      </div>
    </li>
  );
}

export function LineageView() {
  const { id } = useParams();
  const thread = (id && LINEAGE_THREADS[id]) || LINEAGE_THREADS["where-pedro-works"];

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-28 pt-8">
        <Link to="/proto/review" className="text-sm font-semibold text-ink-muted transition hover:text-ink">
          ← Back to review
        </Link>

        <header className="mb-7 mt-3">
          <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-clay-deep">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
              <path d="M12 2 9.5 9.5 2 12l7.5 2.5L12 22l2.5-7.5L22 12l-7.5-2.5Z" />
            </svg>
            Lineage
          </p>
          <h1 className="mt-1.5 text-balance font-display text-[2rem] leading-tight text-ink">{thread.title}</h1>
          <p className="mt-1.5 text-sm leading-relaxed text-ink-muted">{thread.question}</p>
        </header>

        <ol>
          {thread.nodes.map((n, i) => (
            <Node key={n.id} node={n} first={i === 0} last={i === thread.nodes.length - 1} />
          ))}
        </ol>

        {thread.conflict && (
          <div className="ml-[2.25rem] animate-fade-up rounded-card border border-clay/30 bg-clay-tint/50 p-4">
            <p className="flex items-center gap-1.5 font-display text-[1.02rem] text-ink">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#A54B2E" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M12 9v4M12 17h.01M3.6 18l7-13a1.6 1.6 0 0 1 2.8 0l7 13A1.6 1.6 0 0 1 21 20H4a1.6 1.6 0 0 1-1.4-2Z" />
              </svg>
              These two can't both be true.
            </p>
            <p className="mt-1 text-sm text-ink-muted">
              The newer note contradicts a memory you pinned. Pinned memories don't change on their own, so I left this for you.
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button type="button" className="rounded-pill bg-clay px-3.5 py-1.5 text-sm font-semibold text-warm-white shadow-soft transition hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2">
                Keep the pinned one
              </button>
              <button type="button" className="rounded-pill border border-hairline bg-warm-white px-3.5 py-1.5 text-sm font-semibold text-ink transition hover:border-ink-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2">
                Unpin &amp; take the newer
              </button>
              <button type="button" className="rounded-pill border border-hairline bg-warm-white px-3.5 py-1.5 text-sm font-semibold text-ink transition hover:border-ink-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2">
                Keep both
              </button>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
