import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCrypto } from "../contexts/CryptoContext";
import { useVault } from "../hooks/useVault";
import { getAccount, listEscrow, deleteEscrow, type EscrowRecordMeta } from "../lib/api";
import { AppHeader } from "../components/AppHeader";
import { toExportJson, toExportMarkdown, exportFilename } from "../lib/export";
import { download } from "../lib/download";
import { escrowFilename } from "../lib/auth/escrow";
import { recordFileBackup, getLastFileBackupAt } from "../lib/vault/fileBackupRecord";
import { isMnemonicValid } from "../lib/crypto";
import type { VaultItem } from "../lib/types";

/** Sensitive-action gate (#323): the file is unencrypted plaintext. */
function confirmThenExport(items: VaultItem[], format: "json" | "md") {
  if (
    !confirm(
      `Export ${items.length} memories as ${format === "json" ? ".json" : ".md"}? The file is UNENCRYPTED — anyone who opens it can read your memories. Keep it somewhere safe.`,
    )
  ) {
    return;
  }
  if (format === "json") {
    download(exportFilename("json"), toExportJson(items), "application/json");
  } else {
    download(exportFilename("md"), toExportMarkdown(items), "text/markdown");
  }
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-6 rounded-card bg-surface p-5 shadow-soft">
      <h2 className="font-display text-lg font-semibold text-ink">{title}</h2>
      <div className="mt-3 text-sm text-ink-muted">{children}</div>
    </section>
  );
}

function formatDate(iso: string | null): string {
  if (!iso) return "never";
  try {
    return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  } catch {
    return iso;
  }
}

/**
 * Inline "re-enter your phrase" form shared by "Back up this device's
 * passkey" and "Download a new backup" — the mnemonic isn't held anywhere
 * after bootstrap, so both actions need it re-typed once, then one fresh
 * passkey assertion (no re-enrolment) seals it. See CryptoContext's
 * enableEscrowBackup / sealPhraseToFile docstrings.
 */
