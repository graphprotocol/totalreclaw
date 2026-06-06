import { useState } from "react";
import { Link } from "react-router-dom";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";

// Mirrors docs/guides/importing-from-*.md. The SPA hosts the how-to; the agent
// runs the import (that's where the LLM lives). Steps/commands kept accurate.
interface Source {
  id: string;
  label: string;
  exportTitle: string;
  steps: string[];
  link: { label: string; href: string };
  command: string;
  extra: string;
  llm: boolean; // true → extraction sends chat text to the agent's LLM provider
}

const SOURCES: Source[] = [
  {
    id: "chatgpt",
    label: "ChatGPT",
    exportTitle: "Export from ChatGPT",
    steps: [
      "Open ChatGPT → profile icon → Settings",
      "Personalization → Memory → Manage",
      "Select all (⌘/Ctrl + A) and copy your saved memories",
    ],
    link: { label: "Open ChatGPT settings", href: "https://chatgpt.com" },
    command: "Import my ChatGPT memories into TotalReclaw",
    extra:
      "Want everything? Settings → Data Controls → Export data, then say “Import my ChatGPT conversations from <file>”. That fuller path runs your agent's LLM over the whole history.",
    llm: false,
  },
  {
    id: "gemini",
    label: "Gemini",
    exportTitle: "Export from Google Takeout",
    steps: [
      "Go to takeout.google.com → Deselect all",
      "Select Gemini Apps only → Create export",
      "Unzip the emailed link → find My Activity.html",
    ],
    link: { label: "Open Google Takeout", href: "https://takeout.google.com" },
    command: "Import my Gemini history from ~/Downloads/Takeout/.../My Activity.html",
    extra: "On OpenClaw + Hermes you can just drag the HTML file straight into the chat.",
    llm: true,
  },
  {
    id: "claude",
    label: "Claude",
    exportTitle: "Export from Claude",
    steps: [
      "Open Claude → profile icon → Settings",
      "Go to Memory",
      "Select all (⌘/Ctrl + A) and copy your memories",
    ],
    link: { label: "Open Claude settings", href: "https://claude.ai" },
    command: "Import my Claude memories into TotalReclaw",
    extra: "Claude memories are already curated — no LLM needed, they import cleanly.",
    llm: false,
  },
];

function StepNum({ n }: { n: number }) {
  return (
    <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-clay-tint font-mono text-xs font-semibold text-clay-deep">
      {n}
    </span>
  );
}

export function ImportGuide() {
  const [src, setSrc] = useState<Source>(SOURCES[0]);
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(src.command).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-28 pt-8">
        <Link to="/proto/timeline" className="text-sm font-semibold text-ink-muted transition hover:text-ink">
          ← Back
        </Link>

        <header className="mb-6 mt-3">
          <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">Bring your memories</h1>
          <p className="mt-1.5 max-w-prose text-sm leading-relaxed text-ink-muted">
            Already have history in another tool? Export it, then hand it to your paired agent — it does
            the import, because the model that reads your old chats lives there, not here. I'll show you how.
          </p>
        </header>

        {/* source picker */}
        <div className="mb-6 inline-flex rounded-pill p-1 ring-1 ring-hairline">
          {SOURCES.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => {
                setSrc(s);
                setCopied(false);
              }}
              aria-pressed={src.id === s.id}
              className={clsx(
                "rounded-pill px-4 py-1.5 text-sm font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-1",
                src.id === s.id ? "bg-clay text-warm-white shadow-soft" : "text-ink-muted hover:text-ink",
              )}
            >
              {s.label}
            </button>
          ))}
        </div>

        <div className="space-y-3">
          {/* Step 1 — export */}
          <section className="rounded-card bg-surface p-5 shadow-soft">
            <div className="flex items-center gap-3">
              <StepNum n={1} />
              <h2 className="font-display text-lg text-ink">{src.exportTitle}</h2>
            </div>
            <ol className="mt-3 space-y-2 pl-10">
              {src.steps.map((step, i) => (
                <li key={i} className="flex gap-2.5 text-sm text-ink">
                  <span className="font-mono text-xs text-ink-muted">{i + 1}.</span>
                  {step}
                </li>
              ))}
            </ol>
            <div className="mt-4 pl-10">
              <a
                href={src.link.href}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 rounded-control border border-hairline bg-warm-white px-3.5 py-2 text-sm font-semibold text-ink transition hover:border-ink-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
              >
                {src.link.label}
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <path d="M7 17 17 7M9 7h8v8" />
                </svg>
              </a>
            </div>
          </section>

          {/* Step 2 — hand to agent */}
          <section className="rounded-card bg-surface p-5 shadow-soft">
            <div className="flex items-center gap-3">
              <StepNum n={2} />
              <h2 className="font-display text-lg text-ink">Hand it to your agent</h2>
            </div>
            <div className="mt-3 pl-10">
              <p className="text-sm text-ink-muted">In your paired agent, say:</p>
              <div className="mt-2 flex items-stretch gap-2">
                <code className="flex-1 rounded-control border border-hairline bg-warm-white px-3.5 py-2.5 font-mono text-sm text-ink">
                  {src.command}
                </code>
                <button
                  type="button"
                  onClick={copy}
                  className="shrink-0 rounded-control bg-clay px-3.5 text-sm font-semibold text-warm-white shadow-soft transition duration-150 ease-keeper hover:bg-clay-deep focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
                >
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <p className="mt-2.5 text-xs text-ink-muted">{src.extra}</p>
              <p className="mt-2 text-xs text-ink-muted">
                No agent yet?{" "}
                <Link to="/proto/pair-agent" className="font-semibold text-clay-deep hover:underline">
                  Pair one first
                </Link>
                .
              </p>
            </div>
          </section>

          {/* Step 3 — review */}
          <section className="rounded-card bg-surface p-5 shadow-soft">
            <div className="flex items-center gap-3">
              <StepNum n={3} />
              <h2 className="font-display text-lg text-ink">Review what came in</h2>
            </div>
            <div className="mt-3 pl-10">
              <p className="text-sm text-ink">
                Imported memories land in your vault, deduplicated. I'll surface anything that looks
                duplicated, conflicting, or sensitive in{" "}
                <Link to="/proto/review" className="font-semibold text-clay-deep hover:underline">
                  Review
                </Link>
                .
              </p>
            </div>
          </section>
        </div>

        {/* privacy honesty */}
        <p className="mt-5 flex items-start gap-2 rounded-control bg-clay-tint/40 px-4 py-3 text-xs leading-relaxed text-ink-muted">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#A54B2E" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="mt-0.5 shrink-0" aria-hidden>
            <rect x="4" y="10" width="16" height="10" rx="2" />
            <path d="M8 10V7a4 4 0 0 1 8 0v3" />
          </svg>
          {src.llm
            ? "Full-history imports send your chat text to your agent's LLM provider in cleartext for extraction — your agent shows a privacy disclosure and asks first. TotalReclaw only ever stores the finished, encrypted memories."
            : "Curated-memory imports classify by pattern, no LLM needed. TotalReclaw only ever stores finished, encrypted memories — the vault never sees plaintext."}
        </p>
      </main>
    </div>
  );
}
