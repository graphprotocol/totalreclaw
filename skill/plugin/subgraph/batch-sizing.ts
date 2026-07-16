/**
 * batch-sizing.ts — internal#449
 *
 * Byte-capped adaptive batch sizing for the plugin's `executeBatch` UserOp
 * path (`subgraph-store.ts::submitFactBatchOnChain`). Ports the now-canonical
 * Python design (python/src/totalreclaw/operations.py::group_and_store_adaptive,
 * internal#435/#461/#490) to TS and CALIBRATES it against the plugin's real
 * `encodeFactProtobuf` output (the est>=real invariant the Python side
 * verified across 3 review rounds).
 *
 * Three concerns, factored as pure functions so they pin without a bundler or
 * the AA10/AA25 submit machinery:
 *
 *   1. `estimatePayloadBytes` — a calibrated est>=real byte estimator. Bounds
 *      the REAL `encodeFactProtobuf` protobuf length for a fact, used to predict
 *      calldata size before a group is submitted.
 *   2. `groupPayloadsBySize` — a SINGLE grouping layer that flushes a group
 *      before appending a fact that would exceed EITHER the count cap or the
 *      byte cap. (One layer only — the nested double-pass that #490's review
 *      caught is structurally impossible here.)
 *   3. `groupAndStoreAdaptive` — stores each group through a caller-supplied
 *      `storeFn`, halving-and-retrying a group that sim-reverts (floor 1).
 *
 * Why calibrate rather than copy the Python constants verbatim: the plugin
 * encodes its 640-dim embedding as `encryptToHex(JSON.stringify(embedding))`
 * (an encrypted JSON string, field 13) — typically ~8-23KB on the wire — whereas
 * the Python side packs f32 embeddings (~4.6KB). Python's flat
 * `_BYTES_PER_EMBEDDING` (6060) therefore UNDER-counts the plugin's real
 * protobuf and would violate the est>=real rule. The estimator below measures
 * the plugin's REAL encrypted-field lengths instead, so it can never under-count
 * regardless of embedding precision. `MAX_BATCH_BYTES` (32KB), the per-index
 * cost, and the fixed-overhead constants ARE ported verbatim from Python.
 */

// ---------------------------------------------------------------------------
// Calibrated constants
// ---------------------------------------------------------------------------

/**
 * Per-blind-index wire cost. Protobuf field 5 (`blind_indices`) is a
 * `repeated string`; each element is a 64-hex-char SHA-256 → 64 bytes + 1 tag
 * + 1 length varint = 66 bytes real (measured: 66.0 across 1..100 indices).
 * 68 keeps a 2-byte margin and matches python's `_BYTES_PER_BLIND_INDEX`.
 */
export const BYTES_PER_BLIND_INDEX = 68;

/**
 * Scalar-field + wrapper overhead floor: id, timestamp, owner, content_fp,
 * source, agent_id, decay_score, version, per-field varints/tags, and the outer
 * protobuf framing. Measured real ≈ 200-300; 620 (ported from python) keeps a
 * safe margin for unusually long ids / agent names and matches the python
 * constant. These fields are tiny relative to the blob/indices/embedding terms,
 * so the conservatism does not materially shrink groups.
 */
export const BYTES_FIXED_OVERHEAD = 620;

/**
 * Protobuf tag + length-varint overhead on the embedding string field (13).
 * The embedding hex dominates this field, so a flat 8-byte margin is ample.
 */
export const EMBEDDING_FIELD_OVERHEAD = 8;

/**
 * Estimated on-chain calldata-byte ceiling per batched UserOp (python
 * `MAX_BATCH_BYTES`). Kept comfortably under the observed ~85KB sim-revert
 * cliff (a sim-passing ~67KB op at 15 facts still didn't reliably get INCLUDED
 * on the staging bundler, so 32KB buys inclusion headroom too). Groups flush
 * when adding the next fact would exceed EITHER this or the count ceiling.
 */
export const MAX_BATCH_BYTES = 32_000;

