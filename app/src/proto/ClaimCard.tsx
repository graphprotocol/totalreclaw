import { useState, type CSSProperties } from "react";
import { clsx } from "clsx";
import { TypeBadge } from "../components/TypeBadge";
import { MEMORY_TYPES_V1, type MemoryTypeV1 } from "../lib/types";
import { sourceLabel, typeBucket, type Presentation } from "./presentation";
import type { SeedFact } from "./seed";

const BUCKET_TONE: Record<string, string> = {
  rule: "bg-clay-tint text-clay-deep",
  todo: "bg-type-commitment text-type-commitment-ink",
  pref: "bg-type-preference text-type-preference-ink",
};

interface Props {
  fact: SeedFact;
  presentation?: Presentation;
  style?: CSSProperties;
  onPin: () => void;
  onRetype: (t: MemoryTypeV1) => void;
  onDelete: () => void;
}

/** Atomic memory inside a session: serif text, in-place curation. Header leads with
 *  type (taxonomy view) or provenance + a sparse plain-language badge (source view). */
export function ClaimCard({ fact, presentation = "type", style, onPin, onRetype, onDelete }: Props) {
  const [menu, setMenu] = useState(false);
  const [retyping, setRetyping] = useState(false);
  const close = () => {
    setMenu(false);
    setRetyping(false);
  };

  const sourceView = presentation === "source";
  const bucket = sourceView ? typeBucket(fact.type) : null;

  return (
    <article
      style={style}
      className={clsx(
        "animate-fade-up relative rounded-card p-4 shadow-soft transition duration-200 ease-keeper",
        fact.pinned ? "bg-clay-tint" : "bg-surface",
      )}
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {sourceView ? (
            <>
              <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-ink-muted">
                <span
                  className={clsx(
                    "h-1.5 w-1.5 rounded-full",
                    fact.source === "user" ? "bg-clay" : "bg-ink-muted",
                  )}
                  aria-hidden
                />
                {sourceLabel(fact.source)}
              </span>
              {bucket && (
                <span
                  className={clsx(
                    "rounded-pill px-2 py-0.5 text-xs font-semibold",
                    BUCKET_TONE[bucket.tone],
                  )}
                >
                  {bucket.label}
                </span>
              )}
            </>
          ) : (
            <TypeBadge type={fact.type} />
          )}
        </div>

        <div className="flex items-center gap-0.5">
          <button
            type="button"
            onClick={onPin}
            aria-label={fact.pinned ? "Unpin memory" : "Pin memory"}
            className={clsx(
              "rounded-control p-1.5 transition active:scale-90",
              fact.pinned ? "text-clay" : "text-ink-muted hover:bg-warm-white hover:text-ink",
            )}
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill={fact.pinned ? "currentColor" : "none"}
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M12 17v5M9 10.76V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v6.76a2 2 0 0 0 .5 1.32l1.7 1.92A1 1 0 0 1 17.5 16h-11a1 1 0 0 1-.7-1.99l1.7-1.93A2 2 0 0 0 9 10.76Z" />
            </svg>
          </button>
          <button
            type="button"
            onClick={() => (menu ? close() : setMenu(true))}
            aria-label="More actions"
            className="rounded-control p-1.5 text-ink-muted transition hover:bg-warm-white hover:text-ink"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
              <circle cx="5" cy="12" r="1.8" />
              <circle cx="12" cy="12" r="1.8" />
              <circle cx="19" cy="12" r="1.8" />
            </svg>
          </button>
        </div>
      </div>

      <p className="font-display text-[1.05rem] leading-snug text-ink">{fact.text}</p>

      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 font-sans text-xs text-ink-muted">
        {sourceView ? (
          <span>{fact.scope}</span>
        ) : (
          <>
            <span>from {fact.source}</span>
            <span aria-hidden>·</span>
            <span>{fact.scope}</span>
          </>
        )}
        {fact.pinned && (
          <>
            <span aria-hidden>·</span>
            <span className="font-semibold text-clay-deep">pinned</span>
          </>
        )}
      </div>

      {menu && (
        <>
          <button
            type="button"
            aria-hidden
            tabIndex={-1}
            onClick={close}
            className="fixed inset-0 z-0 cursor-default"
          />
          <div className="absolute right-3 top-12 z-10 w-44 overflow-hidden rounded-control border border-hairline bg-surface py-1 shadow-overlay">
            {!retyping ? (
              <>
                <button
                  type="button"
                  onClick={() => setRetyping(true)}
                  className="block w-full px-3 py-2 text-left text-sm text-ink transition hover:bg-warm-white"
                >
                  Change type…
                </button>
                <button
                  type="button"
                  onClick={() => {
                    onDelete();
                    close();
                  }}
                  className="block w-full px-3 py-2 text-left text-sm text-clay-deep transition hover:bg-clay-tint"
                >
                  Delete
                </button>
              </>
            ) : (
              MEMORY_TYPES_V1.map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => {
                    onRetype(t);
                    close();
                  }}
                  className={clsx(
                    "block w-full px-3 py-1.5 text-left text-sm transition hover:bg-warm-white",
                    t === fact.type ? "font-semibold text-clay-deep" : "text-ink",
                  )}
                >
                  {t}
                </button>
              ))
            )}
          </div>
        </>
      )}
    </article>
  );
}
