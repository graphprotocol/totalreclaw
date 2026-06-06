import type { ReactNode } from "react";

/** Warm, centered empty-state shell in the Keeper's voice. An empty vault is an
 *  activation surface, not a dead end — keep it inviting and point at the one
 *  action that fills it. */
export function KeeperEmpty({
  icon,
  title,
  body,
  children,
}: {
  icon: ReactNode;
  title: string;
  body: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="animate-fade-up rounded-card bg-surface px-6 py-12 text-center shadow-soft sm:px-10">
      <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-clay-tint text-clay-deep">
        {icon}
      </div>
      <h2 className="text-balance font-display text-2xl leading-tight text-ink">{title}</h2>
      <p className="mx-auto mt-2.5 max-w-sm text-pretty text-sm leading-relaxed text-ink-muted">{body}</p>
      {children}
    </div>
  );
}

/** A non-interactive ghost of a memory card — shows the user what *will* fill in
 *  without faking real data. Faded + blurred so it reads as a preview, not content. */
export function GhostGlimpse() {
  return (
    <div className="mt-8" aria-hidden>
      <p className="mb-2.5 text-center text-xs font-semibold uppercase tracking-wide text-ink-muted/70">
        A glimpse of what fills in
      </p>
      <div
        className="pointer-events-none select-none space-y-3 opacity-55 blur-[0.6px] [mask-image:linear-gradient(to_bottom,black,transparent)]"
      >
        {[
          { date: "Today", w: "82%", chips: ["personal", "you"] },
          { date: "Yesterday", w: "64%", chips: ["work", "rule"] },
        ].map((g, i) => (
          <div key={i} className="rounded-card bg-surface p-4 shadow-soft">
            <div className="mb-3 flex items-center gap-2">
              <span className="h-2 w-2 rounded-full bg-clay/50" />
              <span className="text-xs font-semibold text-ink-muted">{g.date}</span>
            </div>
            <div className="h-3 rounded-pill bg-hairline" style={{ width: g.w }} />
            <div className="mt-2 h-3 w-2/5 rounded-pill bg-hairline" />
            <div className="mt-3 flex gap-1.5">
              {g.chips.map((c) => (
                <span key={c} className="rounded-pill border border-hairline px-2 py-0.5 text-[0.7rem] text-ink-muted">
                  {c}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
