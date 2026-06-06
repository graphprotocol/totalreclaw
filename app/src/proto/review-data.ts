// Seed for the "Review" surface (memory health / Watchtower). Pure fixtures — no
// engine wiring. Each kind maps to a real-or-near-real backend primitive; see the
// `backed` note for honesty about what's shipped vs needs plumbing.
//   conflict  -> contradiction detection (BUILT, but not yet persisted as queryable)
//   stale     -> volatility + createdAt (BUILT; auto-decay ranking not applied yet)
//   changed   -> superseded_by chain in the encrypted blob (BUILT, client traverses)
//   secret    -> secrets.rs 14 detectors (BUILT in core; surfacing needs wiring)

export type ReviewKind = "conflict" | "stale" | "changed" | "secret";

export interface ReviewBase {
  id: string;
  kind: ReviewKind;
  /** Honesty tag rendered in the prototype footer of each card. */
  backed: "shipped" | "needs-plumbing" | "needs-backend";
}

export interface ConflictItem extends ReviewBase {
  kind: "conflict";
  /** Two claims the agent holds that disagree. */
  a: { text: string; age: string; source: string; pinned?: boolean };
  b: { text: string; age: string; source: string; pinned?: boolean };
  /** Lineage thread this conflict belongs to. */
  thread: string;
}

export interface StaleItem extends ReviewBase {
  kind: "stale";
  text: string;
  age: string;
  scope: string;
}

export interface ChangedItem extends ReviewBase {
  kind: "changed";
  /** Human changelog line, Keeper voice. */
  summary: string;
  from: string;
  to: string;
  age: string;
  thread: string;
}

export interface SecretItem extends ReviewBase {
  kind: "secret";
  label: string; // e.g. "Anthropic API key"
  context: string; // where it was caught
}

export type ReviewItem = ConflictItem | StaleItem | ChangedItem | SecretItem;

export const REVIEW_ITEMS: ReviewItem[] = [
  {
    id: "r1",
    kind: "conflict",
    backed: "needs-backend",
    thread: "where-pedro-works",
    a: {
      text: "Pedro works at The Graph Foundation.",
      age: "3 weeks ago",
      source: "user",
      pinned: true,
    },
    b: {
      text: "Pedro is between jobs right now.",
      age: "2 days ago",
      source: "assistant",
    },
  },
  {
    id: "r2",
    kind: "stale",
    backed: "shipped",
    text: "You live in Lisbon.",
    age: "7 months ago",
    scope: "personal",
  },
  {
    id: "r3",
    kind: "changed",
    backed: "shipped",
    summary: "I moved your July trip from Lisbon to Porto.",
    from: "Family trip to Lisbon in early July",
    to: "Family trip to Porto in early July",
    age: "Last week",
    thread: "july-trip",
  },
  {
    id: "r4",
    kind: "secret",
    backed: "needs-plumbing",
    label: "Anthropic API key",
    context: "caught mid-session and vaulted, hidden from agents by default",
  },
  {
    id: "r5",
    kind: "stale",
    backed: "shipped",
    text: "You're keeping runs easy for two weeks while the knee settles.",
    age: "Almost two weeks ago",
    scope: "health",
  },
];

export const BACKED_LABEL: Record<ReviewBase["backed"], string> = {
  shipped: "Backed by shipped engine data",
  "needs-plumbing": "Detector shipped, surfacing not wired yet",
  "needs-backend": "Needs backend: persist unresolved conflicts",
};
