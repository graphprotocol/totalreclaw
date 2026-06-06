import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { clsx } from "clsx";
import { generateMnemonic } from "@scure/bip39";
import { wordlist } from "@scure/bip39/wordlists/english";
import { ProtoHeader } from "./ProtoHeader";

const STEP_COUNT = 5;

function pickConfirmIndices(): number[] {
  const idx = new Set<number>();
  while (idx.size < 4) idx.add(Math.floor(Math.random() * 12));
  return [...idx].sort((a, b) => a - b);
}

function PrimaryBtn({
  children,
  onClick,
  disabled,
  className,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        "w-full rounded-control px-4 py-3 font-sans text-sm font-semibold shadow-soft transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2",
        disabled
          ? "cursor-not-allowed bg-hairline text-ink-muted shadow-none"
          : "bg-clay text-warm-white hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised",
        className,
      )}
    >
      {children}
    </button>
  );
}

/** New-account flow (non-functional): generates a REAL BIP-39 phrase client-side,
 *  models the locked onboarding (backup warning + 4-word confirm + passkey + tour). */
export function ProtoOnboarding() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const phrase = useMemo(() => generateMnemonic(wordlist, 128), []);
  const words = useMemo(() => phrase.split(" "), [phrase]);
  const confirmIdx = useMemo(pickConfirmIndices, []);
  const [saved, setSaved] = useState(false);
  const [copied, setCopied] = useState(false);
  const [answers, setAnswers] = useState<Record<number, string>>({});

  const next = () => setStep((s) => Math.min(s + 1, STEP_COUNT - 1));
  const confirmOk = confirmIdx.every((i) => (answers[i] ?? "").trim().toLowerCase() === words[i]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(phrase);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — fine, it's a prototype */
    }
  };

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <div className="flex min-h-[calc(100vh-57px)] items-start justify-center px-4 py-10">
        <div className="animate-page-in w-full max-w-md">
          <div className="mb-6 flex items-center justify-center gap-1.5">
            {Array.from({ length: STEP_COUNT }, (_, i) => (
              <span
                key={i}
                className={clsx(
                  "h-1.5 rounded-full transition-all duration-200",
                  i === step ? "w-6 bg-clay" : i < step ? "w-1.5 bg-clay" : "w-1.5 bg-hairline",
                )}
              />
            ))}
          </div>

          <div className="rounded-card bg-surface p-6 shadow-soft">
            {step === 0 && (
              <div className="text-center">
                <h1 className="text-balance font-display text-[1.8rem] leading-tight text-ink">Create your vault</h1>
                <p className="mt-3 text-sm leading-relaxed text-ink-muted">
                  Your memory is encrypted on your device.{" "}
                  <span className="font-semibold text-ink">We can't read it</span> — and we can't
                  recover it for you. Next you'll get a recovery phrase: the only key to your vault.
                </p>
                <PrimaryBtn onClick={next} className="mt-6">
                  Create a recovery phrase
                </PrimaryBtn>
                <p className="mt-3 text-xs text-ink-muted">
                  Already have one?{" "}
                  <Link to="/proto/pair" className="font-semibold text-clay-deep hover:underline">
                    Unlock instead
                  </Link>
                </p>
              </div>
            )}

            {step === 1 && (
              <div>
                <h2 className="font-display text-xl text-ink">Your recovery phrase</h2>
                <p className="mt-1 text-sm leading-relaxed text-ink-muted">
                  These 12 words <span className="font-semibold text-ink">are</span> your vault.
                  Anyone with them can read your memory, and we can never recover them for you.
                </p>
                <div className="mt-4 grid grid-cols-3 gap-2">
                  {words.map((w, i) => (
                    <div
                      key={i}
                      className="flex items-center gap-1.5 rounded-control border border-hairline bg-warm-white px-2.5 py-1.5"
                    >
                      <span className="font-mono text-[0.7rem] text-ink-muted/70">{i + 1}</span>
                      <span className="font-mono text-sm text-ink">{w}</span>
                    </div>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={copy}
                  className="mt-3 text-xs font-semibold text-clay-deep transition hover:underline"
                >
                  {copied ? "Copied ✓" : "Copy phrase"}
                </button>
                <label className="mt-4 flex items-start gap-2.5 text-sm leading-snug text-ink">
                  <input
                    type="checkbox"
                    checked={saved}
                    onChange={(e) => setSaved(e.target.checked)}
                    className="mt-0.5 h-4 w-4 shrink-0 accent-clay"
                  />
                  <span>
                    I've stored my recovery phrase somewhere safe (a password manager or written
                    down). I understand it can't be recovered.
                  </span>
                </label>
                <PrimaryBtn onClick={next} disabled={!saved} className="mt-5">
                  Continue
                </PrimaryBtn>
              </div>
            )}

            {step === 2 && (
              <div>
                <h2 className="font-display text-xl text-ink">Confirm your phrase</h2>
                <p className="mt-1 text-sm leading-relaxed text-ink-muted">
                  Type these words from your phrase, so we know you saved it.
                </p>
                <div className="mt-4 space-y-2.5">
                  {confirmIdx.map((i) => {
                    const ok = (answers[i] ?? "").trim().toLowerCase() === words[i];
                    return (
                      <label key={i} className="flex items-center gap-3">
                        <span className="w-16 shrink-0 text-sm text-ink-muted">Word {i + 1}</span>
                        <input
                          value={answers[i] ?? ""}
                          onChange={(e) => setAnswers((a) => ({ ...a, [i]: e.target.value }))}
                          autoComplete="off"
                          autoCapitalize="none"
                          spellCheck={false}
                          className={clsx(
                            "w-full rounded-control border bg-warm-white px-3 py-2 font-mono text-sm text-ink transition focus:outline-none focus:ring-2 focus:ring-clay/35",
                            ok && (answers[i] ?? "") ? "border-clay" : "border-hairline focus:border-clay",
                          )}
                        />
                      </label>
                    );
                  })}
                </div>
                <PrimaryBtn onClick={next} disabled={!confirmOk} className="mt-5">
                  Confirm
                </PrimaryBtn>
              </div>
            )}

            {step === 3 && (
              <div className="text-center">
                <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-clay-tint">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#A54B2E" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 11c-1.5 0-2 1-2 3.5 0 2 .5 3.5 1 4.5" />
                    <path d="M8.5 8.5A5 5 0 0 1 17 12c0 3 .5 5 1 6" />
                    <path d="M5.5 11a6.5 6.5 0 0 1 11-4.7" />
                    <path d="M12 14c0 2 .3 3.5.8 5" />
                  </svg>
                </div>
                <h2 className="font-display text-xl text-ink">Skip the phrase next time</h2>
                <p className="mt-2 text-sm leading-relaxed text-ink-muted">
                  Enable Face ID / Touch ID on this device. You'll unlock with a glance —{" "}
                  <span className="font-semibold text-ink">you won't type your phrase again</span>{" "}
                  unless you switch devices.
                </p>
                <PrimaryBtn onClick={next} className="mt-6">
                  Enable Face ID / Touch ID
                </PrimaryBtn>
                <button
                  type="button"
                  onClick={next}
                  className="mt-3 block w-full text-xs font-semibold text-ink-muted transition hover:text-ink"
                >
                  Maybe later
                </button>
              </div>
            )}

            {step === 4 && (
              <div>
                <h2 className="font-display text-xl text-ink">You're set</h2>
                <ul className="mt-3 space-y-3 text-sm leading-snug text-ink">
                  {[
                    ["Your map.", "Topics and entities your agent remembers, as a graph."],
                    ["Pair an agent.", "Connect Hermes so it starts remembering for you."],
                    ["Curate anytime.", "Pin, retype, or delete any memory."],
                  ].map(([h, t]) => (
                    <li key={h} className="flex gap-2.5">
                      <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-clay" aria-hidden />
                      <span>
                        <span className="font-semibold">{h}</span> {t}
                      </span>
                    </li>
                  ))}
                </ul>
                <PrimaryBtn onClick={() => navigate("/proto/timeline?empty")} className="mt-6">
                  Open your vault
                </PrimaryBtn>
              </div>
            )}
          </div>

          <p className="mt-4 text-center text-xs leading-relaxed text-ink-muted">
            Only you can read this. We can't.
          </p>
        </div>
      </div>
    </div>
  );
}
