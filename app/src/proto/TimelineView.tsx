import { useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";
import { SessionCard } from "./SessionCard";
import { SEED_SESSIONS } from "./seed";
import { sourceShort, type Presentation } from "./presentation";

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
