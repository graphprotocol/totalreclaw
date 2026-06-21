/**
 * memory-runtime — adapter that bridges OpenClaw's FILE-ORIENTED memory
 * result shapes to TR's ENCRYPTED-FACT + ON-CHAIN vault.
 *
 * Phase 2 (Task 2.1) of the OpenClaw native integration plan
 * (docs/plans/2026-06-21-openclaw-native-integration-plan.md, 2026-06-21).
 *
 * WHY THIS FILE EXISTS — the load-bearing discovery:
 *   OpenClaw 2026.6.8's memory subsystem calls
 *   `runtime.getMemorySearchManager(...)` to get a MemorySearchManager,
 *   then `.search(query)` / a file-read method on it. Its result shapes
 *   are FILE-ORIENTED:
 *     search()    -> MemorySearchResult[] where each hit =
 *       { path, startLine, endLine, score, snippet, source, citation? }
 *     read-by-rel -> { text, path, truncated?, from?, lines?, nextFrom? }
 *
 *   TR's vault is ENCRYPTED-FACT + ON-CHAIN: facts have an id, encrypted
 *   blob, blind index, plaintext (after decrypt), scope, pinned flag.
 *   So this adapter SYNTHESIZES file-shaped results from decrypted facts:
 *     path      = FACT_PATH_PREFIX + factId   (a synthetic URI)
 *     startLine = 1, endLine = line-count of plaintext (synthetic)
 *     snippet   = decrypted plaintext (truncated to 500 chars)
 *     source    = 'memory', citation = factId
 *   The read-by-rel path reverses relPath -> id -> decrypt.
 *
 *   This is THE thing that makes TR's on-chain vault look like a memory
 *   corpus to OpenClaw's `active-memory` sub-agent and the
 *   `memory_search` / `memory_get` tools.
 *
 * SCANNER-CLEAN HARD CONTRACT (env=N net=N):
 *   This file is pure orchestration. It touches NO environment state and
 *   performs NO outbound network I/O. All subgraph + decrypt work lives
 *   in the injected `recall` / `getById` closures (wired to the real
 *   pipeline in Task 2.3: subgraph-search + vault-crypto.decrypt +
 *   reranker). Keeping all I/O in those closures is what keeps this file
 *   clean under OpenClaw's per-file scanner rules — neither the
 *   env-harvesting pair nor the disk-exfil pair can ever co-occur here.
 *   `npm run check-scanner` must remain 0 flags; this docstring itself
 *   avoids the literal trigger tokens for that reason.
 *
 * FUTURE PHASE 2 ADDITIONS (home for them):
 *   - Task 2.3: `createTrMemoryPluginRuntime` — the MemoryPluginRuntime
 *     wrapper that owns the wiring (recall/getById → real pipeline).
 *   - Task 2.4: `promptBuilder` (guidance + quota + pinned).
 *   - Task 2.5: `flushPlanResolver`.
 *   For now this file ships ONLY the TrMemorySearchManager adapter.
 */

// ---------------------------------------------------------------------------
// Types — injected caller shapes. Kept loose (no OpenClaw type import) so
// the plugin compiles without depending on OpenClaw's type package. The
// returned manager is STRUCTURALLY compatible with OpenClaw's
// MemorySearchManager interface at runtime.
// ---------------------------------------------------------------------------

/**
 * A decrypted fact ready to be surfaced as a memory hit. `pinned` is
 * optional and forwarded by recall if the pipeline already knows it.
 */
export interface TrFact {
  id: string;
  plaintext: string;
  score: number;
  pinned?: boolean;
}

/** recall() runs the real subgraph-search + decrypt + reranker pipeline. */
export interface TrRecallFn {
  (query: string, opts?: { maxResults?: number }): Promise<TrFact[]>;
}

/** getById() decrypts a single fact by id (the readFile reverse path). */
export interface TrGetFn {
  (id: string): Promise<{ id: string; plaintext: string } | null>;
}

