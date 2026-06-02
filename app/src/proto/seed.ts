// Prototype seed data for look-and-feel work on the warm "Keeper" direction.
// NOT wired to the real vault / crypto / relay — pure fixtures so the timeline
// and mind-map screens render without auth. Safe to delete with the branch.
import type { MemoryTypeV1 } from "../lib/types";

export interface SeedFact {
  id: string;
  text: string;
  type: MemoryTypeV1;
  source: string;
  scope: string;
  pinned?: boolean;
}

export interface SeedSession {
  id: string;
  date: string; // YYYY-MM-DD
  hash8: string;
  crystal: {
    narrative: string;
    keyOutcomes: string[];
    openThreads: string[];
    lessons?: string[];
  };
  facts: SeedFact[];
  entities: string[];
}

export const SEED_SESSIONS: SeedSession[] = [
  {
    id: "s1",
    date: "2026-05-31",
    hash8: "a3f10b9c",
    crystal: {
      narrative:
        "Designed the warm, personal direction for the TotalReclaw web vault and chose a clay-and-warm-white palette.",
      keyOutcomes: [
        "Locked the session timeline as the landing surface, Crystal as each card's headline.",
        "Picked Fraunces for memory text, Figtree for the interface.",
      ],
      openThreads: ["Decide whether the mind-map ships behind a flag if kg-3 slips."],
      lessons: ["Restraint earns trust faster than decoration on a privacy product."],
    },
    entities: ["Vault SPA", "Crystal", "mind-map", "Fraunces"],
    facts: [
      { id: "f1", text: "Prefers the interface to feel like a personal journal, not an admin console.", type: "preference", source: "user", scope: "work" },
      { id: "f2", text: "Chose terracotta/clay as the single brand accent because it reads warm without being loud.", type: "claim", source: "user", scope: "work" },
      { id: "f3", text: "Hide all chain and wallet language from the user in the vault UI.", type: "directive", source: "user", scope: "work", pinned: true },
      { id: "f4", text: "Will prototype the timeline and mind-map screens first to test the feel.", type: "commitment", source: "user", scope: "work" },
    ],
  },
  {
    id: "s2",
    date: "2026-05-28",
    hash8: "7c20e441",
    crystal: {
      narrative:
        "Reviewed The Graph Foundation roadmap and aligned the memory product bets for the next quarter.",
      keyOutcomes: [
        "Hermes auth-hardening stays the top priority; OpenClaw work parked.",
        "Imports (Gemini, ChatGPT) confirmed as the second bet.",
      ],
      openThreads: ["Confirm the passkey recovery story before the SPA ships.", "Schedule the subgraph v0.7 review."],
    },
    entities: ["The Graph", "Hermes", "subgraph", "Gnosis"],
    facts: [
      { id: "f5", text: "Works at The Graph Foundation; email is pedro@thegraph.foundation.", type: "claim", source: "user", scope: "work" },
      { id: "f6", text: "Decided to focus the memory product on Hermes and park the other clients.", type: "claim", source: "user", scope: "work", pinned: true },
      { id: "f7", text: "All managed-service tiers route to Gnosis mainnet under the single-chain policy.", type: "claim", source: "assistant", scope: "work" },
      { id: "f8", text: "Quarterly roadmap: auth-hardening, imports, then the web vault.", type: "summary", source: "derived", scope: "work" },
    ],
  },
  {
    id: "s3",
    date: "2026-05-24",
    hash8: "11d9aa02",
    crystal: {
      narrative:
        "Talked through a return-to-running plan after the knee niggle, keeping mileage gentle for two weeks.",
      keyOutcomes: ["Capped weekly mileage at 25km until the knee settles."],
      openThreads: ["Book the physio follow-up."],
      lessons: ["A short easy block now beats a long forced layoff later."],
    },
    entities: ["running", "knee", "sleep"],
    facts: [
      { id: "f9", text: "Recovering from a mild knee niggle; keeping runs easy for two weeks.", type: "episode", source: "user", scope: "health" },
      { id: "f10", text: "Prefers morning runs before the first meeting.", type: "preference", source: "user", scope: "health" },
      { id: "f11", text: "Sleeps better on days with a morning run.", type: "claim", source: "user-inferred", scope: "health" },
    ],
  },
  {
    id: "s4",
    date: "2026-05-20",
    hash8: "5e88b73a",
    crystal: {
      narrative:
        "Planned the family trip to Lisbon in July and noted a few things to sort before booking.",
      keyOutcomes: ["Settled on the first two weeks of July for the trip."],
      openThreads: ["Find a place near the grandparents.", "Check passport expiry for the kids."],
    },
    entities: ["Lisbon", "family", "July trip"],
    facts: [
      { id: "f12", text: "Planning a family trip to Lisbon in early July.", type: "episode", source: "user", scope: "family" },
      { id: "f13", text: "Wants to stay close to the grandparents' place.", type: "preference", source: "user", scope: "family" },
      { id: "f14", text: "Book flights once the kids' passports are confirmed.", type: "commitment", source: "user", scope: "family" },
    ],
  },
  {
    id: "s5",
    date: "2026-05-12",
    hash8: "9b40f1d7",
    crystal: {
      narrative:
        "Started an essay on memory and forgetting, drawing on Borges, and sketched the opening.",
      keyOutcomes: ["Drafted the opening around 'Funes the Memorious'."],
      openThreads: ["Decide if the essay is personal or argumentative."],
    },
    entities: ["Borges", "essay", "writing"],
    facts: [
      { id: "f15", text: "Writing an essay on memory and forgetting.", type: "episode", source: "user", scope: "creative" },
      { id: "f16", text: "Admires Borges' 'Funes the Memorious' as a frame for the piece.", type: "preference", source: "user", scope: "creative" },
    ],
  },
];

