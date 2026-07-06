/**
 * Belief lineage — a claim's typed evolution via supersession.
 *
 * Edges available today come from `superseded_by` (the tombstone chain →
 * "replaced by"). `contradicts` / `led-to` edges need backend contradiction
 * persistence (#306) and are out of scope until then. Requires the full-history
 * fetch (active + inactive), since superseded versions are tombstoned.
 */
import type { VaultItem } from "../types";

/** Ordered (oldest → newest) chain of versions of one belief, identified by any
 *  member's inner claim id. Returns [] if the claim isn't present. */
export function buildClaimLineage(items: VaultItem[], claimId: string): VaultItem[] {
  const byClaimId = new Map(items.map((i) => [i.claim.id, i]));
  // predecessor of node N = the item whose superseded_by points at N.
  const predOf = new Map<string, VaultItem>();
  for (const it of items) {
    if (it.claim.superseded_by) predOf.set(it.claim.superseded_by, it);
  }

  let start = byClaimId.get(claimId);
  if (!start) return [];

  const back = new Set<string>([start.claim.id]);
  for (;;) {
    const pred = predOf.get(start.claim.id);
    if (!pred || back.has(pred.claim.id)) break;
    back.add(pred.claim.id);
    start = pred;
  }

  const chain: VaultItem[] = [];
  const fwd = new Set<string>();
  let cur: VaultItem | undefined = start;
  while (cur && !fwd.has(cur.claim.id)) {
    chain.push(cur);
    fwd.add(cur.claim.id);
    cur = cur.claim.superseded_by ? byClaimId.get(cur.claim.superseded_by) : undefined;
  }
  return chain;
}

/** All multi-version chains (length > 1), newest-activity first. Powers the
 *  Review "changed / handled for you" feed. */
export function listChangedChains(items: VaultItem[]): VaultItem[][] {
  const visited = new Set<string>();
  const chains: VaultItem[][] = [];
  for (const it of items) {
    if (visited.has(it.claim.id)) continue;
    const chain = buildClaimLineage(items, it.claim.id);
    chain.forEach((c) => visited.add(c.claim.id));
    if (chain.length > 1) chains.push(chain);
  }
  chains.sort((a, b) => {
    const ad = a[a.length - 1].createdAt.getTime();
    const bd = b[b.length - 1].createdAt.getTime();
    return bd - ad;
  });
  return chains;
}