/**
 * Belt-and-braces count ceiling alongside `MAX_BATCH_BYTES`. This is core's
 * `userop::MAX_BATCH_SIZE` (30) — the hard guard `encodeBatchCall` enforces —
 * read here as a stable constant (the plugin's submit path also has the core
 * guard as a backstop, so the two can never disagree). NOTE: the python
 * reference uses a more conservative 15 (`MAX_BATCH_GROUP_COUNT`); the byte cap
 * is the real governor in both, so the difference only affects how many TINY
 * (embedding-less) facts pack into one group. See internal#449.
 */
export const MAX_BATCH_GROUP_COUNT = 30;

// ---------------------------------------------------------------------------
// Estimator
// ---------------------------------------------------------------------------

/**
 * The FactPayload fields the estimator consumes. These are the post-encrypt,
 * pre-encode values the plugin already has in hand when it builds a
 * `FactPayload` — so the estimator measures the REAL encrypted lengths rather
 * than a text-based heuristic. `blindIndices` is the full index array the
 * store assembles (word + 20 LSH buckets + entity trapdoors), so the index term
 * accounts for every index regardless of source.
 */
export interface FactSizing {
  /** Hex-encoded XChaCha20-Poly1305 ciphertext of the canonical claim blob. */
  encryptedBlob: string;
  /** Full blind-index array (word + LSH + entity). */
  blindIndices: string[];
  /** Hex-encoded encrypted embedding, when the fact carries one. */
  encryptedEmbedding?: string;
}

/**
 * Estimate a single fact's encoded protobuf byte length, bounding the REAL
 * `encodeFactProtobuf` output (est >= real, verified in batch-sizing.test.ts).
 *
 * Terms mirror the python estimator, calibrated to the plugin's encoder:
 *   - fixed overhead (scalar fields + framing) — `BYTES_FIXED_OVERHEAD`;
 *   - the encrypted claim blob — protobuf field 4 is `bytes`, so the hex is
 *     decoded to raw bytes (length / 2), NOT stored char-for-char;
 *   - the blind indices — the dominant, entropy-dependent term — the REAL
 *     index count × per-index wire cost (a char-linear estimate cannot bound
 *     this, hence the PR #461 review NO-GO);
 *   - the encrypted embedding — protobuf field 13 is `string`, so the hex is
 *     stored char-for-char (its real length), NOT decoded.
 */
export function estimatePayloadBytes(fs: FactSizing): number {
  let est = BYTES_FIXED_OVERHEAD;
  // Field 4 `encrypted_blob` (bytes): hex decoded to raw bytes by encodeFactProtobuf.
  est += Math.ceil(fs.encryptedBlob.length / 2);
  // Field 5 `blind_indices` (repeated string): each 64-hex index stored as-is.
  est += fs.blindIndices.length * BYTES_PER_BLIND_INDEX;
  // Field 13 `encrypted_embedding` (string): hex stored char-for-char.
  if (fs.encryptedEmbedding) {
    est += fs.encryptedEmbedding.length + EMBEDDING_FIELD_OVERHEAD;
  }
  return est;
}

// ---------------------------------------------------------------------------
// Grouping — single layer, dual-cap (count AND bytes)
// ---------------------------------------------------------------------------

/**
 * Group items so each group respects BOTH a count cap and a byte cap. A group
 * is flushed BEFORE appending the item whose addition would exceed either cap.
 * A single item larger than `maxBytes` still forms its own group (never
 * dropped) — the adaptive halving in `groupAndStoreAdaptive` is the backstop if
 * such a lone op still sim-reverts.
 *
 * Generic over the item type with a caller-supplied `sizeOf`, so the same
 * algorithm groups encoded `Buffer`s (shipped path: `sizeOf = b => b.length`)
 * and sizing inputs (estimator path: `sizeOf = estimatePayloadBytes`).
 */
export function groupPayloadsBySize<T>(
  payloads: T[],
  maxCount: number,
  maxBytes: number,
  sizeOf: (p: T) => number,
): T[][] {
  const groups: T[][] = [];
  let group: T[] = [];
  let groupBytes = 0;
  for (const p of payloads) {
    const est = sizeOf(p);
    if (group.length > 0 && (group.length >= maxCount || groupBytes + est > maxBytes)) {
      groups.push(group);
      group = [];
      groupBytes = 0;
    }
    group.push(p);
    groupBytes += est;
  }
  if (group.length > 0) groups.push(group);
  return groups;
}

// ---------------------------------------------------------------------------
// Sim-revert detection
// ---------------------------------------------------------------------------

