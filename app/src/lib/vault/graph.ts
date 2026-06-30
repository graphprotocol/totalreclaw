/**
 * Client-side knowledge graph derived from the decrypted vault.
 *
 * The subgraph is server-blind — it stores only ciphertext + envelope. Entities
 * (`claim.entities[]`, per-claim) and topics (`metadata.topics_discussed`,
 * Crystal-only) live INSIDE the encrypted blob, so the graph is computed here
 * after full decryption. Topic↔entity links aren't stored anywhere; we derive
 * them by co-occurrence within a session. Entity-nav is the reliable axis;
 * topics are sparse (imports + MCP write no Crystals → no topics).
 *
 * See app/src/proto/MEMORY-REDESIGN.md ("Entity-nav is the reliable axis").
 */
import type { SessionGroup } from "./timeline";

export type GraphKind = "topic" | "entity";

export interface KGNode {
  id: string;
  label: string;
  kind: GraphKind;
  /** Number of sessions this node appears in (for emphasis / ranking). */
  weight: number;
}

export interface KGLink {
  source: string;
  target: string;
}

export interface DerivedGraph {
  nodes: KGNode[];
  links: KGLink[];
  neighborsOf: (id: string) => Set<string>;
  /** Distinct entities found before the layout cap. */
  entityCount: number;
  /** Distinct topics found (Crystal-only). */
  topicCount: number;
  /** Entities dropped to keep the force layout legible (0 if none). Surfaced
   *  honestly in the UI — never a silent truncation. */
  cappedEntities: number;
}

/** Keep the force layout readable. Real vaults can have hundreds of entities;
 *  we render the most-connected ones and tell the user how many were held back. */
const MAX_ENTITY_NODES = 60;

const entId = (lc: string) => `e:${lc}`;
const topId = (lc: string) => `t:${lc}`;

/** Build the entity/topic graph from the (already filtered) session timeline. */
export function buildGraph(groups: SessionGroup[]): DerivedGraph {
  // Tally entity frequency (by case-folded name → display + session count).
  const entityFreq = new Map<string, { label: string; weight: number }>();
  const topicFreq = new Map<string, { label: string; weight: number }>();

  // Per-session membership (case-folded) for co-occurrence edges.
  const sessions: { entities: string[]; topics: string[] }[] = [];

  for (const g of groups) {
    const entities = uniqLc(g.entityNames, entityFreq);
    const topics = uniqLc(g.crystal?.claim.metadata?.topics_discussed ?? [], topicFreq);
    if (entities.length || topics.length) sessions.push({ entities, topics });
  }

  const entityCount = entityFreq.size;
  const topicCount = topicFreq.size;

  // Cap entities to the most-frequent for layout legibility.
  const keptEntities = new Set(
    [...entityFreq.entries()]
      .sort((a, b) => b[1].weight - a[1].weight)
      .slice(0, MAX_ENTITY_NODES)
      .map(([lc]) => lc),
  );
  const cappedEntities = Math.max(0, entityCount - keptEntities.size);

  const nodes: KGNode[] = [];
  for (const [lc, { label, weight }] of topicFreq) {
    nodes.push({ id: topId(lc), label, kind: "topic", weight });
  }
  for (const lc of keptEntities) {
    const e = entityFreq.get(lc)!;
    nodes.push({ id: entId(lc), label: e.label, kind: "entity", weight: e.weight });
  }

  // Co-occurrence edges (deduped), only among kept nodes.
  const seen = new Set<string>();
  const links: KGLink[] = [];
  const addEdge = (a: string, b: string) => {
    if (a === b) return;
    const key = a < b ? `${a}|${b}` : `${b}|${a}`;
    if (seen.has(key)) return;
    seen.add(key);
    links.push({ source: a, target: b });
  };

  for (const s of sessions) {
    const ents = s.entities.filter((lc) => keptEntities.has(lc)).map(entId);
    const tops = s.topics.map(topId);
    // topic ↔ entity
    for (const t of tops) for (const e of ents) addEdge(t, e);
    // entity ↔ entity (same session)
    for (let i = 0; i < ents.length; i++)
      for (let j = i + 1; j < ents.length; j++) addEdge(ents[i], ents[j]);
    // topic ↔ topic (keeps multi-topic sessions connected)
    for (let i = 0; i < tops.length; i++)
      for (let j = i + 1; j < tops.length; j++) addEdge(tops[i], tops[j]);
  }

  const adjacency = new Map<string, Set<string>>();
  for (const n of nodes) adjacency.set(n.id, new Set());
  for (const l of links) {
    adjacency.get(l.source)?.add(l.target);
    adjacency.get(l.target)?.add(l.source);
  }

  return {
    nodes,
    links,
    neighborsOf: (id) => adjacency.get(id) ?? new Set<string>(),
    entityCount,
    topicCount,
    cappedEntities,
  };
}

/** Case-fold + dedupe a name list, tallying frequency into `freq`. Returns the
 *  case-folded keys (for edge building). */
function uniqLc(
  names: string[],
  freq: Map<string, { label: string; weight: number }>,
): string[] {
  const out: string[] = [];
  const local = new Set<string>();
  for (const raw of names) {
    const label = raw.trim();
    if (!label) continue;
    const lc = label.toLowerCase();
    if (local.has(lc)) continue;
    local.add(lc);
    out.push(lc);
    const prev = freq.get(lc);
    if (prev) prev.weight += 1;
    else freq.set(lc, { label, weight: 1 });
  }
  return out;
}
