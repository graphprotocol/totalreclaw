import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import { clsx } from "clsx";
import { EntityChip } from "./EntityChip";
import { relativeDate, type SeedSession } from "./seed";
import { type Presentation } from "./presentation";

// Small ink-toned dot per memory type present in the session (a glance hint;
// the readable labels live on the Claim Cards inside the session detail).
const TYPE_DOT: Record<string, string> = {
  claim: "bg-type-claim-ink",
  preference: "bg-type-preference-ink",
  directive: "bg-type-directive-ink",
  commitment: "bg-type-commitment-ink",
  episode: "bg-type-episode-ink",
  summary: "bg-type-summary-ink",
};

interface Props {
  session: SeedSession;
  style?: CSSProperties;
  /** When provided, entity chips filter instead of linking to the mind-map. */
  onEntityClick?: (label: string) => void;
  /** When set, the card body links to this route (the entity chips stay separate). */
  href?: string;
  presentation?: Presentation;
}

/** Signature component: a session as a page from a journal, headlined by its Crystal. */
export function SessionCard({ session, style, onEntityClick, href, presentation = "type" }: Props) {
  const { crystal, facts, entities } = session;
  const typesPresent = Array.from(new Set(facts.map((f) => f.type)));
  const shownEntities = entities.slice(0, 3);
  const moreEntities = entities.length - shownEntities.length;

  const body = (
    <>
      <header className="mb-3 flex items-center justify-between gap-3">
        <time className="font-mono text-xs text-ink-muted">{relativeDate(session.date)}</time>
        {presentation !== "source" && (
          <div className="flex items-center gap-1.5" aria-hidden>
            {typesPresent.map((t) => (
              <span key={t} className={clsx("h-1.5 w-1.5 rounded-full", TYPE_DOT[t])} />
            ))}
          </div>
        )}
      </header>

      <p
        className="font-display text-[1.35rem] leading-snug text-ink"
        style={{ textWrap: "pretty" } as CSSProperties}
      >
        {crystal.narrative}
      </p>

      {crystal.keyOutcomes.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {crystal.keyOutcomes.slice(0, 2).map((outcome) => (
            <li key={outcome} className="flex gap-2 text-sm leading-snug text-ink-muted">
              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-clay" aria-hidden />
              <span>{outcome}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );

  return (
    <article
      className="animate-fade-up rounded-card bg-surface p-5 shadow-soft transition duration-200 ease-keeper hover:-translate-y-0.5 hover:shadow-raised"
      style={style}
    >
      {href ? (
        <Link
          to={href}
          className="block rounded-card focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
        >
          {body}
        </Link>
      ) : (
        body
      )}

      <footer className="mt-4 flex flex-wrap items-center gap-2">
        <span className="rounded-pill bg-warm-white px-2.5 py-1 font-mono text-xs text-ink-muted ring-1 ring-hairline">
          {facts.length} facts · {entities.length} entities · {crystal.openThreads.length} threads
        </span>
        {shownEntities.map((entity) => (
          <EntityChip key={entity} label={entity} onClick={onEntityClick} />
        ))}
        {moreEntities > 0 && <span className="text-xs text-ink-muted">+{moreEntities} more</span>}
      </footer>
    </article>
  );
}