/**
 * Does this error look like an oversized-`executeBatch` simulation failure?
 *
 * Pimlico surfaces such a failure as the catch-all `-32500 "... reverted during
 * simulation ..."`. A `-32500` that is an AA25 (exhausted the userop-layer
 * retry) is NOT a size revert — halving it would be pointless — so any
 * AA25-tagged error is excluded (PR #461 review Finding 2). The submit path's
 * own AA10/AA25 retry loop (plugin #407) handles those separately.
 */
export function isSimRevertError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err);
  const msgL = msg.toLowerCase();
  // Token-bounded so an unrelated hex/id embedding "aa25" can't misclassify
  // a genuine size revert as an AA25 (review #531 finding 3).
  if (/\baa25\b|invalid account nonce/.test(msgL)) return false;
  return /reverted during simulation/.test(msgL) || msgL.includes('-32500');
}

// ---------------------------------------------------------------------------
// Adaptive store — group, then halve-on-simfail
// ---------------------------------------------------------------------------

/**
 * The result of storing a batch: the successful per-(sub)group results (in
 * input order) and a list of surfaced error strings (empty on full success).
 */
export interface AdaptiveStoreResult<R> {
  results: R[];
  errors: string[];
}

/**
 * Single source of truth for byte-capped batching + halve-on-simfail.
 *
 * Groups `payloads` by BOTH `maxCount` and `maxBytes` (via
 * `groupPayloadsBySize`), then stores each group through `storeFn`. When a
 * group of >1 sim-reverts (`isSimRevertError`), it is split in half and each
 * half retried recursively (floor 1) — making the writer adaptive to wherever a
 * given bundler's calldata cliff sits, on top of the static byte cap. A
 * duplicate rejection is swallowed (no result, no error); any other error — or
 * a size revert at the single-fact floor — is surfaced into `errors` so the
 * batch is counted FAILED, not silently dropped.
 *
 * `storeFn` returns one result per group; `results` collects one entry per
 * successful (sub)group store (halving a group yields multiple entries).
 */
export async function groupAndStoreAdaptive<T, R>(
  payloads: T[],
  storeFn: (group: T[]) => Promise<R>,
  maxCount: number,
  maxBytes: number,
  sizeOf: (p: T) => number,
): Promise<AdaptiveStoreResult<R>> {
  const groups = groupPayloadsBySize(payloads, maxCount, maxBytes, sizeOf);
  const results: R[] = [];
  const errors: string[] = [];
  for (const group of groups) {
    const r = await storeGroupAdaptive(storeFn, group);
    results.push(...r.results);
    errors.push(...r.errors);
  }
  return { results, errors };
}

/** Store one group, halving-and-retrying on a simulation-size revert. */
async function storeGroupAdaptive<T, R>(
  storeFn: (group: T[]) => Promise<R>,
  group: T[],
): Promise<AdaptiveStoreResult<R>> {
  try {
    const r = await storeFn(group);
    return { results: [r], errors: [] };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    // Duplicate rejection — swallow (no ids, no error), matching python.
    // ANCHORED (review #531 finding 2): requires a relay-shaped rejection —
    // an HTTP 409 token AND a duplicate/fingerprint token together. A bare
    // substring match ("duplicate"/"fingerprint" anywhere, e.g. inside an
    // unrelated bundler error or a hex blob) must NOT silently drop a group.
    // The on-chain bundler path has no duplicate failure mode today (dedup
    // is client-side pre-submit), so this branch only matters for a future
    // relay-store caller.
    if (/\b409\b/.test(msg) && /duplicate|fingerprint/i.test(msg)) {
      return { results: [], errors: [] };
    }
    if (isSimRevertError(err) && group.length > 1) {
      const mid = group.length >> 1;
      const a = await storeGroupAdaptive(storeFn, group.slice(0, mid));
      const b = await storeGroupAdaptive(storeFn, group.slice(mid));
      return { results: [...a.results, ...b.results], errors: [...a.errors, ...b.errors] };
    }
    // Single-fact floor (can't split further) or a non-size error — surface it
    // so the fact is counted failed rather than silently dropped.
    return { results: [], errors: [`Batch store failed (${group.length} facts): ${msg}`] };
  }
}
