import { Link } from "react-router-dom";
import { ProtoHeader } from "./ProtoHeader";

// Non-functional settings/account corner. Also the home for the "export / portability"
// value prop and for managing paired agents (named-instance provenance, see #317).

function Section({
  title,
  desc,
  children,
}: {
  title: string;
  desc?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-card bg-surface p-5 shadow-soft">
      <h2 className="font-display text-xl text-ink">{title}</h2>
      {desc && <p className="mt-1 text-sm leading-relaxed text-ink-muted">{desc}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function GhostBtn({ children, tone = "ghost" }: { children: React.ReactNode; tone?: "ghost" | "danger" }) {
  return (
    <button
      type="button"
      className={
        tone === "danger"
          ? "rounded-control border border-clay/40 bg-warm-white px-3.5 py-2 text-sm font-semibold text-clay-deep transition hover:bg-clay-tint focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
          : "rounded-control border border-hairline bg-warm-white px-3.5 py-2 text-sm font-semibold text-ink transition hover:border-ink-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
      }
    >
      {children}
    </button>
  );
}

const AGENTS = [
  { name: "John", client: "Hermes", note: "this Mac · active" },
  { name: "Claude Desktop", client: "MCP", note: "active" },
];

export function ProtoSettings() {
  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-28 pt-8">
        <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">Settings</h1>
        <p className="mt-1.5 text-sm text-ink-muted">Your vault, your keys, your agents. All on your terms.</p>

        <div className="mt-6 space-y-4">
          <Section title="Account">
            <dl className="space-y-3 text-sm">
              <div className="flex items-center justify-between gap-3">
                <dt className="text-ink-muted">Vault</dt>
                <dd className="font-mono text-ink">0x2c0C…8250</dd>
              </div>
              <div className="flex items-center justify-between gap-3">
                <dt className="text-ink-muted">Plan</dt>
                <dd className="flex items-center gap-2">
                  <span className="rounded-pill bg-clay-tint px-2.5 py-0.5 text-xs font-semibold text-clay-deep">Free</span>
                  <GhostBtn>Upgrade to Pro</GhostBtn>
                </dd>
              </div>
              <div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-ink-muted">This month</dt>
                  <dd className="text-ink">142 of 250 memories</dd>
                </div>
                <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-pill bg-hairline">
                  <div className="h-full rounded-pill bg-clay" style={{ width: "57%" }} />
                </div>
              </div>
            </dl>
          </Section>

          <Section title="Security & recovery" desc="Only you can unlock this vault. We never see your keys.">
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-ink">Passkey on this device</p>
                  <p className="text-xs text-ink-muted">Face ID / Touch ID · added today</p>
                </div>
                <GhostBtn>Add another device</GhostBtn>
              </div>
              <div className="flex items-center justify-between gap-3 border-t border-hairline pt-3">
                <div>
                  <p className="text-sm font-semibold text-ink">Recovery phrase</p>
                  <p className="text-xs text-ink-muted">Your last-resort backup. We'll ask for your passkey first.</p>
                </div>
                <GhostBtn>Reveal phrase</GhostBtn>
              </div>
            </div>
          </Section>

          <Section title="Paired agents" desc="The agents that can read and add memories. Each gets a scoped key — never your phrase.">
            <div className="space-y-2.5">
              {AGENTS.map((a) => (
                <div key={a.name} className="flex items-center justify-between gap-3 rounded-control border border-hairline bg-warm-white px-3.5 py-2.5">
                  <div>
                    <p className="text-sm font-semibold text-ink">
                      {a.name} <span className="font-normal text-ink-muted">({a.client})</span>
                    </p>
                    <p className="text-xs text-ink-muted">{a.note}</p>
                  </div>
                  <button
                    type="button"
                    className="text-sm font-semibold text-clay-deep transition hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
                  >
                    Revoke
                  </button>
                </div>
              ))}
              <Link
                to="/proto/pair-agent"
                className="inline-flex items-center gap-1.5 text-sm font-semibold text-clay-deep transition hover:underline"
              >
                + Pair another agent
              </Link>
            </div>
          </Section>

          <Section title="Your data" desc="Portable by design. No lock-in, ever.">
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <GhostBtn>Export memories (.json)</GhostBtn>
                <GhostBtn>Export memories (.md)</GhostBtn>
                <Link
                  to="/proto/import"
                  className="rounded-control border border-hairline bg-warm-white px-3.5 py-2 text-sm font-semibold text-ink transition hover:border-ink-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
                >
                  Import from another tool
                </Link>
              </div>
              <p className="flex items-start gap-2 rounded-control bg-clay-tint/40 px-3.5 py-2.5 text-xs leading-relaxed text-ink-muted">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#A54B2E" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="mt-0.5 shrink-0" aria-hidden>
                  <path d="M12 9v4M12 17h.01M3.6 18l7-13a1.6 1.6 0 0 1 2.8 0l7 13A1.6 1.6 0 0 1 21 20H4a1.6 1.6 0 0 1-1.4-2Z" />
                </svg>
                An export is decrypted on this device — the file is <strong className="font-semibold">unencrypted</strong>. Anyone who opens it can read your memories. Keep it somewhere safe.
              </p>
            </div>
          </Section>

          <Section title="Danger zone" desc="This can't be undone.">
            <GhostBtn tone="danger">Delete this vault and everything in it</GhostBtn>
          </Section>
        </div>
      </main>
    </div>
  );
}
