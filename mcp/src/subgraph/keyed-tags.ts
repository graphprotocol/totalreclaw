/**
 * TotalReclaw MCP - keyed blind indices (internal#573).
 *
 * The ONE module that produces every tag, trapdoor and content fingerprint
 * the managed-mode MCP server puts on the wire. Everything here is a thin
 * call into `@totalreclaw/core`; nothing in this file hashes anything itself
 * and no key is ever derived, exposed or logged here.
 *
 *   fact tags   = sortDedupTags(stemTags(text) + hashKeyed(embedding) + entityTag(name)...)
 *   query tags  = generateSearchTrapdoorsKeyed(...)      (final, already sorted)
 *   fingerprint = generateContentFingerprintV2(text, dedupKey)
 *
 * Callers write the returned lists as they are: no reordering, no filtering,
 * nothing appended afterwards.
 */

import {
  WasmBlindIndexer,
  generateContentFingerprintV2,
  generateSearchTrapdoorsKeyed,
  sortDedupTags,
} from '@totalreclaw/core';
import { LSHHasher } from './lsh.js';
import { liveDecayScore } from './decay.js';
import type { FactPayload } from './protobuf.js';

export class KeyedTagger {
  // ES private fields: not enumerable, so JSON.stringify / util.inspect /
  // console.log of a tagger (or of a state object that holds one) show no key
  // material. No getters, no toJSON.
  readonly #indexer: WasmBlindIndexer;
  readonly #dedupKeyHex: string;
  readonly #lsh: LSHHasher;

  constructor(encryptionKey: Buffer, dedupKey: Buffer, lsh: LSHHasher) {
    this.#indexer = new WasmBlindIndexer(encryptionKey.toString('hex'));
    this.#dedupKeyHex = dedupKey.toString('hex');
    this.#lsh = lsh;
  }

  /** Sorted, deduplicated tag list for a fact that is about to be written. */
  factTags(text: string, embedding?: number[], entityNames: string[] = []): string[] {
    const tags: string[] = [...this.#indexer.stemTags(text)];
    if (embedding && embedding.length > 0) tags.push(...this.#lsh.hashKeyed(embedding));
    for (const name of entityNames) tags.push(this.#indexer.entityTag(name));
    return sortDedupTags(tags);
  }

  /** Final, sorted trapdoor list for a search. Callers batch it in order. */
  queryTrapdoors(query: string, queryEmbedding: number[], entityNames: string[] = []): string[] {
    return generateSearchTrapdoorsKeyed(
      query,
      entityNames,
      new Float32Array(queryEmbedding),
      this.#indexer,
      this.#lsh.wasmHasher,
    );
  }

  /** Keyed tag for one entity name (core normalises the name). */
  entityTag(name: string): string {
    return this.#indexer.entityTag(name);
  }

  /** Content fingerprint v2 (replaces fp v1 everywhere). */
  contentFp(text: string): string {
    return generateContentFingerprintV2(text, this.#dedupKeyHex);
  }
}

export interface BuildFactPayloadInput {
  tagger: KeyedTagger;
  id: string;
  owner: string;
  /** Write-path label (e.g. `mcp_remember`); not encoded on-chain. */
  source: string;
  /** Plaintext of the fact (fingerprint input). */
  text: string;
  /** Output of `encrypt()` for the claim blob (base64). */
  encryptedBlobBase64: string;
  /** Output of `encryptEmbedding()`, when an embedding exists. */
  encryptedEmbedding?: string;
  /** The sorted keyed tag list from `tagger.factTags(...)`, written as is. */
  tags: string[];
}

/**
 * Per-item assembly shared by remember, crystal debrief and legacy debrief:
 * tags in, fingerprint v2, encrypted embedding, live decay. One place builds
 * the write payload, so there is one seam to test on the real write path.
 */
export function buildFactPayload(input: BuildFactPayloadInput): FactPayload {
  return {
    id: input.id,
    timestamp: new Date().toISOString(),
    owner: input.owner,
    encryptedBlob: Buffer.from(input.encryptedBlobBase64, 'base64').toString('hex'),
    blindIndices: input.tags,
    decayScore: liveDecayScore(), // constant 1.0, no importance (#789)
    source: input.source,
    contentFp: input.tagger.contentFp(input.text),
    agentId: 'mcp-server',
    encryptedEmbedding: input.encryptedEmbedding,
  };
}