export interface TopicNode {
  id: string;
  label: string;
  count: number;
  entities: string[];
}

export const TOPIC_TREE: TopicNode[] = [
  { id: "t1", label: "TotalReclaw", count: 14, entities: ["Vault SPA", "Crystal", "mind-map", "Fraunces", "relay"] },
  { id: "t2", label: "The Graph", count: 9, entities: ["subgraph", "Gnosis", "Hermes", "foundation"] },
  { id: "t3", label: "Health & running", count: 6, entities: ["running", "knee", "sleep"] },
  { id: "t4", label: "Family", count: 5, entities: ["Lisbon", "July trip", "grandparents"] },
  { id: "t5", label: "Reading & writing", count: 4, entities: ["Borges", "essay", "memory"] },
];

export interface GraphNode {
  id: string;
  label: string;
  x: number;
  y: number;
  hub?: boolean;
}

export const GRAPH_NODES: GraphNode[] = [
  { id: "n1", label: "TotalReclaw", x: 160, y: 110, hub: true },
  { id: "n2", label: "Crystal", x: 64, y: 56 },
  { id: "n3", label: "mind-map", x: 252, y: 58 },
  { id: "n4", label: "Hermes", x: 248, y: 158 },
  { id: "n5", label: "The Graph", x: 70, y: 168 },
  { id: "n6", label: "subgraph", x: 150, y: 196 },
  { id: "n7", label: "Gnosis", x: 290, y: 110 },
];

export const GRAPH_EDGES: Array<[string, string]> = [
  ["n1", "n2"],
  ["n1", "n3"],
  ["n1", "n4"],
  ["n1", "n5"],
  ["n4", "n5"],
  ["n5", "n6"],
  ["n4", "n7"],
];

export function relativeDate(dateStr: string): string {
  const then = new Date(`${dateStr}T12:00:00Z`).getTime();
  const days = Math.round((Date.now() - then) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  if (days < 14) return "Last week";
  if (days < 60) return `${Math.round(days / 7)} weeks ago`;
  return `${Math.round(days / 30)} months ago`;
}
