import { useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useCrypto } from "../contexts/CryptoContext";
import { isMnemonicValid } from "../lib/crypto";
import { mapUnlockError, type UnlockErrorContext } from "../lib/errorTaxonomy";

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-warm-white px-5 py-12">
      <div className="mx-auto w-full max-w-md animate-page-in">{children}</div>
    </div>
  );
}

function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return <p className="mt-4 rounded-control bg-clay-tint px-3 py-2 text-sm text-clay-deep">{message}</p>;
}

export function UnlockPage() {
  const { status, unlock, unlockWithPhrase, restoreFromEscrow, restoreFromFile } = useCrypto();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showPhrase, setShowPhrase] = useState(false);
  const [phrase, setPhrase] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const goVault = useCallback(() => navigate("/memory", { replace: true }), [navigate]);

  const runGuarded = useCallback(
    async (context: UnlockErrorContext, fn: () => Promise<void>) => {
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        await fn();
      } catch (e) {
        setError(mapUnlockError(e, context).message);
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const handlePasskey = useCallback(
    () =>
      runGuarded("local", async () => {
        await unlock();
        goVault();
      }),
    [runGuarded, unlock, goVault],
  );

  const handleRecoverWithPasskey = useCallback(
    () =>
      runGuarded("escrow", async () => {
        const result = await restoreFromEscrow();
        if (result === "no-escrow") {
          setNotice(
            "We couldn’t find a backup for this passkey. Try a different passkey, or use your recovery phrase.",
          );
          return;
        }
        goVault();
      }),
    [runGuarded, restoreFromEscrow, goVault],
  );

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      e.target.value = ""; // allow re-selecting the same file later
      if (!file) return;
      void runGuarded("file", async () => {
        const result = await restoreFromFile(file);
        if (result === "bad-file") {
          setNotice("This doesn’t look like a TotalReclaw backup file.");
          return;
        }
        if (result === "unsupported-version") {
          setNotice("This backup was made by a newer version of TotalReclaw.");
          return;
        }
        if (result === "wrong-passkey") {
          setNotice("This backup was made with a different passkey.");
          return;
        }
        goVault();
      });
    },
    [runGuarded, restoreFromFile, goVault],
  );

  const handlePhrase = useCallback(
    () =>
      runGuarded("local", async () => {
        await unlockWithPhrase(phrase.trim().toLowerCase(), { reEnrol: true });
        goVault();
      }),
    [runGuarded, phrase, unlockWithPhrase, goVault],
  );

  const handleCreateNew = useCallback(() => {
    if (
      !confirm(
        "Create a new vault instead of recovering? Your existing memories aren’t deleted — you can still reach them later with the right passkey or phrase — but this device will start a separate, empty vault.",
      )
    ) {
      return;
    }
    navigate("/bootstrap");
  }, [navigate]);

  const phraseEntry = (
    <div className="mt-4">
      <textarea
        value={phrase}
        onChange={(e) => setPhrase(e.target.value)}
        rows={3}
        placeholder="twelve words separated by spaces"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        className="w-full rounded-card bg-surface px-4 py-3 font-mono text-sm text-ink ring-1 ring-hairline focus:outline-none focus:ring-2 focus:ring-clay"
      />
      <button
        disabled={busy || !isMnemonicValid(phrase)}
        onClick={handlePhrase}
        className="mt-4 w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep disabled:cursor-not-allowed disabled:opacity-40"
      >
        {busy ? "Restoring…" : "Restore & enrol passkey"}
      </button>
      <button
        disabled={busy}
        onClick={() => {
          setError(null);
          setNotice(null);
          setShowPhrase(false);
        }}
        className="mt-2 w-full rounded-control px-5 py-2 text-sm font-semibold text-ink-muted hover:text-ink"
      >
        Back
      </button>
    </div>
  );

  // ---- no local VaultRecord: four-way recovery choice (§6.3) ----
  if (status === "no-vault") {
    return (
      <Shell>
        <h1 className="font-display text-3xl font-semibold text-ink">Welcome back.</h1>
        <p className="mt-3 text-ink-muted">
          This device doesn’t have a vault yet. Recover an existing one, or start fresh.
        </p>
        <ErrorBanner message={error} />
        {notice && <p className="mt-4 rounded-control bg-surface px-3 py-2 text-sm text-ink-muted ring-1 ring-hairline">{notice}</p>}

        {!showPhrase ? (
          <div className="mt-8 space-y-3">
            <button
              disabled={busy}
              onClick={handleRecoverWithPasskey}
              className="w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep disabled:opacity-50"
            >
              {busy ? "Recovering…" : "Recover with passkey"}
            </button>
            <button
              disabled={busy}
              onClick={() => fileInputRef.current?.click()}
              className="w-full rounded-control bg-surface px-5 py-3 font-semibold text-ink ring-1 ring-hairline transition duration-200 ease-keeper hover:ring-clay disabled:opacity-50"
            >
              Open a backup file
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="application/json"
              onChange={handleFileChange}
              className="hidden"
            />
            <button
              disabled={busy}
              onClick={() => {
                setError(null);
                setNotice(null);
                setShowPhrase(true);
              }}
              className="w-full rounded-control px-5 py-2 text-sm font-semibold text-ink-muted hover:text-ink"
            >
              Use recovery phrase instead
            </button>
            <div className="pt-2 text-center">
              <button
                disabled={busy}
                onClick={handleCreateNew}
                className="text-xs font-semibold text-ink-muted underline decoration-dotted hover:text-ink"
              >
                Create a new vault
              </button>
            </div>
          </div>
        ) : (
          phraseEntry
        )}
      </Shell>
    );
  }

  // ---- status === "locked": local VaultRecord present on this device ----
  return (
    <Shell>
      <h1 className="font-display text-3xl font-semibold text-ink">Welcome back.</h1>
      <p className="mt-3 text-ink-muted">Unlock your vault with your passkey.</p>

      <ErrorBanner message={error} />

      {!showPhrase ? (
        <div className="mt-8 space-y-3">
          <button
            disabled={busy}
            onClick={handlePasskey}
            className="w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep disabled:opacity-50"
          >
            {busy ? "Unlocking…" : "Unlock with passkey"}
          </button>
          <button
            disabled={busy}
            onClick={() => {
              setError(null);
              setShowPhrase(true);
            }}
            className="w-full rounded-control px-5 py-2 text-sm font-semibold text-ink-muted hover:text-ink"
          >
            Use recovery phrase instead
          </button>
        </div>
      ) : (
        <div className="mt-8">
          <p className="text-ink-muted">
            New device or lost passkey? Enter your recovery phrase. This device will enrol a fresh
            passkey.
          </p>
          {phraseEntry}
        </div>
      )}
    </Shell>
  );
}
