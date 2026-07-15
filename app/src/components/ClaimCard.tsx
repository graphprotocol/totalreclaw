import { useState, type CSSProperties } from "react";
import type { VaultItem, MemoryTypeV1 } from "../lib/types";
import { relativeDate } from "../lib/format";
import { agentProvenanceLabel } from "../lib/provenance";

const TYPE_TONE: Record<string, string> = {
  claim: "bg-type-claim text-type-claim-ink",
  preference: "bg-type-preference text-type-preference-ink",
  directive: "bg-type-directive text-type-directive-ink",
  commitment: "bg-type-commitment text-type-commitment-ink",
  episode: "bg-type-episode text-type-episode-ink",
  summary: "bg-type-summary text-type-summary-ink",
};

function sourceLabel(source: string): string {
  switch (source) {
    case "user":
      return "from you";
    case "user-inferred":
      return "inferred from you";
    case "assistant":
      return "from your agent";
    case "external":
      return "imported";
    case "derived":
      return "derived";
    default:
      return source || "unknown";
  }
}

/** A single memory, set to read. (Ported from the Keeper prototype ClaimCard.)
 *
 *  A.2 curation: when `onForget` is supplied, a subtle "Forget" affordance
 *  appears with an inline confirm; when `onTogglePin` is supplied, a "Pin"/
 *  "Unpin" affordance sits beside it (no confirm — pinning is reversible).
 *  Retype/set_scope land in A.2 Phase 3. With neither handler, the card is
 *  byte-for-byte the read-only card it was. */
export function ClaimCard({
  item,
  style,
  onForget,
  forgetPending = false,
  onTogglePin,
  pinPending = false,
}: {
  item: VaultItem;
  style?: CSSProperties;
  /** Opt-in delete: tombstone this memory on-chain. Absent → read-only card. */
  onForget?: () => void;
  /** True while the on-chain tombstone is in flight (awaiting the receipt). */
  forgetPending?: boolean;
  /** Opt-in pin/unpin: 2-call supersession on-chain. Absent → no affordance. */
  onTogglePin?: () => void;
  /** True while the pin/unpin supersession batch awaits its receipt. */
  pinPending?: boolean;
}) {
  const { claim } = item;
  const type = (claim.type as MemoryTypeV1) ?? "claim";
  const tone = TYPE_TONE[type] ?? TYPE_TONE.claim;
  // #317 — agent-instance provenance ("John (Hermes)"). Absent → undefined,
  // so the source/scope/date line below renders unchanged for most memories.
  const provenance = agentProvenanceLabel(claim);
  const [confirming, setConfirming] = useState(false);

  return (
    <article
      style={style}
      className={`animate-fade-up relative rounded-card p-4 shadow-soft transition duration-200 ease-keeper ${
        item.pinned ? "bg-clay-tint" : "bg-surface"
      }`}
    >
      {item.pinned && (
        <span
          className="absolute right-3 top-3 h-2 w-2 rounded-full bg-clay"
          aria-label="Pinned"
          title="Pinned"
        />
      )}
      <p
        className="font-display text-lg leading-snug text-ink"
        style={{ textWrap: "pretty" } as CSSProperties}
      >
        {claim.text}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className={`rounded-pill px-2.5 py-0.5 text-xs font-semibold ${tone}`}>{type}</span>
        <span className="text-xs text-ink-muted">
          {sourceLabel(claim.source)}
          {claim.scope && claim.scope !== "unspecified" ? ` · ${claim.scope}` : ""} ·{" "}
          {relativeDate(item.createdAt)}
        </span>
        {provenance && (
          <span className="text-xs text-ink-muted" title="Which agent instance recorded this">
            · via {provenance}
          </span>
        )}
      </div>
      {claim.reasoning && (
        <p className="mt-2 border-l-2 border-hairline pl-3 text-sm text-ink-muted">
          {claim.reasoning}
        </p>
      )}
      {(onForget || onTogglePin) && (
        <div className="mt-3 flex items-center justify-end gap-2 text-xs">
          {onTogglePin &&
            (pinPending ? (
              <span className="text-ink-muted" aria-live="polite">
                {item.pinned ? "Unpinning…" : "Pinning…"}
              </span>
            ) : (
              !confirming &&
              !forgetPending && (
                <button
                  type="button"
                  onClick={onTogglePin}
                  className="rounded-pill px-2.5 py-0.5 font-semibold text-ink-muted transition hover:bg-clay-tint hover:text-clay-deep focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
                >
                  {item.pinned ? "Unpin" : "Pin"}
                </button>
              )
            ))}
          {onForget && (forgetPending ? (
            <span className="text-ink-muted" aria-live="polite">
              Forgetting…
            </span>
          ) : confirming ? (
            <>
              <span className="text-ink-muted">Forget this memory?</span>
              <button
                type="button"
                onClick={() => {
                  setConfirming(false);
                  onForget();
                }}
                className="rounded-pill bg-clay px-2.5 py-0.5 font-semibold text-warm-white transition hover:bg-clay-deep focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
              >
                Forget
              </button>
              <button
                type="button"
                onClick={() => setConfirming(false)}
                className="rounded-pill px-2.5 py-0.5 font-semibold text-ink-muted transition hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setConfirming(true)}
              className="rounded-pill px-2.5 py-0.5 font-semibold text-ink-muted transition hover:bg-clay-tint hover:text-clay-deep focus:outline-none focus-visible:ring-2 focus-visible:ring-clay"
            >
              Forget
            </button>
          ))}
        </div>
      )}
    </article>
  );
}
