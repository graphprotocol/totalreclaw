import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ProtoHeader } from "./ProtoHeader";
import { REVIEW_ITEMS, type StaleItem } from "./review-data";

// The signature on-open ritual: 1-3 gentle "still true?" confirmations before you
// dive in. Cheap, high-trust, and every answer improves retrieval — the reason to
// come back. Questions are the stale items the Keeper wants confirmed.
const QUESTIONS = REVIEW_ITEMS.filter((i): i is StaleItem => i.kind === "stale");

export function ProtoCheckin() {
  const navigate = useNavigate();
  const [i, setI] = useState(0);
  const total = QUESTIONS.length;
  const done = i >= total;
  const q = QUESTIONS[i];

  const next = () => setI((n) => n + 1);

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <div className="flex min-h-[calc(100vh-57px)] items-center justify-center px-4 py-10">
        <div className="w-full max-w-md">
          {!done ? (
            <div key={i} className="animate-fade-up text-center">
              <div className="mx-auto mb-5 flex h-12 w-12 items-center justify-center rounded-full bg-clay-tint text-clay-deep">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
                  <path d="M12 2 9.5 9.5 2 12l7.5 2.5L12 22l2.5-7.5L22 12l-7.5-2.5Z" />
                </svg>
              </div>
              <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
                Quick check-in · {i + 1} of {total}
              </p>
              <h1 className="mt-2 text-balance font-display text-[1.7rem] leading-tight text-ink">
                {q.text}
              </h1>
              <p className="mt-2 text-sm text-ink-muted">Still true? You told me this {q.age}.</p>

              <div className="mt-6 flex flex-col gap-2">
                <button
                  type="button"
                  onClick={next}
                  className="rounded-control bg-clay px-4 py-3 font-sans text-sm font-semibold text-warm-white shadow-soft transition duration-150 ease-keeper hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
                >
                  Still true
                </button>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={next}
                    className="flex-1 rounded-control border border-hairline bg-warm-white px-4 py-2.5 font-sans text-sm font-semibold text-ink transition hover:border-ink-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
                  >
                    Update
                  </button>
                  <button
                    type="button"
                    onClick={next}
                    className="flex-1 rounded-control border border-hairline bg-warm-white px-4 py-2.5 font-sans text-sm font-semibold text-ink transition hover:border-ink-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
                  >
                    Not anymore
                  </button>
                </div>
              </div>

              <Link
                to="/proto/review"
                className="mt-5 inline-block text-xs font-semibold text-ink-muted transition hover:text-ink"
              >
                Skip for now
              </Link>
            </div>
          ) : (
            <div className="animate-fade-up text-center">
              <div className="mx-auto mb-5 flex h-12 w-12 items-center justify-center rounded-full bg-clay-tint">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#A54B2E" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <path d="M20 6 9 17l-5-5" />
                </svg>
              </div>
              <h1 className="text-balance font-display text-[1.7rem] leading-tight text-ink">All set</h1>
              <p className="mx-auto mt-2 max-w-xs text-sm leading-relaxed text-ink-muted">
                Thanks for keeping me accurate. That's all I needed — the rest I'll handle quietly.
              </p>
              <button
                type="button"
                onClick={() => navigate("/proto/review")}
                className="mt-6 rounded-control bg-clay px-5 py-2.5 font-sans text-sm font-semibold text-warm-white shadow-soft transition duration-150 ease-keeper hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
              >
                Into your vault
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
