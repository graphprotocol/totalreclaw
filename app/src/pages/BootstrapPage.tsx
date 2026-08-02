import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useCrypto, type EscrowChoice } from "../contexts/CryptoContext";
import { isMnemonicValid } from "../lib/crypto";
import { isPasskeyPrfAvailable } from "../lib/auth/prf-support";
import { PrfUnsupportedError } from "../lib/auth/passkey";
import { download } from "../lib/download";
import { escrowFilename } from "../lib/auth/escrow";
import { recordFileBackup } from "../lib/vault/fileBackupRecord";

type Step =
  | "checking"
  | "unsupported"
  | "choose"
  | "show-phrase"
  | "confirm-backup"
  | "escrow-consent"
  | "file-caveat"
  | "import"
  | "working"
  | "escrow-notice";

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

  const goVault = useCallback(() => navigate("/memory", { replace: true }), [navigate]);

  const doBootstrap = useCallback(
    async (mnemonic: string, escrow: EscrowChoice) => {
      setStep("working");
      setError(null);
      try {
        const result = await bootstrap({ mnemonic, escrow });
        if (result.escrowFile) {
          download(escrowFilename(), JSON.stringify(result.escrowFile, null, 2), "application/json");
          recordFileBackup(result.smartAccount);
        }
        if (result.escrowSaveFailed) {
          setStep("escrow-notice");
          return;
        }
        goVault();
      } catch (e) {
        if (e instanceof PrfUnsupportedError) {
          setStep("unsupported");
          return;
        }
        setError(e instanceof Error ? e.message : String(e));
        setStep("choose");
      }
    },
    [bootstrap, goVault],
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
          Try a recent Chrome, Edge, or Safari 18+ (iOS 18+) on a device with Touch ID / Face ID / Windows
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

  if (step === "escrow-notice") {
    return (
      <Shell>
        <h1 className="font-display text-2xl font-semibold text-ink">Your vault is ready</h1>
        <p className="mt-3 rounded-control bg-clay-tint px-3 py-2 text-sm text-clay-deep">
          We couldn’t save your backup. You can turn it on later in Settings.
        </p>
        <p className="mt-3 text-ink-muted">
          Your written recovery phrase still works — nothing about your vault is affected.
        </p>
        <button
          onClick={goVault}
          className="mt-6 w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep"
        >
          Continue to your vault
        </button>
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
          onClick={() => setStep("escrow-consent")}
          className="mt-6 w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep disabled:cursor-not-allowed disabled:opacity-40"
        >
          Continue
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

  if (step === "escrow-consent") {
    return (
      <Shell>
        <h1 className="font-display text-2xl font-semibold text-ink">Back up your recovery phrase?</h1>
        <p className="mt-3 text-ink-muted">
          We can store an encrypted copy of your phrase, locked to this passkey. If you lose this
          device, your passkey unlocks it again on a new one.
        </p>
        <p className="mt-3 text-ink-muted">
          We can’t read it — only your passkey can unlock it. But it does mean someone who takes over
          both our servers and your Apple or Google account could reach your memories. Without a
          backup, losing this device and your written phrase means losing your vault permanently.
        </p>
        <div className="mt-6 space-y-3">
          <button
            onClick={() => doBootstrap(phrase, "relay")}
            className="w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep"
          >
            Back up my phrase
          </button>
          <button
            onClick={() => setStep("file-caveat")}
            className="w-full rounded-control bg-surface px-5 py-3 font-semibold text-ink ring-1 ring-hairline transition duration-200 ease-keeper hover:ring-clay"
          >
            Download an encrypted file instead
          </button>
          <button
            onClick={() => doBootstrap(phrase, "none")}
            className="w-full rounded-control px-5 py-2 text-sm font-semibold text-ink-muted hover:text-ink"
          >
            Not now
          </button>
        </div>
        <p className="mt-4 text-center text-xs text-ink-muted">You can change this any time in Settings.</p>
      </Shell>
    );
  }

  if (step === "file-caveat") {
    return (
      <Shell>
        <h1 className="font-display text-2xl font-semibold text-ink">Where you put this matters.</h1>
        <p className="mt-3 text-ink-muted">
          If you save it to the same account that stores your passkey — iCloud with iCloud Keychain,
          or Google Drive with Google Password Manager — then anyone who gets into that account has
          both halves, and the file protects you less than it appears to. Somewhere separate is safer:
          a different provider, or a USB drive.
        </p>
        <div className="mt-6 space-y-3">
          <button
            onClick={() => doBootstrap(phrase, "file")}
            className="w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep"
          >
            Download backup
          </button>
          <button
            onClick={() => setStep("escrow-consent")}
            className="w-full rounded-control px-5 py-2 text-sm font-semibold text-ink-muted hover:text-ink"
          >
            Back
          </button>
        </div>
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
        onClick={() => {
          setPhrase(importValue.trim().toLowerCase());
          setStep("escrow-consent");
        }}
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
