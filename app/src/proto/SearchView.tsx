import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ProtoHeader } from "./ProtoHeader";
import { SEED_SESSIONS, relativeDate } from "./seed";
import { sourceShort, typeBucket } from "./presentation";
import { count } from "./format";

// Flatten seed facts into a searchable index with session context. Lexical only —
// this is SPA-side retrieval (decrypt + match locally, no LLM). A synthesized
// written answer is an agent capability (see the "ask your agent" affordance).
const INDEX = SEED_SESSIONS.flatMap((s) =>
  s.facts.map((f) => ({
    ...f,
    sessionId: s.id,
    date: s.date,
    haystack: `${f.text} ${f.scope} ${f.type} ${f.source}`.toLowerCase(),
  })),
);

const SUGGESTIONS = ["running", "Lisbon", "work", "morning"];

function Highlight({ text, q }: { text: string; q: string }) {
  if (!q) return <>{text}</>;
  const ql = q.toLowerCase();
  const out: React.ReactNode[] = [];
  let rest = text;
  let k = 0;
  while (true) {
    const idx = rest.toLowerCase().indexOf(ql);
    if (idx < 0) {
      out.push(rest);
      break;
    }
    if (idx > 0) out.push(rest.slice(0, idx));
    out.push(
      <mark key={k++} className="rounded bg-clay-tint px-0.5 text-clay-deep">
        {rest.slice(idx, idx + q.length)}
      </mark>,
    );
    rest = rest.slice(idx + q.length);
  }
  return <>{out}</>;
}

export function SearchView() {
  const [q, setQ] = useState("");
  const [copied, setCopied] = useState(false);
  const query = q.trim();

  const results = useMemo(() => {
    if (!query) return [];
    const ql = query.toLowerCase();
    return INDEX.filter((f) => f.haystack.includes(ql))
      .map((f) => ({ f, score: f.text.toLowerCase().includes(ql) ? 2 : 1 }))
      .sort((a, b) => b.score - a.score || b.f.date.localeCompare(a.f.date))
      .map((r) => r.f);
  }, [query]);

  const copyAsk = () => {
    navigator.clipboard?.writeText(query).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-28 pt-8">
        <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">Find anything</h1>
        <p className="mt-1.5 text-sm leading-relaxed text-ink-muted">
          Search everything you've told your agents. It all stays on this device — only you can read it.
        </p>

        {/* search input */}
        <div className="relative mt-5">
          <svg
            className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-ink-muted"
            width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Try “running”, “where do I live”, “work”…"
            className="w-full rounded-control border border-hairline bg-surface py-3 pl-11 pr-4 font-sans text-[0.95rem] text-ink shadow-soft transition placeholder:text-ink-muted/60 focus:border-clay focus:outline-none focus:ring-2 focus:ring-clay/30"
          />
        </div>

        {/* empty query → suggestions */}
        {!query && (
          <div className="mt-5">
            <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Try</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setQ(s)}
                  className="rounded-pill border border-hairline bg-surface px-3 py-1 text-sm font-semibold text-ink-muted transition hover:border-ink-muted/40 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-1"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* results */}
        {query && (
          <div className="mt-5">
            <p className="mb-3 text-sm text-ink-muted">
              {results.length > 0
                ? `${count(results.length, "memory", "memories")} match “${query}”.`
                : `Nothing matches “${query}”.`}
            </p>

            <div className="space-y-2.5">
              {results.map((f) => {
                const bucket = typeBucket(f.type);
                return (
                  <Link
                    key={f.id}
                    to={`/proto/session/${f.sessionId}`}
                    className="block rounded-card bg-surface p-4 shadow-soft transition duration-150 ease-keeper hover:-translate-y-px hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
                  >
                    <p className="font-display text-[1.05rem] leading-snug text-ink">
                      <Highlight text={f.text} q={query} />
                    </p>
                    <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-muted">
                      {bucket && (
                        <span className="rounded-pill bg-clay-tint px-2 py-0.5 font-semibold text-clay-deep">
                          {bucket.label}
                        </span>
                      )}
                      <span>{sourceShort(f.source)}</span>
                      <span aria-hidden>·</span>
                      <span>{f.scope}</span>
                      <span aria-hidden>·</span>
                      <span>{relativeDate(f.date)}</span>
                    </div>
                  </Link>
                );
              })}
            </div>

            {/* ask-your-agent affordance (synthesis = agent, not SPA) */}
            <div className="mt-5 rounded-card border border-hairline bg-warm-white p-4">
              <p className="text-sm font-semibold text-ink">
                {results.length > 0 ? "Want it written up?" : "Looking for something I didn't catch?"}
              </p>
              <p className="mt-1 text-sm text-ink-muted">
                Ask your paired agent for a written answer — the model that reasons over your memories
                lives there, not here.
              </p>
              <div className="mt-2.5 flex items-stretch gap-2">
                <code className="flex-1 truncate rounded-control border border-hairline bg-surface px-3.5 py-2.5 font-mono text-sm text-ink">
                  {query}
                </code>
                <button
                  type="button"
                  onClick={copyAsk}
                  className="shrink-0 rounded-control bg-clay px-3.5 text-sm font-semibold text-warm-white shadow-soft transition duration-150 ease-keeper hover:bg-clay-deep focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
                >
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
