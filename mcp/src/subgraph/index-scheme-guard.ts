/**
 * internal#573: constants for the pin / unpin / retype / set_scope guard.
 *
 * The guard refuses to rewrite a fact unless it is confirmed to carry keyed
 * blind indices (`Fact.indexScheme == 1`). Re-writing an unkeyed fact would
 * leave a keyed twin next to a legacy original.
 */

/**
 * Proposed user-facing copy (needs owner approval before release). Does not
 * suggest saving the memory again: re-storing old text would create a twin.
 */
export const INDEX_SCHEME_UNCONFIRMED_MSG =
  "This memory can't be changed right now because its storage format could not be confirmed. Nothing was changed. Try again later.";

export const INDEX_SCHEME_UNCONFIRMED_CODE = 'index_scheme_unconfirmed' as const;

import type { RecentWrites } from './recent-writes.js';

/**
 * Build the `fetchIndexScheme` dep for pin / unpin / retype / set_scope.
 *
 * A fact in this process's `RecentWrites` record was encoded by this running
 * process, so it is keyed by construction: pass without a query. (A query
 * could land on an indexer that has not caught up and wrongly refuse a pin
 * issued right after a remember.) Anything else asks the subgraph.
 */
export function createIndexSchemeLookup(opts: {
  owner: string;
  cache: RecentWrites;
  lookupSubgraph: (factId: string) => Promise<number | null>;
}): (factId: string) => Promise<number | null> {
  const { owner, cache, lookupSubgraph } = opts;
  return async (factId: string) => {
    if (cache.get(owner, factId)) return 1;
    return lookupSubgraph(factId);
  };
}
