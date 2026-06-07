import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useCrypto } from "../contexts/CryptoContext";
import { isMnemonicValid } from "../lib/crypto";
import { isPasskeyPrfAvailable } from "../lib/auth/prf-support";
import { PrfUnsupportedError } from "../lib/auth/passkey";

type Step =
  | "checking"
  | "unsupported"
  | "choose"
  | "show-phrase"
  | "confirm-backup"
  | "import"
  | "working";

/** Pick `n` distinct 0-based positions from `len` words (backup challenge). */
function pickPositions(len: number, n: number): number[] {
  const pool = Array.from({ length: len }, (_, i) => i);
  for (let i = pool.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  return pool.slice(0, n).sort((a, b) => a - b);
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-warm-white px-5 py-12">
      <div className="mx-auto w-full max-w-md animate-page-in">{children}</div>
    </div>
  );
}

export function BootstrapPage() {
  const { generatePhrase, bootstrap } = useCrypto();
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>("checking");
  const [phrase, setPhrase] = useState("");
  const [positions, setPositions] = useState<number[]>([]);
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const [importValue, setImportValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    isPasskeyPrfAvailable().then((ok) => setStep(ok ? "choose" : "unsupported"));
  }, []);

  const words = phrase ? phrase.trim().split(/\s+/) : [];

  const doBootstrap = useCallback(
    async (mnemonic: string) => {
      setStep("working");
      setError(null);
      try {
        await bootstrap({ mnemonic });
        navigate("/vault", { replace: true });
      } catch (e) {
        if (e instanceof PrfUnsupportedError) {
          setStep("unsupported");
          return;
        }
        setError(e instanceof Error ? e.message : String(e));
        setStep("choose");
      }
    },
    [bootstrap, navigate],
  );

  const startCreate = useCallback(() => {
    const p = generatePhrase();
    setPhrase(p);
    setPositions([]);
    setAnswers({});
    setError(null);
    setStep("show-phrase");
  }, [generatePhrase]);

  const beginBackupCheck = useCallback(() => {
    setPositions(pickPositions(words.length, 3));
    setAnswers({});
    setStep("confirm-backup");
  }, [words.length]);

  const backupOk =
    positions.length > 0 &&
    positions.every((p) => (answers[p] ?? "").trim().toLowerCase() === words[p]);

  // ---- render ----

  if (step === "checking") {
    return (
      <Shell>
        <p className="text-center text-sm text-ink-muted">Checking your browser…</p>
      </Shell>
    );
  }

  if (step === "unsupported") {
    return (
      <Shell>
        <h1 className="font-display text-2xl font-semibold text-ink">Passkeys not available</h1>
        <p className="mt-3 text-ink-muted">
          TotalReclaw needs a passkey (the WebAuthn <span className="font-mono text-sm">prf</span>{" "}
          extension) to protect your vault key on this device. Your browser or platform doesn’t
          support it yet.
        </p>
        <p className="mt-3 text-ink-muted">
          Try a recent Chrome, Edge, or Safari 17+ on a device with Touch ID / Face ID / Windows
          Hello.
        </p>
      </Shell>
    );
  }

  if (step === "working") {
    return (
      <Shell>
        <p className="text-center text-ink-muted">
          Setting up your vault… confirm with your device when prompted.
        </p>
      </Shell>
    );
  }

  if (step === "choose") {
    return (
      <Shell>
        <h1 className="font-display text-3xl font-semibold text-ink">Your memory, kept safe.</h1>
        <p className="mt-3 text-ink-muted">
          Encrypted on your device. We can’t read it. Create a new vault, or restore one you already
          have.
        </p>
        {error && <p className="mt-4 rounded-control bg-clay-tint px-3 py-2 text-sm text-clay-deep">{error}</p>}
        <div className="mt-8 space-y-3">
          <button
            onClick={startCreate}
            className="w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep"
          >
            Create a new vault
          </button>
          <button
            onClick={() => {
              setError(null);
              setStep("import");
            }}
            className="w-full rounded-control bg-surface px-5 py-3 font-semibold text-ink ring-1 ring-hairline transition duration-200 ease-keeper hover:ring-clay"
          >
            I have a recovery phrase
          </button>
        </div>
      </Shell>
    );
  }

  if (step === "show-phrase") {
    return (
      <Shell>
        <h1 className="font-display text-2xl font-semibold text-ink">Write this down</h1>
        <p className="mt-2 text-ink-muted">
          This recovery phrase is the only way to restore your vault if you lose this device. Keep it
          somewhere safe and private. We never see it.
        </p>
        <ol className="mt-6 grid grid-cols-2 gap-2 rounded-card bg-surface p-4 shadow-soft">
          {words.map((w, i) => (
            <li key={i} className="flex items-baseline gap-2 font-mono text-sm text-ink">
              <span className="w-5 shrink-0 text-right text-ink-muted">{i + 1}</span>
              <span>{w}</span>
            </li>
          ))}
        </ol>
        <button
          onClick={beginBackupCheck}
          className="mt-6 w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep"
        >
          I’ve written it down
        </button>
      </Shell>
    );
  }

  if (step === "confirm-backup") {
    return (
      <Shell>
        <h1 className="font-display text-2xl font-semibold text-ink">Confirm your backup</h1>
        <p className="mt-2 text-ink-muted">Type these words from your phrase to confirm you saved it.</p>
        <div className="mt-6 space-y-4">
          {positions.map((p) => (
            <label key={p} className="block">
              <span className="text-sm font-semibold text-ink-muted">Word #{p + 1}</span>
              <input
                value={answers[p] ?? ""}
                onChange={(e) => setAnswers((a) => ({ ...a, [p]: e.target.value }))}
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                className="mt-1 w-full rounded-control bg-surface px-4 py-3 font-mono text-ink ring-1 ring-hairline focus:outline-none focus:ring-2 focus:ring-clay"
              />
            </label>
          ))}
        </div>
        <button
          disabled={!backupOk}
          onClick={() => doBootstrap(phrase)}
          className="mt-6 w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep disabled:cursor-not-allowed disabled:opacity-40"
        >
          Create my vault
        </button>
        <button
          onClick={() => setStep("show-phrase")}
          className="mt-2 w-full rounded-control px-5 py-2 text-sm font-semibold text-ink-muted hover:text-ink"
        >
          Show the phrase again
        </button>
      </Shell>
    );
  }

  // step === "import"
  const importValid = isMnemonicValid(importValue);
  return (
    <Shell>
      <h1 className="font-display text-2xl font-semibold text-ink">Restore your vault</h1>
      <p className="mt-2 text-ink-muted">
        Enter your recovery phrase. This device will enrol its own passkey so you won’t need to type
        it again.
      </p>
      {error && <p className="mt-4 rounded-control bg-clay-tint px-3 py-2 text-sm text-clay-deep">{error}</p>}
      <textarea
        value={importValue}
        onChange={(e) => setImportValue(e.target.value)}
        rows={3}
        placeholder="twelve words separated by spaces"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        className="mt-6 w-full rounded-card bg-surface px-4 py-3 font-mono text-sm text-ink ring-1 ring-hairline focus:outline-none focus:ring-2 focus:ring-clay"
      />
      <button
        disabled={!importValid}
        onClick={() => doBootstrap(importValue.trim().toLowerCase())}
        className="mt-4 w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep disabled:cursor-not-allowed disabled:opacity-40"
      >
        Restore vault
      </button>
      <button
        onClick={() => {
          setError(null);
          setStep("choose");
        }}
        className="mt-2 w-full rounded-control px-5 py-2 text-sm font-semibold text-ink-muted hover:text-ink"
      >
        Back
      </button>
    </Shell>
  );
}
