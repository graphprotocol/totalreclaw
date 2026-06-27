import { useState, useEffect, useCallback, type ReactNode } from "react";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";
import { SEED_SESSIONS, relativeDate, type SeedSession } from "./seed";
import { count } from "./format";
import { sourceShort } from "./presentation";
import { TypeBadge } from "../components/TypeBadge";

/**
 * Memory redesign A/B chassis (seed data). Mode switcher (List · Timeline · Graph)
 * + a shared side-panel drawer. List mode is fully built here; Timeline + Graph
 * show their sub-toggles with placeholders until their variants land.
 * See app/src/proto/MEMORY-REDESIGN.md.
 */
type Mode = "list" | "timeline" | "graph";
type TimelineVariant = "rail" | "activity";
type GraphVariant = "entities" | "topics";

// ── Segmented control ────────────────────────────────────────────────
function Segmented<T extends string>({
  value,
  onChange,
  options,
  size = "md",
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
  size?: "md" | "sm";
}) {
  return (
    <div
      role="tablist"
      className={clsx(
        "inline-flex items-center gap-0.5 rounded-pill border border-hairline bg-surface p-0.5",
        size === "sm" ? "text-xs" : "text-sm",
      )}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(o.value)}
            className={clsx(
              "rounded-pill font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-1",
              size === "sm" ? "px-3 py-1" : "px-4 py-1.5",
              active ? "bg-clay text-warm-white shadow-soft" : "text-ink-muted hover:text-ink",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ── Gallery card (panel-aware) ───────────────────────────────────────
function GalleryCard({ session, onOpen }: { session: SeedSession; onOpen: () => void }) {
  const c = session.crystal;
  const outcomes = c.keyOutcomes.slice(0, 2);
  return (
    <button
      onClick={onOpen}
      className="animate-fade-up flex h-full flex-col rounded-card bg-surface p-5 text-left shadow-soft transition duration-200 ease-keeper hover:-translate-y-0.5 hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
    >
      <div className="mb-2 flex items-center gap-2">
        <time className="font-mono text-xs text-ink-muted">{relativeDate(session.date)}</time>
        {session.importSource && (
          <span className="inline-flex items-center gap-1 rounded-pill bg-clay-tint px-2 py-0.5 text-[0.65rem] font-semibold text-clay-deep">
            Imported · {session.importSource}
          </span>
        )}
      </div>
      <p className="font-display text-[1.2rem] leading-snug text-ink" style={{ textWrap: "pretty" }}>
        {c.narrative}
      </p>
      {outcomes.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {outcomes.map((o) => (
            <li key={o} className="flex gap-2 text-sm leading-snug text-ink-muted">
              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-clay" aria-hidden />
              <span>{o}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-auto pt-4">
        <span className="rounded-pill bg-warm-white px-2.5 py-1 font-mono text-xs text-ink-muted ring-1 ring-hairline">
          {count(session.facts.length, "fact")} · {count(session.entities.length, "entity", "entities")} ·{" "}
          {count(c.openThreads.length, "thread")}
        </span>
      </div>
    </button>
  );
}

// ── Side panel (Notion-style drawer) ─────────────────────────────────
function PanelSection({ title, items }: { title: string; items: string[] }) {
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

function SidePanel({ session, onClose }: { session: SeedSession | null; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const open = !!session;
  return (
    <>
      {/* backdrop */}
      <div
        onClick={onClose}
        aria-hidden
        className={clsx(
          "fixed inset-0 z-40 bg-ink/20 transition-opacity duration-200 ease-keeper motion-reduce:transition-none",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
      />
      {/* drawer */}
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={session ? "Session detail" : undefined}
        className={clsx(
          "fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col bg-warm-white shadow-overlay transition-transform duration-200 ease-keeper motion-reduce:transition-none",
          open ? "translate-x-0" : "translate-x-full",
        )}
      >
        {session && (
          <>
            <div className="flex items-center justify-between border-b border-hairline px-5 py-3">
              <time className="font-mono text-xs text-ink-muted">{relativeDate(session.date)}</time>
              <button
                onClick={onClose}
                aria-label="Close"
                className="rounded-pill p-1.5 text-ink-muted transition hover:bg-clay-tint hover:text-clay-deep focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-5">
              {session.importSource && (
                <span className="mb-3 inline-flex items-center gap-1 rounded-pill bg-clay-tint px-2.5 py-0.5 text-xs font-semibold text-clay-deep">
                  Imported · {session.importSource}
                </span>
              )}
              <h2 className="font-display text-xl leading-snug text-ink" style={{ textWrap: "pretty" }}>
                {session.crystal.narrative}
              </h2>
              <PanelSection title="Key outcomes" items={session.crystal.keyOutcomes} />
              <PanelSection title="Open threads" items={session.crystal.openThreads} />
              <PanelSection title="Lessons" items={session.crystal.lessons ?? []} />

              {session.entities.length > 0 && (
                <div className="mt-5 flex flex-wrap gap-1.5">
                  {session.entities.map((e) => (
                    <span key={e} className="rounded-pill bg-warm-white px-2.5 py-1 font-mono text-xs text-ink-muted ring-1 ring-hairline">
                      {e}
                    </span>
                  ))}
                </div>
              )}

              <h3 className="mt-7 text-xs font-semibold uppercase tracking-wide text-ink-muted">
                Memories from this session
              </h3>
              <div className="mt-3 space-y-3">
                {session.facts.map((f) => (
                  <article
                    key={f.id}
                    className={clsx("rounded-card p-4 shadow-soft", f.pinned ? "bg-clay-tint" : "bg-surface")}
                  >
                    <p className="font-display text-base leading-snug text-ink" style={{ textWrap: "pretty" }}>
                      {f.text}
                    </p>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <TypeBadge type={f.type} />
                      <span className="text-xs text-ink-muted">
                        {sourceShort(f.source)}
                        {f.scope ? ` · ${f.scope}` : ""}
                        {f.pinned ? " · pinned" : ""}
                      </span>
                    </div>
                  </article>
                ))}
              </div>
            </div>
          </>
        )}
      </aside>
    </>
  );
}

// ── Placeholder for not-yet-built modes ──────────────────────────────
function Placeholder({ children }: { children: ReactNode }) {
  return (
    <div className="mt-6 rounded-card border border-dashed border-hairline bg-surface/60 p-10 text-center">
      <p className="text-sm text-ink-muted">{children}</p>
    </div>
  );
}

// ── Timeline: Rail (horizontal date axis) ───────────────────────────
function parseDate(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function RailTimeline({
  sessions,
  onOpen,
}: {
  sessions: SeedSession[];
  onOpen: (s: SeedSession) => void;
}) {
  const sorted = [...sessions].sort((a, b) => a.date.localeCompare(b.date));
  return (
    <div className="mt-6 -mx-4 overflow-x-auto px-4 pb-3">
      <div className="relative flex min-w-max items-stretch gap-4 pt-5">
        <div className="pointer-events-none absolute inset-x-1 top-[13px] h-px bg-hairline" aria-hidden />
        {sorted.map((s, i) => (
          <button
            key={s.id}
            onClick={() => onOpen(s)}
            style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}
            className="animate-fade-up relative flex w-60 shrink-0 flex-col rounded-card bg-surface p-4 text-left shadow-soft transition duration-200 ease-keeper hover:-translate-y-0.5 hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
          >
            <span className="absolute -top-[6px] left-6 z-10 h-3 w-3 rounded-full bg-clay ring-4 ring-warm-white" aria-hidden />
            <time className="font-mono text-xs text-ink-muted">{relativeDate(s.date)}</time>
            {s.importSource && (
              <span className="mt-1.5 inline-flex w-fit items-center gap-1 rounded-pill bg-clay-tint px-2 py-0.5 text-[0.65rem] font-semibold text-clay-deep">
                Imported · {s.importSource}
              </span>
            )}
            <p className="mt-1.5 font-display text-[0.95rem] leading-snug text-ink" style={{ textWrap: "pretty" }}>
              {s.crystal.narrative}
            </p>
            <span className="mt-auto pt-3 font-mono text-[0.7rem] text-ink-muted">
              {count(s.facts.length, "fact")} · {count(s.crystal.openThreads.length, "thread")}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Timeline: Activity (heatmap) ─────────────────────────────────────
const DAY = 86_400_000;
function startOfWeek(d: Date): Date {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - x.getDay()); // back to Sunday
  return x;
}
function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function ActivityTimeline({
  sessions,
  onOpen,
}: {
  sessions: SeedSession[];
  onOpen: (s: SeedSession) => void;
}) {
  const byDate = new Map<string, SeedSession[]>();
  for (const s of sessions) {
    const arr = byDate.get(s.date);
    if (arr) arr.push(s);
    else byDate.set(s.date, [s]);
  }
  const times = sessions.map((s) => parseDate(s.date).getTime());
  const min = startOfWeek(new Date(Math.min(...times)));
  const maxEnd = new Date(Math.max(...times) + 6 * DAY);
  const weeks: Date[][] = [];
  for (let t = min.getTime(); t <= maxEnd.getTime(); ) {
    const week: Date[] = [];
    for (let d = 0; d < 7; d++) {
      week.push(new Date(t));
      t += DAY;
    }
    weeks.push(week);
  }
  const tone = (n: number) =>
    n === 0 ? "bg-hairline/50" : n === 1 ? "bg-clay-tint" : n === 2 ? "bg-clay/60" : "bg-clay";

  const chronological = [...sessions].sort((a, b) => b.date.localeCompare(a.date));

  return (
    <div className="mt-6">
      <div className="-mx-4 overflow-x-auto px-4 pb-2">
        <div className="flex min-w-max gap-1">
          {weeks.map((week, wi) => (
            <div key={wi} className="flex flex-col gap-1">
              {week.map((day) => {
                const key = ymd(day);
                const ds = byDate.get(key) ?? [];
                return (
                  <button
                    key={key}
                    onClick={() => ds[0] && onOpen(ds[0])}
                    disabled={ds.length === 0}
                    title={`${key}${ds.length ? ` · ${count(ds.length, "session")}` : ""}`}
                    aria-label={`${key}${ds.length ? `, ${count(ds.length, "session")}` : ", no sessions"}`}
                    className={clsx(
                      "h-3.5 w-3.5 rounded-sm transition",
                      tone(ds.length),
                      ds.length > 0 && "cursor-pointer hover:ring-2 hover:ring-clay/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay",
                    )}
                  />
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <div className="mt-3 flex items-center gap-2 text-[0.7rem] text-ink-muted">
        <span>Less</span>
        <span className="h-3 w-3 rounded-sm bg-hairline/50" />
        <span className="h-3 w-3 rounded-sm bg-clay-tint" />
        <span className="h-3 w-3 rounded-sm bg-clay/60" />
        <span className="h-3 w-3 rounded-sm bg-clay" />
        <span>More</span>
      </div>

      <div className="mt-6 space-y-2">
        {chronological.map((s) => (
          <button
            key={s.id}
            onClick={() => onOpen(s)}
            className="flex w-full items-baseline gap-3 rounded-control bg-surface px-4 py-3 text-left shadow-soft transition hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
          >
            <time className="w-24 shrink-0 font-mono text-xs text-ink-muted">{relativeDate(s.date)}</time>
            <span className="font-display text-[0.95rem] leading-snug text-ink line-clamp-1">
              {s.crystal.narrative}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────
export function MemoryRedesign() {
  const [mode, setMode] = useState<Mode>("list");
  const [timelineVariant, setTimelineVariant] = useState<TimelineVariant>("rail");
  const [graphVariant, setGraphVariant] = useState<GraphVariant>("entities");
  const [selected, setSelected] = useState<SeedSession | null>(null);
  const close = useCallback(() => setSelected(null), []);

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-24 pt-8">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-display text-[2rem] leading-tight text-ink">Your memory</h1>
          <Segmented
            value={mode}
            onChange={setMode}
            options={[
              { value: "list", label: "List" },
              { value: "timeline", label: "Timeline" },
              { value: "graph", label: "Graph" },
            ]}
          />
        </div>
        <p className="mb-6 text-sm text-ink-muted">
          {SEED_SESSIONS.length} sessions. Only you can read this.
        </p>

        {mode === "list" && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {SEED_SESSIONS.map((s) => (
              <GalleryCard key={s.id} session={s} onOpen={() => setSelected(s)} />
            ))}
          </div>
        )}

        {mode === "timeline" && (
          <div>
            <Segmented
              size="sm"
              value={timelineVariant}
              onChange={setTimelineVariant}
              options={[
                { value: "rail", label: "Rail" },
                { value: "activity", label: "Activity" },
              ]}
            />
            {timelineVariant === "rail" ? (
              <RailTimeline sessions={SEED_SESSIONS} onOpen={setSelected} />
            ) : (
              <ActivityTimeline sessions={SEED_SESSIONS} onOpen={setSelected} />
            )}
          </div>
        )}

        {mode === "graph" && (
          <div>
            <Segmented
              size="sm"
              value={graphVariant}
              onChange={setGraphVariant}
              options={[
                { value: "entities", label: "Entities" },
                { value: "topics", label: "Topics" },
              ]}
            />
            <Placeholder>
              {graphVariant === "entities" ? "Force-directed entity graph" : "Tree of topics"} — building next.
            </Placeholder>
          </div>
        )}
      </main>

      <SidePanel session={selected} onClose={close} />
    </div>
  );
}
