import { useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";
import { SessionCard } from "./SessionCard";
import { KeeperEmpty, GhostGlimpse } from "./KeeperEmpty";
import { SEED_SESSIONS } from "./seed";
import { sourceShort, type Presentation } from "./presentation";

const HOW_IT_FILLS = [
  { n: "1", t: "Pair an agent", d: "Hermes, Claude, any MCP agent." },
  { n: "2", t: "Talk normally", d: "No saving, no tagging. Just chat." },
  { n: "3", t: "I keep what matters", d: "You stay in control of all of it." },
];

function MemoryEmpty() {
  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-24 pt-12">
        <KeeperEmpty
          icon={
            <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
              <path d="M12 2 9.5 9.5 2 12l7.5 2.5L12 22l2.5-7.5L22 12l-7.5-2.5Z" />
            </svg>
          }
          title="Your memory starts here"
          body={
            <>
              Nothing here yet — and that's right. As you talk with your agents, I'll quietly keep
              what matters and bring anything worth your eyes to{" "}
              <Link to="/proto/review" className="font-semibold text-clay-deep hover:underline">
                Review
              </Link>
              . You won't type memories in by hand.
            </>
          }
        >
          <div className="mt-6 flex justify-center">
            <Link
              to="/proto/pair-agent"
              className="rounded-control bg-clay px-5 py-2.5 font-sans text-sm font-semibold text-warm-white shadow-soft transition duration-150 ease-keeper hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
            >
              Pair an agent
            </Link>
          </div>
          <p className="mx-auto mt-3 max-w-sm text-xs leading-relaxed text-ink-muted">
            Already have memories in ChatGPT, Gemini, or Claude? Pair an agent, then ask it to import
            them — that's where the model that reads your old chats lives.{" "}
            <Link to="/proto/import" className="font-semibold text-clay-deep hover:underline">
              See how →
            </Link>
          </p>

          <div className="mt-7 grid gap-3 text-left sm:grid-cols-3">
            {HOW_IT_FILLS.map((s) => (
              <div key={s.n} className="rounded-control border border-hairline bg-warm-white p-3">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-clay-tint font-mono text-xs font-semibold text-clay-deep">
                  {s.n}
                </span>
                <p className="mt-2 text-sm font-semibold text-ink">{s.t}</p>
                <p className="mt-0.5 text-xs leading-relaxed text-ink-muted">{s.d}</p>
              </div>
            ))}
          </div>
        </KeeperEmpty>

        <GhostGlimpse />
      </main>
    </div>
  );
}

// The first thing the Keeper keeps — shown as a one-time moment, not a silent card.
const FIRST_MEMORY = {
  text: "You're happiest doing focused work in the morning, before meetings start.",
  badge: "preference",
};

function FirstMemory() {
  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-xl px-4 pb-24 pt-14">
        <div className="text-center">
          <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-clay-tint text-clay-deep">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
              <path d="M12 2 9.5 9.5 2 12l7.5 2.5L12 22l2.5-7.5L22 12l-7.5-2.5Z" />
            </svg>
          </div>
          <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">I kept my first memory</h1>
          <p className="mx-auto mt-2 max-w-sm text-sm leading-relaxed text-ink-muted">
            From your conversation just now. Did I get it right?
          </p>
        </div>

        <article className="animate-fade-up mt-6 rounded-card bg-surface p-5 shadow-soft">
          <span className="rounded-pill bg-type-preference px-2.5 py-0.5 text-xs font-semibold text-type-preference-ink">
            {FIRST_MEMORY.badge}
          </span>
          <p className="mt-2.5 font-display text-[1.2rem] leading-snug text-ink">{FIRST_MEMORY.text}</p>
          <p className="mt-2 text-xs text-ink-muted">from you · just now</p>
        </article>

        <div className="mt-5 flex flex-wrap justify-center gap-2">
          <Link
            to="/proto/timeline?warming"
            className="rounded-control bg-clay px-5 py-2.5 font-sans text-sm font-semibold text-warm-white shadow-soft transition duration-150 ease-keeper hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
          >
            Yes, that's right
          </Link>
          <Link
            to="/proto/timeline?warming"
            className="rounded-control border border-hairline bg-warm-white px-4 py-2.5 font-sans text-sm font-semibold text-ink transition hover:border-ink-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
          >
            Not quite — edit
          </Link>
          <Link
            to="/proto/timeline?empty"
            className="rounded-control px-4 py-2.5 font-sans text-sm font-semibold text-ink-muted transition hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
          >
            Forget it
          </Link>
        </div>

        <p className="mx-auto mt-7 max-w-sm text-center text-sm leading-relaxed text-ink-muted">
          From here I'll keep what matters quietly — you'll only hear from me when something needs you
          in{" "}
          <Link to="/proto/review" className="font-semibold text-clay-deep hover:underline">
            Review
          </Link>
          .
        </p>
      </main>
    </div>
  );
}

