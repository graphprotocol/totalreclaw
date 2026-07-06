import { Link, useLocation, useNavigate } from "react-router-dom";
import { useCrypto } from "../contexts/CryptoContext";

const NAV = [
  { to: "/memory", label: "Memory" },
  { to: "/review", label: "Review" },
];

/** Keeper top bar: wordmark + Memory · Review + lock. (Review/Settings land in
 *  later tasks; their links resolve once those pages exist.) */
export function AppHeader() {
  const { lock } = useCrypto();
  const navigate = useNavigate();
  const { pathname } = useLocation();

  const onLock = () => {
    lock();
    navigate("/unlock", { replace: true });
  };

  return (
    <header className="sticky top-0 z-10 border-b border-hairline bg-warm-white/90 backdrop-blur">
      <div className="mx-auto flex max-w-2xl items-center justify-between px-5 py-3">
        <Link to="/memory" className="font-display text-lg font-semibold text-ink">
          TotalReclaw
        </Link>
        <nav className="flex items-center gap-1">
          {NAV.map((n) => {
            const active = pathname === n.to || pathname.startsWith(n.to + "/");
            return (
              <Link
                key={n.to}
                to={n.to}
                className={`rounded-pill px-3 py-1.5 text-sm font-semibold transition ${
                  active ? "bg-clay-tint text-clay-deep" : "text-ink-muted hover:text-ink"
                }`}
              >
                {n.label}
              </Link>
            );
          })}
          <Link
            to="/settings"
            className={`rounded-pill px-2.5 py-1.5 text-ink-muted transition hover:text-ink ${
              pathname.startsWith("/settings") ? "text-ink" : ""
            }`}
            title="Settings"
            aria-label="Settings"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </Link>
          <button
            onClick={onLock}
            className="ml-1 rounded-pill px-3 py-1.5 text-sm font-semibold text-ink-muted hover:text-ink"
            title="Lock vault"
          >
            Lock
          </button>
        </nav>
      </div>
    </header>
  );
}
