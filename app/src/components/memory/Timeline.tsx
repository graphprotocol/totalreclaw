import { clsx } from "clsx";
import type { SessionGroup } from "../../lib/vault/timeline";
import type { VaultItem } from "../../lib/types";
import { relativeDate, count } from "../../lib/format";

/** Timeline variants (Rail · Activity) over the decrypted session timeline.
 *  Ported from the Keeper prototype, on real SessionGroup data. */
const DAY = 86_400_000;

function membersOf(g: SessionGroup): VaultItem[] {
  return g.crystal ? [g.crystal, ...g.facts] : g.facts;
}
function isImported(g: SessionGroup): boolean {
  return membersOf(g).some((m) => m.claim.source === "external");
}
function startOfWeek(d: Date): Date {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - x.getDay()); // back to Sunday
  return x;
}
function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// ── Rail: horizontal date axis ───────────────────────────────────────
export function RailTimeline({
  groups,
  onOpen,
}: {
  groups: SessionGroup[];
  onOpen: (g: SessionGroup) => void;
}) {
  const sorted = [...groups].sort((a, b) => a.date.getTime() - b.date.getTime());
  return (
    <div className="mt-6 -mx-4 overflow-x-auto px-4 pb-3">
      <div className="relative flex min-w-max items-stretch gap-4 pt-5">
        <div className="pointer-events-none absolute inset-x-1 top-[13px] h-px bg-hairline" aria-hidden />
        {sorted.map((g, i) => (
          <button
            key={g.key}
            onClick={() => onOpen(g)}
            style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}
            className="animate-fade-up relative flex w-60 shrink-0 flex-col rounded-card bg-surface p-4 text-left shadow-soft transition duration-200 ease-keeper hover:-translate-y-0.5 hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
          >
            <span className="absolute -top-[6px] left-6 z-10 h-3 w-3 rounded-full bg-clay ring-4 ring-warm-white" aria-hidden />
            <time className="font-mono text-xs text-ink-muted">{relativeDate(g.date)}</time>
            {isImported(g) && (
              <span className="mt-1.5 inline-flex w-fit items-center gap-1 rounded-pill bg-clay-tint px-2 py-0.5 text-[0.65rem] font-semibold text-clay-deep">
                Imported
              </span>
            )}
            <p className="mt-1.5 font-display text-[0.95rem] leading-snug text-ink" style={{ textWrap: "pretty" }}>
              {g.headline}
            </p>
            <span className="mt-auto pt-3 font-mono text-[0.7rem] text-ink-muted">
              {count(g.facts.length, "fact")} · {count(g.openThreads, "thread")}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Activity: density heatmap + session list ─────────────────────────
export function ActivityTimeline({
  groups,
  onOpen,
}: {
  groups: SessionGroup[];
  onOpen: (g: SessionGroup) => void;
}) {
  const byDate = new Map<string, SessionGroup[]>();
  for (const g of groups) {
    const key = ymd(g.date);
    const arr = byDate.get(key);
    if (arr) arr.push(g);
    else byDate.set(key, [g]);
  }
  const times = groups.map((g) => g.date.getTime());
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

  const chronological = [...groups].sort((a, b) => b.date.getTime() - a.date.getTime());

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
                      ds.length > 0 &&
                        "cursor-pointer hover:ring-2 hover:ring-clay/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay",
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
        {chronological.map((g) => (
          <button
            key={g.key}
            onClick={() => onOpen(g)}
            className="flex w-full items-baseline gap-3 rounded-control bg-surface px-4 py-3 text-left shadow-soft transition hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
          >
            <time className="w-24 shrink-0 font-mono text-xs text-ink-muted">{relativeDate(g.date)}</time>
            <span className="font-display text-[0.95rem] leading-snug text-ink line-clamp-1">
              {g.headline}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