function WarmingTimeline() {
  const shown = SEED_SESSIONS.slice(0, 1);
  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-24 pt-8">
        <div className="mb-5">
          <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">Your memory</h1>
          <p className="mt-1.5 text-sm leading-relaxed text-ink-muted">
            Taking shape — a few memories so far. Keep talking, and it grows on its own.
          </p>
        </div>
        <div className="space-y-4">
          {shown.map((session, i) => (
            <SessionCard
              key={session.id}
              session={session}
              style={{ animationDelay: `${i * 60}ms` }}
              onEntityClick={() => {}}
              href={`/proto/session/${session.id}`}
            />
          ))}
          <div className="rounded-card border border-dashed border-hairline p-6 text-center">
            <p className="text-sm text-ink-muted">More will appear here as you talk — nothing to do.</p>
            <Link
              to="/proto/review"
              className="mt-1.5 inline-block text-sm font-semibold text-clay-deep transition hover:underline"
            >
              Anything that needs you shows up in Review →
            </Link>
          </div>
        </div>
      </main>
    </div>
  );
}

const SCOPES = [...new Set(SEED_SESSIONS.flatMap((s) => s.facts.map((f) => f.scope)))];
const TYPES = [...new Set(SEED_SESSIONS.flatMap((s) => s.facts.map((f) => f.type)))];
const SOURCES = [...new Set(SEED_SESSIONS.flatMap((s) => s.facts.map((f) => f.source)))];
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={clsx(
        "rounded-pill px-3 py-1 text-xs font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-1",
        active
          ? "bg-clay-tint text-clay-deep ring-1 ring-clay/40"
          : "border border-hairline bg-surface text-ink-muted hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

/** Session timeline. A/B: "By type" (taxonomy filters) vs "By source" (provenance filters). */
export function TimelineView() {
  const [params, setParams] = useSearchParams();
  const empty = params.has("empty"); // cold-start preview: ?empty
  const first = params.has("first"); // first-memory "aha" moment: ?first
  const warming = params.has("warming"); // warming-up (few memories): ?warming
  const view: Presentation = params.get("view") === "type" ? "type" : "source";
  const setView = (v: Presentation) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (v === "type") next.set("view", "type");
        else next.delete("view");
        return next;
      },
      { replace: true },
    );

  const [scope, setScope] = useState<string | null>(null);
  const [type, setType] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [openOnly, setOpenOnly] = useState(false);
  const [entity, setEntity] = useState<string | null>(null);

  const filtered = useMemo(
    () =>
      SEED_SESSIONS.filter((s) => {
        if (openOnly && s.crystal.openThreads.length === 0) return false;
        if (entity && !s.entities.includes(entity)) return false;
        if (scope && !s.facts.some((f) => f.scope === scope)) return false;
        if (view === "type" && type && !s.facts.some((f) => f.type === type)) return false;
        if (view === "source" && source && !s.facts.some((f) => f.source === source)) return false;
        return true;
      }),
    [scope, type, source, openOnly, entity, view],
  );

  const anyFilter = Boolean(scope || type || source || openOnly || entity);
  const clearAll = () => {
    setScope(null);
    setType(null);
    setSource(null);
    setOpenOnly(false);
    setEntity(null);
  };
  const hrefFor = (id: string) => `/proto/session/${id}${view === "type" ? "?view=type" : ""}`;

  if (empty) return <MemoryEmpty />;
  if (first) return <FirstMemory />;
  if (warming) return <WarmingTimeline />;

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-24 pt-8">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">Your memory</h1>
            <p className="mt-1.5 text-sm text-ink-muted">
              {filtered.length} of {SEED_SESSIONS.length} sessions
              {entity ? ` · about ${entity}` : ""}. Only you can read this.
            </p>
          </div>
          <div className="inline-flex rounded-pill p-1 ring-1 ring-hairline">
            {(["type", "source"] as Presentation[]).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                aria-pressed={view === v}
                className={clsx(
                  "rounded-pill px-3 py-1.5 text-xs font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-1",
                  view === v ? "bg-clay text-warm-white shadow-soft" : "text-ink-muted hover:text-ink",
                )}
              >
                {v === "type" ? "By type" : "By source"}
              </button>
            ))}
          </div>
        </div>

        <div className="mb-6 space-y-2.5">
          {entity && (
            <span className="inline-flex items-center gap-1.5 rounded-pill bg-clay px-3 py-1 text-xs font-semibold text-warm-white">
              {entity}
              <button
                type="button"
                onClick={() => setEntity(null)}
                aria-label={`Clear ${entity} filter`}
                className="text-warm-white/80 transition hover:text-warm-white"
              >
                ✕
              </button>
            </span>
          )}
          <div className="flex flex-wrap items-center gap-1.5">
            <Chip active={openOnly} onClick={() => setOpenOnly(!openOnly)}>
              Open threads
            </Chip>
            <span className="mx-1 h-4 w-px bg-hairline" aria-hidden />
            {SCOPES.map((sc) => (
              <Chip key={sc} active={scope === sc} onClick={() => setScope(scope === sc ? null : sc)}>
                {cap(sc)}
              </Chip>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {view === "type" ? (
              <>
                <span className="mr-0.5 text-xs font-semibold text-ink-muted">Type</span>
                {TYPES.map((t) => (
                  <Chip key={t} active={type === t} onClick={() => setType(type === t ? null : t)}>
                    {t}
                  </Chip>
                ))}
              </>
            ) : (
              <>
                <span className="mr-0.5 text-xs font-semibold text-ink-muted">Source</span>
                {SOURCES.map((sc) => (
                  <Chip
                    key={sc}
                    active={source === sc}
                    onClick={() => setSource(source === sc ? null : sc)}
                  >
                    {sourceShort(sc)}
                  </Chip>
                ))}
              </>
            )}
            {anyFilter && (
              <button
                type="button"
                onClick={clearAll}
                className="ml-1 rounded-pill px-2.5 py-1 text-xs font-semibold text-clay-deep transition hover:bg-clay-tint"
              >
                Clear all
              </button>
            )}
          </div>
        </div>

        <div className="space-y-4">
          {filtered.map((session, i) => (
            <SessionCard
              key={session.id}
              session={session}
              style={{ animationDelay: `${i * 60}ms` }}
              onEntityClick={setEntity}
              href={hrefFor(session.id)}
            />
          ))}
          {filtered.length === 0 && (
            <div className="rounded-card bg-surface p-8 text-center shadow-soft">
              <p className="text-sm text-ink-muted">No sessions match these filters.</p>
              <button
                type="button"
                onClick={clearAll}
                className="mt-2 text-sm font-semibold text-clay-deep hover:underline"
              >
                Clear all filters
              </button>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
