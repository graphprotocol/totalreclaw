import { useEffect } from "react";
import { clsx } from "clsx";
import {
  agentBreakdown,
  importSourceOf,
  sourceBreakdown,
  type SessionGroup,
} from "../../lib/vault/timeline";
import type { VaultItem } from "../../lib/types";
import { ClaimCard } from "../ClaimCard";
import { EntityChip } from "../EntityChip";
import { relativeDate, count } from "../../lib/format";

/** What the shared drawer is showing — a session Crystal+facts, or everything
 *  about one entity. Mirrors the proto's PanelView union, on real vault data. */
export type PanelView =
  | { kind: "session"; group: SessionGroup }
  | { kind: "entity"; name: string };


function CrystalList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <section className="mt-5">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">{title}</h3>
      <ul className="mt-2 space-y-1.5">
        {items.map((it) => (
          <li key={it} className="flex gap-2 text-sm leading-snug text-ink">
            <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-clay" aria-hidden />
            <span>{it}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

interface Props {
  view: PanelView | null;
  onClose: () => void;
  onOpenSession: (g: SessionGroup) => void;
  onOpenEntity: (name: string) => void;
  /** All session groups (for the entity view's "mentioned in" list). */
  groups: SessionGroup[];
  /** All decrypted items (for the entity view's fact-level list). */
  items: VaultItem[];
}

export function SidePanel({ view, onClose, onOpenSession, onOpenEntity, groups, items }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const open = !!view;
  const group = view?.kind === "session" ? view.group : null;
  const entity = view?.kind === "entity" ? view.name : null;

  const lc = entity?.toLowerCase() ?? null;
  const entitySessions = lc
    ? groups.filter((g) => g.entityNames.some((e) => e.toLowerCase() === lc))
    : [];
  const entityFacts = lc
    ? items
        .filter((i) => (i.claim.entities ?? []).some((e) => e.name.toLowerCase() === lc))
        .sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime())
    : [];

  const crystalMeta = group?.crystal?.claim.metadata;
  const headline = group?.crystal?.claim.text ?? group?.headline ?? "";

  return (
    <>
      <div
        onClick={onClose}
        aria-hidden
        className={clsx(
          "fixed inset-0 z-40 bg-ink/20 transition-opacity duration-200 ease-keeper motion-reduce:transition-none",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={group ? "Session detail" : entity ? `About ${entity}` : undefined}
        className={clsx(
          "fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col bg-warm-white shadow-overlay transition-transform duration-200 ease-keeper motion-reduce:transition-none",
          open ? "translate-x-0" : "translate-x-full",
        )}
      >
        {view && (
          <div className="flex items-center justify-between border-b border-hairline px-5 py-3">
            <span className="font-mono text-xs text-ink-muted">
              {group ? relativeDate(group.date) : "Entity"}
            </span>
            <button
              onClick={onClose}
              aria-label="Close"
              className="rounded-pill p-1.5 text-ink-muted transition hover:bg-clay-tint hover:text-clay-deep focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
                <path d="M18 6 6 18M6 6l12 12" />
              </svg>
            </button>
          </div>
        )}

        {/* Entity view — everything about one entity. */}
        {entity && (
          <div className="flex-1 overflow-y-auto px-5 py-5">
            <h2 className="font-display text-2xl leading-snug text-ink" style={{ textWrap: "pretty" }}>
              {entity}
            </h2>
            <p className="mt-1 text-sm text-ink-muted">
              {entityFacts.length > 0
                ? `${count(entityFacts.length, "memory", "memories")} · ${count(entitySessions.length, "session")}`
                : "Nothing references this yet."}
            </p>

            {entitySessions.length > 0 && (
              <>
                <h3 className="mt-6 text-xs font-semibold uppercase tracking-wide text-ink-muted">
                  Mentioned in
                </h3>
                <div className="mt-2.5 space-y-2.5">
                  {entitySessions.map((s) => (
                    <button
                      key={s.key}
                      onClick={() => onOpenSession(s)}
                      className="block w-full rounded-card bg-surface p-3.5 text-left shadow-soft transition duration-200 ease-keeper hover:-translate-y-0.5 hover:shadow-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
                    >
                      <time className="font-mono text-xs text-ink-muted">{relativeDate(s.date)}</time>
                      <p className="mt-1 font-display text-[0.95rem] leading-snug text-ink" style={{ textWrap: "pretty" }}>
                        {s.headline}
                      </p>
                    </button>
                  ))}
                </div>
              </>
            )}

            {entityFacts.length > 0 && (
              <>
                <h3 className="mt-7 text-xs font-semibold uppercase tracking-wide text-ink-muted">
                  Memories mentioning {entity}
                </h3>
                <div className="mt-3 space-y-3">
                  {entityFacts.map((f) => (
                    <ClaimCard key={f.id} item={f} />
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {/* Session view — the Crystal + its facts. */}
        {group && (
          <div className="flex-1 overflow-y-auto px-5 py-5">
            {importSourceOf(group) && (
              <span className="mb-3 inline-flex items-center gap-1 rounded-pill bg-clay-tint px-2.5 py-0.5 text-xs font-semibold text-clay-deep">
                Imported · {importSourceOf(group)}
              </span>
            )}
            <h2 className="font-display text-xl leading-snug text-ink" style={{ textWrap: "pretty" }}>
              {headline}
            </h2>
            {/* Honest provenance — shows the real source mix, so a session that
                blends origins is visible rather than silently blanket-labelled. */}
            <p className="mt-2 text-xs text-ink-muted">
              {sourceBreakdown(group)
                .map((s) => `${s.n} ${s.label}`)
                .join(" · ")}
            </p>
            {/* #317 — agent-instance provenance. Only rendered when a member
                carries an agent_name; otherwise this block is absent and the
                panel looks exactly as before. */}
            {agentBreakdown(group).length > 0 && (
              <p className="mt-1 text-xs text-ink-muted">
                {agentBreakdown(group)
                  .map((a) => `${a.n} via ${a.label}`)
                  .join(" · ")}
              </p>
            )}
            {crystalMeta && (
              <>
                <CrystalList title="Key outcomes" items={crystalMeta.key_outcomes ?? []} />
                <CrystalList title="Open threads" items={crystalMeta.open_threads ?? []} />
                <CrystalList title="Lessons" items={crystalMeta.lessons ?? []} />
              </>
            )}

            {group.entityNames.length > 0 && (
              <div className="mt-5 flex flex-wrap gap-1.5">
                {group.entityNames.map((e) => (
                  <EntityChip key={e} label={e} onClick={onOpenEntity} />
                ))}
              </div>
            )}

            <h3 className="mt-7 text-xs font-semibold uppercase tracking-wide text-ink-muted">
              {group.facts.length > 0 ? "Memories from this session" : "No atomic memories"}
            </h3>
            <div className="mt-3 space-y-3">
              {group.facts.map((f) => (
                <ClaimCard key={f.id} item={f} />
              ))}
            </div>
          </div>
        )}
      </aside>
    </>
  );
}
