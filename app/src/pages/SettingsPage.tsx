import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useCrypto } from "../contexts/CryptoContext";
import { useVault } from "../hooks/useVault";
import { getAccount } from "../lib/api";
import { AppHeader } from "../components/AppHeader";
import { relativeDate } from "../lib/format";
import type { VaultItem } from "../lib/types";

function download(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function toJson(items: VaultItem[]): string {
  return JSON.stringify(
    items.map((i) => i.claim),
    null,
    2,
  );
}

function toMarkdown(items: VaultItem[]): string {
  const lines = ["# TotalReclaw vault export", ""];
  for (const i of items) {
    lines.push(`- **[${i.claim.type}]** ${i.claim.text}`);
    const meta = [i.claim.source, i.claim.scope, relativeDate(i.createdAt)].filter(Boolean);
    lines.push(`  - _${meta.join(" · ")}_`);
  }
  return lines.join("\n");
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-6 rounded-card bg-surface p-5 shadow-soft">
      <h2 className="font-display text-lg font-semibold text-ink">{title}</h2>
      <div className="mt-3 text-sm text-ink-muted">{children}</div>
    </section>
  );
}

export function SettingsPage() {
  const { keys, smartAccount, chainId, forgetDevice } = useCrypto();
  const navigate = useNavigate();
  const { data: items = [] } = useVault(keys);
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
            Your recovery phrase is your <span className="font-semibold">only</span> backup and is
            never stored — not on this device, not by us. Keep your written copy safe; you’ll need it
            to restore on a new device.
          </p>
        </Section>

        <Section title="Export">
          <p>
            Download your decrypted memories. The exported file is{" "}
            <span className="font-semibold text-clay-deep">unencrypted</span> — store it somewhere
            safe.
          </p>
          <div className="mt-3 flex gap-2">
            <button
              onClick={() => download("totalreclaw-vault.json", toJson(items), "application/json")}
              disabled={items.length === 0}
              className="rounded-control bg-clay px-4 py-2 text-sm font-semibold text-warm-white hover:bg-clay-deep disabled:opacity-40"
            >
              Export .json
            </button>
            <button
              onClick={() => download("totalreclaw-vault.md", toMarkdown(items), "text/markdown")}
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
