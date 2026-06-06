import { Link, useLocation } from "react-router-dom";
import { clsx } from "clsx";
import { REVIEW_ITEMS } from "./review-data";

const TABS = [
  { to: "/proto/timeline", label: "Memory" },
  { to: "/proto/review", label: "Review" },
];

// "Needs you" count → the always-visible signal that Review has something for you.
const NEEDS_YOU = REVIEW_ITEMS.filter((i) => i.kind === "conflict" || i.kind === "stale").length;

/** Sticky wordmark + tab switch. Solid warm-white, hairline base — no glass. */
export function ProtoHeader() {
  const { pathname } = useLocation();
  return (
    <header className="sticky top-0 z-30 border-b border-hairline bg-warm-white">
      <div className="mx-auto flex w-full max-w-2xl items-center justify-between px-4 py-3">
        <Link
          to="/proto"
          className="font-display text-lg font-medium tracking-tight text-ink transition hover:text-clay-deep"
        >
          TotalReclaw
        </Link>
        <nav className="flex items-center gap-1">
          {TABS.map((t) => {
            const active = pathname === t.to;
            return (
              <Link
                key={t.to}
                to={t.to}
                className={clsx(
                  "inline-flex items-center rounded-pill px-3 py-1.5 text-sm font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2",
                  active
                    ? "bg-clay-tint text-clay-deep"
                    : "text-ink-muted hover:text-ink",
                )}
                aria-current={active ? "page" : undefined}
              >
                {t.label}
                {t.to === "/proto/review" && NEEDS_YOU > 0 && (
                  <span
                    className="ml-1.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-clay px-1 text-[0.65rem] font-bold text-warm-white"
                    aria-label={`${NEEDS_YOU} need your attention`}
                  >
                    {NEEDS_YOU}
                  </span>
                )}
              </Link>
            );
          })}
          <Link
            to="/proto/settings"
            aria-label="Settings"
            className={clsx(
              "ml-1 rounded-pill p-2 transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2",
              pathname === "/proto/settings" ? "bg-clay-tint text-clay-deep" : "text-ink-muted hover:text-ink",
            )}
          >
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
            </svg>
          </Link>
        </nav>
      </div>
    </header>
  );
}
