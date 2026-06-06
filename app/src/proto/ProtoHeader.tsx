import { Link, useLocation } from "react-router-dom";
import { clsx } from "clsx";

const TABS = [
  { to: "/proto/timeline", label: "Memory" },
  { to: "/proto/review", label: "Review" },
  { to: "/proto/lineage", label: "Lineage" },
];

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
          <Link
            to="/proto/search"
            aria-label="Search your memory"
            className={clsx(
              "rounded-pill p-2 transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2",
              pathname === "/proto/search" ? "bg-clay-tint text-clay-deep" : "text-ink-muted hover:text-ink",
            )}
          >
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <circle cx="11" cy="11" r="7" />
              <path d="m21 21-4.3-4.3" />
            </svg>
          </Link>
          {TABS.map((t) => {
            const active = pathname === t.to;
            return (
              <Link
                key={t.to}
                to={t.to}
                className={clsx(
                  "rounded-pill px-3 py-1.5 text-sm font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2",
                  active
                    ? "bg-clay-tint text-clay-deep"
                    : "text-ink-muted hover:text-ink",
                )}
                aria-current={active ? "page" : undefined}
              >
                {t.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
