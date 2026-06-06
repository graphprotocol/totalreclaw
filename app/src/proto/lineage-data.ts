// Seed for the "Lineage" lens — one belief's evolution as a directed, typed thread.
// This is the ONLY place a graph appears, and it earns its keep: edges are typed
// (supersedes / contradicts / derived-from) and scope is a single thread, so it
// never becomes a hairball. Backed by superseded_by + pin_status + contradiction
// records the client walks after decrypting the vault.

export type EdgeType = "supersedes" | "contradicts" | "derived-from";

export interface LineageNode {
  id: string;
  text: string;
  age: string;
  source: string;
  state: "current" | "past" | "rival";
  pinned?: boolean;
  /** Relationship from the node ABOVE to this one. */
  edgeFromPrev?: EdgeType;
}

export interface LineageThread {
  id: string;
  title: string;
  question: string; // the human question this thread answers
  nodes: LineageNode[];
  /** Unresolved contradiction sitting on the thread, if any. */
  conflict?: { topId: string; bottomId: string };
}

export const LINEAGE_THREADS: Record<string, LineageThread> = {
  "where-pedro-works": {
    id: "where-pedro-works",
    title: "Where Pedro works",
    question: "Why does my agent think you're between jobs?",
    nodes: [
      {
        id: "w1",
        text: "Works at The Graph Foundation; email pedro@thegraph.foundation.",
        age: "3 weeks ago",
        source: "user",
        state: "current",
        pinned: true,
      },
      {
        id: "w2",
        text: "Pedro is between jobs right now.",
        age: "2 days ago",
        source: "assistant",
        state: "rival",
        edgeFromPrev: "contradicts",
      },
    ],
    conflict: { topId: "w1", bottomId: "w2" },
  },
  "july-trip": {
    id: "july-trip",
    title: "The July trip",
    question: "How did the trip plan change?",
    nodes: [
      {
        id: "j1",
        text: "Planning a family trip to Lisbon in early July.",
        age: "2 weeks ago",
        source: "user",
        state: "past",
      },
      {
        id: "j2",
        text: "Trip moved to Porto to be near the grandparents.",
        age: "Last week",
        source: "user",
        state: "current",
        edgeFromPrev: "supersedes",
      },
      {
        id: "j3",
        text: "Book flights once the kids' passports are confirmed.",
        age: "Last week",
        source: "user",
        state: "current",
        edgeFromPrev: "derived-from",
      },
    ],
  },
};

export const EDGE_LABEL: Record<EdgeType, string> = {
  supersedes: "replaced by",
  contradicts: "contradicts",
  "derived-from": "led to",
};
