import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";
import { ClaimCard } from "./ClaimCard";
import { EntityChip } from "./EntityChip";
import { UndoToast } from "./UndoToast";
import { SEED_SESSIONS, relativeDate, type SeedFact } from "./seed";
import type { MemoryTypeV1 } from "../lib/types";

function Section({ label, items, dot }: { label: string; items: string[]; dot: string }) {
  return (
    <div className="mt-4">
      <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-muted">{label}</div>
      <ul className="space-y-1.5">
        {items.map((t) => (
          <li key={t} className="flex gap-2.5 text-[0.95rem] leading-snug text-ink">
            <span className={clsx("mt-2 shrink-0 rounded-full", dot)} aria-hidden />
            <span>{t}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function SessionDetailView() {
  const { id } = useParams();
  const session = useMemo(
    () => SEED_SESSIONS.find((s) => s.id === id) ?? SEED_SESSIONS[0],
    [id],
  );
  const [facts, setFacts] = useState<SeedFact[]>(() => session.facts.map((f) => ({ ...f })));
  const [deleted, setDeleted] = useState<{ fact: SeedFact; index: number } | null>(null);

  const pin = (fid: string) =>
    setFacts((fs) => fs.map((f) => (f.id === fid ? { ...f, pinned: !f.pinned } : f)));
  const retype = (fid: string, t: MemoryTypeV1) =>
    setFacts((fs) => fs.map((f) => (f.id === fid ? { ...f, type: t } : f)));
  const del = (fid: string) =>
    setFacts((fs) => {
      const index = fs.findIndex((f) => f.id === fid);
      if (index >= 0) setDeleted({ fact: fs[index], index });
      return fs.filter((f) => f.id !== fid);
    });
  const undo = () => {
    if (!deleted) return;
    setFacts((fs) => {
      const next = [...fs];
      next.splice(deleted.index, 0, deleted.fact);
      return next;
    });
    setDeleted(null);
  };

  const c = session.crystal;

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-28 pt-6">
        <Link
          to="/proto/timeline"
          className="inline-flex items-center gap-1 text-sm font-semibold text-ink-muted transition hover:text-ink"
        >
          ← Timeline
        </Link>

        <div className="animate-fade-up mt-4">
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
            <span className="h-1.5 w-1.5 rounded-full bg-clay" aria-hidden /> Crystal ·{" "}
            {relativeDate(session.date)}
          </div>
          <h1
            className="font-display text-[1.7rem] leading-tight text-ink"
            style={{ textWrap: "balance" }}
          >
            {c.narrative}
          </h1>

          {session.importSource && (
            <span className="mt-2.5 inline-flex items-center gap-1.5 rounded-pill bg-clay-tint px-2.5 py-1 text-xs font-semibold text-clay-deep">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M12 3v12M7 10l5 5 5-5M5 21h14" />
              </svg>
              Imported from {session.importSource}
            </span>
          )}

          {c.keyOutcomes.length > 0 && (
            <Section label="Key outcomes" items={c.keyOutcomes} dot="h-1.5 w-1.5 bg-clay" />
          )}
          {c.openThreads.length > 0 && (
            <Section label="Open threads" items={c.openThreads} dot="h-2 w-2 border border-clay" />
          )}
          {c.lessons && c.lessons.length > 0 && (
            <Section label="Lessons" items={c.lessons} dot="h-1.5 w-1.5 bg-ink-muted" />
          )}
        </div>

        <div className="mt-8">
          <h2 className="mb-3 font-display text-xl text-ink">
            {facts.length} {facts.length === 1 ? "memory" : "memories"}
          </h2>
          <div className="space-y-3">
            {facts.map((f, i) => (
              <ClaimCard
                key={f.id}
                fact={f}
                presentation="source"
                style={{ animationDelay: `${i * 60}ms` }}
                onPin={() => pin(f.id)}
                onRetype={(t) => retype(f.id, t)}
                onDelete={() => del(f.id)}
              />
            ))}
          </div>
        </div>

        {session.entities.length > 0 && (
          <div className="mt-8">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
              Entities
            </div>
            <div className="flex flex-wrap gap-2">
              {session.entities.map((e) => (
                <EntityChip key={e} label={e} />
              ))}
            </div>
          </div>
        )}
      </main>

      {deleted && (
        <UndoToast label="Memory deleted" onUndo={undo} onExpire={() => setDeleted(null)} />
      )}
    </div>
  );
}