export interface TrMemorySearchManagerDeps {
  recall: TrRecallFn;
  getById: TrGetFn;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Synthetic URI prefix encoding a fact id as a memory path. Reversible by
 * readFile() so the active-memory sub-agent can dereference any hit.
 */
export const FACT_PATH_PREFIX = 'totalreclaw://facts/';

/** Maximum snippet length surfaced in search() hits. Keeps tool payloads small. */
const SNIPPET_MAX = 500;

/** Default search cap when the caller doesn't pass maxResults. */
const DEFAULT_MAX_RESULTS = 8;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function toLineCount(s: string): number {
  // '' -> 1 (a single empty line), 'a\nb' -> 2. Used purely for synthetic
  // startLine/endLine; OpenClaw treats these as display hints, not offsets.
  return s.split('\n').length;
}

// ---------------------------------------------------------------------------
// createTrMemorySearchManager — the adapter factory
// ---------------------------------------------------------------------------

export function createTrMemorySearchManager(deps: TrMemorySearchManagerDeps) {
  /**
   * search(): run recall, filter by minScore if requested, and synthesize
   * file-shaped MemorySearchResult hits. recall() is responsible for
   * ordering by relevance; we only slice to maxResults and (optionally)
   * drop below-minScore hits.
   */
  async function search(
    query: string,
    opts?: { maxResults?: number; minScore?: number },
  ) {
    const max = opts?.maxResults ?? DEFAULT_MAX_RESULTS;
    const minScore = opts?.minScore;
    const facts = await deps.recall(query, { maxResults: max });
    const filtered = minScore === undefined ? facts : facts.filter((f) => f.score >= minScore!);
    return filtered.slice(0, max).map((f) => ({
      path: `${FACT_PATH_PREFIX}${f.id}`,
      startLine: 1,
      endLine: toLineCount(f.plaintext),
      score: f.score,
      snippet: f.plaintext.slice(0, SNIPPET_MAX),
      source: 'memory' as const,
      citation: f.id,
    }));
  }

  /**
   * readFile(): reverse relPath -> id -> decrypt. Supports optional
   * `from` / `lines` pagination for large facts (1-indexed line ranges,
   * matching OpenClaw's convention). Returns nextFrom when more lines
   * remain so the caller can page.
   */
  async function readFile(params: { relPath: string; from?: number; lines?: number }) {
    const id = params.relPath.startsWith(FACT_PATH_PREFIX)
      ? params.relPath.slice(FACT_PATH_PREFIX.length)
      : params.relPath;
    const f = await deps.getById(id);
    if (!f) throw new Error(`fact not found: ${id}`);

    const from = params.from && params.from > 0 ? params.from : 1;
    const want = params.lines && params.lines > 0 ? params.lines : undefined;

    const allLines = f.plaintext.split('\n');
    const totalLines = allLines.length;
    const sliceEnd = want === undefined ? totalLines : Math.min(from + want - 1, totalLines);
    const text = allLines.slice(from - 1, sliceEnd).join('\n');
    const truncated = want !== undefined && from + want - 1 < totalLines;
    const nextFrom = truncated ? from + want! : undefined;

    return {
      text,
      path: `${FACT_PATH_PREFIX}${id}`,
      truncated,
      from,
      lines: sliceEnd - from + 1,
      nextFrom,
    };
  }

  function status() {
    // `backend: 'builtin'` mirrors OpenClaw's non-qmd providers. The
    // provider string is what the active-memory sub-agent logs against.
    return { backend: 'builtin' as const, provider: 'totalreclaw' };
  }

  /**
   * probeEmbeddingAvailability / probeVectorAvailability: optimistic OK
   * here. The real availability depends on the injected pipeline (Task
   * 2.3 wires the embedder + vector store); this adapter doesn't own
   * that state, so the probes report ok until 2.3 gives them real hooks.
   */
  async function probeEmbeddingAvailability() {
    return { ok: true };
  }

  async function probeVectorAvailability() {
    return true;
  }

  async function close() {}

  return {
    search,
    readFile,
    status,
    probeEmbeddingAvailability,
    probeVectorAvailability,
    close,
  };
}
