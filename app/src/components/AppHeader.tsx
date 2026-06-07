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
