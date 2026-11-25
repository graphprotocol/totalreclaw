/**
 * Put each block notice back at its input index, so `results[i]` maps to
 * `facts[i]`. `results` holds the stored/skipped facts in order; entries that
 * were not blocked fill the remaining slots.
 */
export function mergeBlockedResults<T>(
  results: T[],
  blocked: Array<{ index: number; result: Record<string, unknown> }>,
): Array<T | Record<string, unknown>> {
  if (blocked.length === 0) return results;
  const total = results.length + blocked.length;
  const out: Array<T | Record<string, unknown>> = new Array(total);
  for (const b of blocked) out[b.index] = b.result;
  let next = 0;
  for (let i = 0; i < total; i++) {
    if (out[i] === undefined) out[i] = results[next++];
  }
  return out;
}
