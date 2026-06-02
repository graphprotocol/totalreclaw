import { Link, useLocation } from "react-router-dom";
import { clsx } from "clsx";

const TABS = [
  { to: "/proto/timeline", label: "Timeline" },
  { to: "/proto/kg", label: "Mind-map" },
  { to: "/proto/explore", label: "Explore" },
];

/** Sticky wordmark + tab switch. Solid warm-white, hairline base — no glass. */
export function ProtoHeader() {
  const { pathname } = useLocation();
  return (
    <header className="sticky top-0 z-30 border-b border-hairline bg-warm-white">
      <div className="mx-auto flex w-full max-w-2xl items-center justify-between px-4 py-3">
        <span className="font-display text-lg font-medium tracking-tight text-ink">
          TotalReclaw
        </span>
        <nav className="flex items-center gap-1">
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
