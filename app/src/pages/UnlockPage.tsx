import { useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useCrypto } from "../contexts/CryptoContext";
import { isMnemonicValid } from "../lib/crypto";

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-warm-white px-5 py-12">
      <div className="mx-auto w-full max-w-md animate-page-in">{children}</div>
    </div>
  );
}

export function UnlockPage() {
  const { unlock, unlockWithPhrase } = useCrypto();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPhrase, setShowPhrase] = useState(false);
  const [phrase, setPhrase] = useState("");

  const goVault = useCallback(() => navigate("/vault", { replace: true }), [navigate]);

  const handlePasskey = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await unlock();
      goVault();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [unlock, goVault]);

  const handlePhrase = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await unlockWithPhrase(phrase.trim().toLowerCase(), { reEnrol: true });
      goVault();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [phrase, unlockWithPhrase, goVault]);

  return (
    <Shell>
      <h1 className="font-display text-3xl font-semibold text-ink">Welcome back.</h1>
      <p className="mt-3 text-ink-muted">Unlock your vault with your passkey.</p>

      {error && (
        <p className="mt-4 rounded-control bg-clay-tint px-3 py-2 text-sm text-clay-deep">{error}</p>
      )}

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
          <textarea
            value={phrase}
            onChange={(e) => setPhrase(e.target.value)}
            rows={3}
            placeholder="twelve words separated by spaces"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            className="mt-4 w-full rounded-card bg-surface px-4 py-3 font-mono text-sm text-ink ring-1 ring-hairline focus:outline-none focus:ring-2 focus:ring-clay"
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
              setShowPhrase(false);
            }}
            className="mt-2 w-full rounded-control px-5 py-2 text-sm font-semibold text-ink-muted hover:text-ink"
          >
            Back to passkey
          </button>
        </div>
      )}
    </Shell>
  );
}
