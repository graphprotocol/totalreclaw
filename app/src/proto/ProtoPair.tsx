import { useCallback, useRef, useState, type KeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { validateMnemonic } from "@scure/bip39";
import { wordlist } from "@scure/bip39/wordlists/english";
import { ProtoHeader } from "./ProtoHeader";

const WORD_COUNT = 12;
const SAMPLE = [...Array(11).fill("abandon"), "about"];

/** Prototype unlock — NON-FUNCTIONAL. Returning users unlock via passkey (the spec's
 *  day-to-day path); the recovery phrase is the new-device / lost-passkey fallback only.
 *  Both paths just navigate. Real auth lives at /pair. */
export function ProtoPair() {
  const navigate = useNavigate();
  const [showPhrase, setShowPhrase] = useState(false);
  const [words, setWords] = useState<string[]>(Array(WORD_COUNT).fill(""));
  const inputs = useRef<(HTMLInputElement | null)[]>([]);

  const open = useCallback(() => navigate("/proto/kg"), [navigate]);

  const setWord = useCallback((i: number, v: string) => {
    const parts = v.trim().split(/\s+/);
    if (parts.length === WORD_COUNT) {
      setWords(parts.map((w) => w.toLowerCase()));
      inputs.current[WORD_COUNT - 1]?.focus();
      return;
    }
    setWords((prev) => {
      const next = [...prev];
      next[i] = v.toLowerCase().replace(/[^a-z]/g, "");
      return next;
    });
  }, []);

  const onKey = useCallback(
    (i: number, e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === " " || e.key === "Enter") {
        e.preventDefault();
        inputs.current[i + 1]?.focus();
      } else if (e.key === "Backspace" && words[i] === "" && i > 0) {
        e.preventDefault();
        inputs.current[i - 1]?.focus();
      }
    },
    [words],
  );

  const filled = words.filter(Boolean).length;
  const allFilled = filled === WORD_COUNT;
  const valid = allFilled && validateMnemonic(words.join(" ").trim(), wordlist);

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <div className="flex min-h-[calc(100vh-57px)] items-center justify-center px-4 py-10">
        <div className="animate-page-in w-full max-w-md">
          <div className="mb-7 text-center">
            <h1 className="font-display text-[2rem] leading-tight text-ink">Welcome back</h1>
            <p className="mt-2 text-sm leading-relaxed text-ink-muted">
              Unlock your memory on this device.
            </p>
          </div>

          {!showPhrase ? (
            <div className="rounded-card bg-surface p-6 text-center shadow-soft">
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-clay-tint">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#A54B2E" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 11c-1.5 0-2 1-2 3.5 0 2 .5 3.5 1 4.5" />
                  <path d="M8.5 8.5A5 5 0 0 1 17 12c0 3 .5 5 1 6" />
                  <path d="M5.5 11a6.5 6.5 0 0 1 11-4.7" />
                  <path d="M12 14c0 2 .3 3.5.8 5" />
                </svg>
              </div>
              <button
                type="button"
                onClick={open}
                className="w-full rounded-control bg-clay px-4 py-3 font-sans text-sm font-semibold text-warm-white shadow-soft transition duration-150 ease-keeper hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
              >
                Unlock with Face ID / Touch ID
              </button>
              <p className="mt-3 text-xs text-ink-muted">
                Your passkey stays on this device. Only you can unlock.
              </p>

              <div className="my-5 flex items-center gap-3 text-xs text-ink-muted">
                <span className="h-px flex-1 bg-hairline" />
                or
                <span className="h-px flex-1 bg-hairline" />
              </div>

              <button
                type="button"
                onClick={() => setShowPhrase(true)}
                className="text-sm font-semibold text-clay-deep transition hover:underline"
              >
                Use your recovery phrase
              </button>
            </div>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                open();
              }}
              className="rounded-card bg-surface p-6 shadow-soft"
            >
              <div className="mb-3 flex items-center justify-between gap-2">
                <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
                  New device or lost passkey
                </span>
                <button
                  type="button"
                  onClick={() => setShowPhrase(false)}
                  className="text-xs font-semibold text-ink-muted transition hover:text-ink"
                >
                  ← Back
                </button>
              </div>

              <div className="grid grid-cols-3 gap-2">
                {words.map((w, i) => (
                  <div key={i} className="relative">
                    <span className="pointer-events-none absolute left-2.5 top-1/2 w-4 -translate-y-1/2 select-none text-right font-mono text-[0.7rem] text-ink-muted/70">
                      {i + 1}
                    </span>
                    <input
                      ref={(el) => {
                        inputs.current[i] = el;
                      }}
                      value={w}
                      onChange={(e) => setWord(i, e.target.value)}
                      onKeyDown={(e) => onKey(i, e)}
                      autoComplete="off"
                      autoCorrect="off"
                      autoCapitalize="none"
                      spellCheck={false}
                      className="w-full rounded-control border border-hairline bg-warm-white py-1.5 pl-8 pr-2 font-mono text-sm text-ink transition placeholder:text-ink-muted/40 focus:border-clay focus:outline-none focus:ring-2 focus:ring-clay/35"
                    />
                  </div>
                ))}
              </div>

              <div className="mt-3 h-5 text-center text-xs">
                {allFilled ? (
                  valid ? (
                    <span className="font-semibold text-clay-deep">✓ Looks like a valid recovery phrase</span>
                  ) : (
                    <span className="text-ink-muted">Not a valid BIP-39 phrase — fine, this is a prototype.</span>
                  )
                ) : (
                  <span className="text-ink-muted">{filled}/12 words</span>
                )}
              </div>

              <button
                type="submit"
                className="mt-3 w-full rounded-control bg-clay px-4 py-3 font-sans text-sm font-semibold text-warm-white shadow-soft transition duration-150 ease-keeper hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
              >
                Restore vault
              </button>
              <button
                type="button"
                onClick={() => setWords([...SAMPLE])}
                className="mt-3 block w-full text-center text-xs font-semibold text-ink-muted transition hover:text-ink"
              >
                Use a sample phrase
              </button>
            </form>
          )}

          <p className="mt-4 text-center text-xs leading-relaxed text-ink-muted">
            New here?{" "}
            <Link to="/proto/onboarding" className="font-semibold text-clay-deep hover:underline">
              Create a vault
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
