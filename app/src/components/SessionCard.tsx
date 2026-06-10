import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import { EntityChip } from "./EntityChip";
import { relativeDate, count } from "../lib/format";
import type { SessionGroup } from "../lib/vault/timeline";

interface Props {
  group: SessionGroup;
  href: string;
  style?: CSSProperties;
  /** When set, entity chips filter the timeline instead of being inert. */
  onEntityClick?: (label: string) => void;
}

/** A session as a page from a journal, headlined by its Crystal. (Ported from
 *  the Keeper prototype SessionCard; wired to real decrypted vault data.) */
export function SessionCard({ group, href, style, onEntityClick }: Props) {
  const outcomes = group.crystal?.claim.metadata?.key_outcomes ?? [];
  const shownEntities = group.entityNames.slice(0, 3);
  const moreEntities = group.entityNames.length - shownEntities.length;

  return (
    <article
      className="animate-fade-up rounded-card bg-surface p-5 shadow-soft transition duration-200 ease-keeper hover:-translate-y-0.5 hover:shadow-raised"
      style={style}
    >
      <Link
        to={href}
        className="block rounded-card focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
      >
        <time className="mb-3 block font-mono text-xs text-ink-muted">
          {relativeDate(group.date)}
        </time>
        <p
          className="font-display text-[1.35rem] leading-snug text-ink"
          style={{ textWrap: "pretty" } as CSSProperties}
        >
          {group.headline}
        </p>
        {outcomes.length > 0 && (
          <ul className="mt-3 space-y-1.5">
            {outcomes.slice(0, 2).map((outcome) => (
              <li key={outcome} className="flex gap-2 text-sm leading-snug text-ink-muted">
                <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-clay" aria-hidden />
                <span>{outcome}</span>
              </li>
            ))}
          </ul>
        )}
      </Link>

      <footer className="mt-4 flex flex-wrap items-center gap-2">
        <span className="rounded-pill bg-warm-white px-2.5 py-1 font-mono text-xs text-ink-muted ring-1 ring-hairline">
          {count(group.facts.length, "fact")} ·{" "}
          {count(group.entityNames.length, "entity", "entities")} ·{" "}
          {count(group.openThreads, "thread")}
        </span>
        {shownEntities.map((entity) => (
          <EntityChip key={entity} label={entity} onClick={onEntityClick} />
        ))}
        {moreEntities > 0 && <span className="text-xs text-ink-muted">+{moreEntities} more</span>}
      </footer>
    </article>
  );
}
