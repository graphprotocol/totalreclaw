import { Link } from "react-router-dom";
import { ProtoHeader } from "./ProtoHeader";

const AGENTS = ["Hermes", "Claude Desktop", "Cursor", "Any MCP agent"];

/** Faux QR — deterministic pattern (no randomness, stable for screenshots) with
 *  three finder squares. Purely decorative; this screen is non-functional. */
function FauxQR() {
  const N = 13;
  const isFinder = (i: number, j: number) => {
    const inBox = (bi: number, bj: number) =>
      i >= bi && i < bi + 3 && j >= bj && j < bj + 3;
    return inBox(0, 0) || inBox(0, N - 3) || inBox(N - 3, 0);
  };
  const cells = [];
  for (let i = 0; i < N; i++) {
    for (let j = 0; j < N; j++) {
      const on = isFinder(i, j) || (i * 7 + j * 13 + i * j) % 5 < 2;
      cells.push(
        <rect
          key={`${i}-${j}`}
          x={j * 8 + 1}
          y={i * 8 + 1}
          width={6}
          height={6}
          rx={1.4}
          fill={on ? "#2B2824" : "transparent"}
        />,
      );
    }
  }
  return (
    <svg viewBox={`0 0 ${N * 8 + 2} ${N * 8 + 2}`} width="148" height="148" role="img" aria-label="Pairing QR code (placeholder)">
      {cells}
    </svg>
  );
}

/** Pair-an-agent — NON-FUNCTIONAL visual stub. The destination for the cold-start
 *  on-ramp; it is NOT the real pairing/auth flow (that's PRD-01, deferred). */
export function ProtoPairAgent() {
  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <div className="flex min-h-[calc(100vh-57px)] items-center justify-center px-4 py-10">
        <div className="animate-page-in w-full max-w-md">
          <div className="mb-6 text-center">
            <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">Pair an agent</h1>
            <p className="mx-auto mt-2 max-w-sm text-sm leading-relaxed text-ink-muted">
              Connect Hermes, Claude, or any MCP agent. It gets a scoped key to read and add memories —
              never your recovery phrase. Everything stays end-to-end encrypted.
            </p>
          </div>

          <div className="rounded-card bg-surface p-6 text-center shadow-soft">
            <div className="mx-auto inline-flex rounded-control border border-hairline bg-warm-white p-3">
              <FauxQR />
            </div>

            <p className="mt-4 text-xs font-semibold uppercase tracking-wide text-ink-muted">
              Or enter this code in your agent
            </p>
            <p className="mt-1.5 font-mono text-3xl font-semibold tracking-[0.2em] text-ink">
              482&nbsp;915
            </p>

            <p className="mt-4 inline-flex items-center gap-2 text-sm text-ink-muted">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-clay/60" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-clay" />
              </span>
              Waiting for your agent…
            </p>

            <div className="mt-5 flex flex-wrap justify-center gap-1.5">
              {AGENTS.map((a) => (
                <span key={a} className="rounded-pill border border-hairline px-2.5 py-1 text-xs font-semibold text-ink-muted">
                  {a}
                </span>
              ))}
            </div>
          </div>

          <div className="mt-5 flex items-center justify-between gap-3">
            <Link to="/proto/timeline" className="text-sm font-semibold text-ink-muted transition hover:text-ink">
              I'll do this later
            </Link>
            <Link
              to="/proto/timeline"
              className="rounded-control bg-clay px-4 py-2.5 font-sans text-sm font-semibold text-warm-white shadow-soft transition duration-150 ease-keeper hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
            >
              Continue to your vault
            </Link>
          </div>

          <p className="mt-4 text-center text-[0.7rem] italic text-ink-muted/70">
            Prototype — pairing is not wired up. Real pairing (QR / deep-link, scoped session keys) is PRD-01.
          </p>
        </div>
      </div>
    </div>
  );
}