function PhraseEntryForm({
  title,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  title: string;
  submitLabel: string;
  onSubmit: (mnemonic: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSubmit(value.trim().toLowerCase());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 rounded-control bg-warm-white p-4 ring-1 ring-hairline">
      <p className="text-sm font-semibold text-ink">{title}</p>
      <p className="mt-1 text-xs text-ink-muted">
        Your phrase isn’t stored anywhere — re-enter it once to seal a fresh backup under this
        device’s passkey.
      </p>
      {error && <p className="mt-2 rounded-control bg-clay-tint px-3 py-2 text-xs text-clay-deep">{error}</p>}
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        rows={2}
        placeholder="twelve words separated by spaces"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        className="mt-2 w-full rounded-control bg-surface px-3 py-2 font-mono text-xs text-ink ring-1 ring-hairline focus:outline-none focus:ring-2 focus:ring-clay"
      />
      <div className="mt-2 flex gap-2">
        <button
          disabled={busy || !isMnemonicValid(value)}
          onClick={submit}
          className="rounded-control bg-clay px-3 py-1.5 text-xs font-semibold text-warm-white hover:bg-clay-deep disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? "Working…" : submitLabel}
        </button>
        <button
          disabled={busy}
          onClick={onCancel}
          className="rounded-control px-3 py-1.5 text-xs font-semibold text-ink-muted hover:text-ink"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

/** The mandatory §5.4 caveat, shown at the moment of download — not behind a tooltip. */
function FileCaveatNotice() {
  return (
    <div className="mt-3 rounded-control bg-clay-tint px-3 py-2 text-xs text-clay-deep">
      <p className="font-semibold">Where you put this matters.</p>
      <p className="mt-1">
        If you save it to the same account that stores your passkey — iCloud with iCloud Keychain, or
        Google Drive with Google Password Manager — anyone who gets into that account has both
        halves. Somewhere separate is safer: a different provider, or a USB drive.
      </p>
    </div>
  );
}

function PasskeysAndBackupSection() {
  const { keys, smartAccount, enableEscrowBackup, sealPhraseToFile } = useCrypto();
  const queryClient = useQueryClient();
  const [activeForm, setActiveForm] = useState<"relay" | "file" | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const {
    data: records,
    isError: recordsErrored,
    isLoading: recordsLoading,
  } = useQuery({
    queryKey: ["escrow-records", smartAccount],
    queryFn: () => listEscrow(keys!),
    enabled: !!keys,
    retry: false,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["escrow-records", smartAccount] });

  const onRemove = async (record: EscrowRecordMeta) => {
    if (
      !confirm(
        "Remove this backup?\n\nYou won’t be able to recover your vault with that passkey any more. Your written recovery phrase still works.",
      )
    ) {
      return;
    }
    setBusyId(record.escrowId);
    try {
      await deleteEscrow(keys!, record.escrowId);
      await invalidate();
    } finally {
      setBusyId(null);
    }
  };

  const onEnableRelay = async (mnemonic: string) => {
    await enableEscrowBackup(mnemonic, { label: "Passkey backup" });
    setActiveForm(null);
    setMessage("Backed up to TotalReclaw.");
    await invalidate();
  };

  const onDownloadFile = async (mnemonic: string) => {
    const file = await sealPhraseToFile(mnemonic);
    download(escrowFilename(), JSON.stringify(file, null, 2), "application/json");
    if (smartAccount) recordFileBackup(smartAccount);
    setActiveForm(null);
    setMessage("Encrypted backup downloaded.");
  };

  const lastFileBackup = smartAccount ? getLastFileBackupAt(smartAccount) : null;

  return (
    <Section title="Passkeys & backup">
      <p>
        Back up your recovery phrase to a passkey — losing this device no longer means losing your
        vault, as long as you still have the passkey.
      </p>
      <p className="mt-2 text-xs">
        We can’t show which row belongs to which device — the relay only knows labels, and a row’s
        handle is only derivable by its own passkey. Dates usually identify them well enough. If it’s
        genuinely ambiguous, the safe move is to remove all of them and back up again.
      </p>

      {message && (
        <p className="mt-3 rounded-control bg-surface px-3 py-2 text-xs text-ink ring-1 ring-hairline">
          {message}
        </p>
      )}

      <ul className="mt-3 space-y-2">
        {(records ?? []).map((r) => (
          <li key={r.escrowId} className="flex items-center justify-between gap-3 text-xs">
            <span>
              {r.label ?? "Passkey backup"} · added {formatDate(r.createdAt)} · last used{" "}
              {formatDate(r.lastFetchedAt)}
            </span>
            <button
              disabled={busyId === r.escrowId}
              onClick={() => onRemove(r)}
              className="shrink-0 rounded-control px-2 py-1 font-semibold text-clay-deep hover:bg-clay-tint disabled:opacity-50"
            >
              Remove
            </button>
          </li>
        ))}
        {recordsLoading && <li className="text-xs text-ink-muted">Loading…</li>}
        {recordsErrored && (
          <li className="text-xs text-ink-muted">Couldn’t reach TotalReclaw to check your backups.</li>
        )}
        {!recordsLoading && !recordsErrored && records?.length === 0 && (
          <li className="text-xs text-ink-muted">No relay backups yet.</li>
        )}
      </ul>

      <p className="mt-4 text-xs">
        Encrypted file downloaded: <span className="font-semibold text-ink">{formatDate(lastFileBackup)}</span>
        {lastFileBackup && (
          <span className="text-ink-muted"> — downloading a new one doesn’t affect the old one; both keep working.</span>
        )}
      </p>

      {activeForm === "relay" && (
        <PhraseEntryForm
          title="Back up this device's passkey"
          submitLabel="Back up"
          onSubmit={onEnableRelay}
          onCancel={() => setActiveForm(null)}
        />
      )}
      {activeForm === "file" && (
        <div className="mt-3">
          <FileCaveatNotice />
          <PhraseEntryForm
            title="Download an encrypted backup file"
            submitLabel="Download backup"
            onSubmit={onDownloadFile}
            onCancel={() => setActiveForm(null)}
          />
        </div>
      )}

      {!activeForm && (
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            onClick={() => {
              setMessage(null);
              setActiveForm("relay");
            }}
            className="rounded-control bg-clay px-3 py-1.5 text-xs font-semibold text-warm-white hover:bg-clay-deep"
          >
            Back up this device’s passkey
          </button>
          <button
            onClick={() => {
              setMessage(null);
              setActiveForm("file");
            }}
            className="rounded-control bg-surface px-3 py-1.5 text-xs font-semibold text-ink ring-1 ring-hairline hover:ring-clay"
          >
            Download a new backup
          </button>
        </div>
      )}
    </Section>
  );
}

export function SettingsPage() {
  const { keys, smartAccount, chainId, forgetDevice } = useCrypto();
  const navigate = useNavigate();
  const { data } = useVault(keys);
  const items = data?.items ?? [];
  const { data: account } = useQuery({
    queryKey: ["billing", smartAccount],
    queryFn: () => getAccount(keys!),
    enabled: !!keys,
  });
  const [busy, setBusy] = useState(false);

  const onForget = async () => {
    if (!confirm("Forget this device? Your vault stays safe on-chain — this only removes the keys from this browser. You’ll need your passkey or recovery phrase to return.")) {
      return;
    }
    setBusy(true);
    await forgetDevice();
    navigate("/unlock", { replace: true });
  };

  return (
    <div className="min-h-screen bg-warm-white">
      <AppHeader />
      <main className="mx-auto max-w-2xl px-5 py-6">
        <h1 className="font-display text-2xl font-semibold text-ink">Settings</h1>

        <Section title="Account">
          <dl className="space-y-1">
            <div className="flex justify-between">
              <dt>Plan</dt>
              <dd className="font-semibold text-ink">{account?.tier ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Memories this month</dt>
              <dd className="font-semibold text-ink">
                {account?.writes_used ?? "—"}
                {account?.writes_limit ? ` / ${account.writes_limit}` : ""}
              </dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt>Vault address</dt>
              <dd className="truncate font-mono text-xs text-ink">{smartAccount ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Chain</dt>
              <dd className="font-mono text-xs text-ink">{chainId ?? "—"}</dd>
            </div>
          </dl>
        </Section>

        <Section title="Security & recovery">
          <p>This device is unlocked with a passkey (Face ID / Touch ID / Windows Hello).</p>
          <p className="mt-2">
            Your recovery phrase is your <span className="font-semibold">only</span> guaranteed
            backup and is never stored — not on this device, not by us. Keep your written copy safe;
            you’ll need it to restore on a new device without a passkey backup.
          </p>
        </Section>

        <PasskeysAndBackupSection />

        <Section title="Export">
          <p>
            Download your decrypted memories. The exported file is{" "}
            <span className="font-semibold text-clay-deep">unencrypted</span> — store it somewhere
            safe.
          </p>
          <div className="mt-3 flex gap-2">
            <button
              onClick={() => confirmThenExport(items, "json")}
              disabled={items.length === 0}
              className="rounded-control bg-clay px-4 py-2 text-sm font-semibold text-warm-white hover:bg-clay-deep disabled:opacity-40"
            >
              Export .json
            </button>
            <button
              onClick={() => confirmThenExport(items, "md")}
              disabled={items.length === 0}
              className="rounded-control bg-surface px-4 py-2 text-sm font-semibold text-ink ring-1 ring-hairline hover:ring-clay disabled:opacity-40"
            >
              Export .md
            </button>
          </div>
        </Section>

        <Section title="Paired agents">
          <p>Pairing an agent (Hermes and others) from the web app is coming soon.</p>
        </Section>

        <section className="mt-6 rounded-card border border-clay/30 bg-surface p-5 shadow-soft">
          <h2 className="font-display text-lg font-semibold text-clay-deep">Danger zone</h2>
          <p className="mt-2 text-sm text-ink-muted">
            Forget this device — removes the encrypted keys from this browser only. Your on-chain
            vault is untouched.
          </p>
          <button
            onClick={onForget}
            disabled={busy}
            className="mt-3 rounded-control bg-clay-tint px-4 py-2 text-sm font-semibold text-clay-deep hover:bg-clay hover:text-warm-white disabled:opacity-50"
          >
            Forget this device
          </button>
        </section>
      </main>
    </div>
  );
}
