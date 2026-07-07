// ---------------------------------------------------------------------------
// Import subsystem — extracted from index.ts.
// ---------------------------------------------------------------------------
//
// The CLI import surface (`openclaw totalreclaw import from|status|abort`):
// adapter parsing, the two-pass smart-import pipeline (profile + triage via
// @totalreclaw/core WASM), background batch/chunk execution, and import-state
// bookkeeping. Self-contained except for `storeExtractedFacts` (which closes
// over the plugin session keys and stays in index.ts) — injected via
// configureImportRuntime(). Owns the `_importInProgress` flag that the
// agent_end hook reads through isImportInProgress()/setImportInProgress().
//
// No environment-variable reads live here (env stays in config.ts / entry.ts per the
// OpenClaw env-harvesting scanner rule).

import { createRequire } from 'node:module';
import crypto from 'node:crypto';
import {
  writeImportState,
  readImportState,
  isImportStale,
  readMostRecentActiveImport,
  type ImportState,
} from './import-state-manager.js';
import {
  extractFacts,
  EXTRACTION_SYSTEM_PROMPT,
  type ExtractedFact,
} from '../extraction/extractor.js';
import { resolveLLMConfig, chatCompletion } from '../llm/llm-client.js';
import { encrypt } from '../crypto/crypto.js';
import type { OpenClawPluginApi, SmartImportContext } from '../runtime/types.js';

const __cjsRequire = createRequire(import.meta.url);

// storeExtractedFacts is injected from index.ts: it closes over the plugin
// session state (encryption keys, apiClient, subgraph owner), so it stays in
// the composing entry point and is wired in here at register time.
type StoreExtractedFacts = (
  facts: ExtractedFact[],
  logger: OpenClawPluginApi['logger'],
  sourceOverride?: string,
) => Promise<number>;
let _storeExtractedFacts: StoreExtractedFacts | null = null;
export function configureImportRuntime(deps: { storeExtractedFacts: StoreExtractedFacts }): void {
  _storeExtractedFacts = deps.storeExtractedFacts;
}
function storeExtractedFacts(
  facts: ExtractedFact[],
  logger: OpenClawPluginApi['logger'],
  sourceOverride?: string,
): Promise<number> {
  if (!_storeExtractedFacts) throw new Error('import-runtime: configureImportRuntime() not called');
  return _storeExtractedFacts(facts, logger, sourceOverride);
}

// BUG-2 fix: Skip agent_end extraction during import operations. Import
// failures previously triggered agent_end -> re-extraction -> re-import loops.
// The agent_end hook in index.ts reads this via the getter/setter below.
let _importInProgress = false;
export function isImportInProgress(): boolean {
  return _importInProgress;
}
export function setImportInProgress(value: boolean): void {
  _importInProgress = value;
}

// ---------------------------------------------------------------------------
// Import handler (for the registerCli `openclaw totalreclaw import-from` surface)
// ---------------------------------------------------------------------------

/**
 * Handle import_from calls (CLI subcommand path; was the totalreclaw_import_from
 * agent tool before Phase 3.2 retired the agent tools).
 *
 * Two paths:
 * 1. Pre-structured sources (Mem0, MCP Memory) — adapter returns facts directly,
 *    stored via storeExtractedFacts().
 * 2. Conversation-based sources (ChatGPT, Claude) — adapter returns conversation
 *    chunks, each chunk is passed through extractFacts() (the same LLM extraction
 *    pipeline used for auto-extraction), then stored via storeExtractedFacts().
 */
export async function handlePluginImportFrom(
  params: Record<string, unknown>,
  logger: OpenClawPluginApi['logger'],
): Promise<Record<string, unknown>> {
  _importInProgress = true;
  const startTime = Date.now();

  const source = params.source as string;
  const validSources = ['mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini'];

  if (!source || !validSources.includes(source)) {
    return { success: false, error: `Invalid source. Must be one of: ${validSources.join(', ')}` };
  }

  // Generate import_id up front so dry-run responses and background tasks share it.
  const importId = (params.resume_id as string | undefined) ?? crypto.randomUUID();

  try {
    const { getAdapter } = await import('../import-adapters/index.js');
    const adapter = getAdapter(source as import('../import-adapters/types.js').ImportSource);

    const parseResult = await adapter.parse({
      content: params.content as string | undefined,
      api_key: params.api_key as string | undefined,
      source_user_id: params.source_user_id as string | undefined,
      api_url: params.api_url as string | undefined,
      file_path: params.file_path as string | undefined,
    });

    const hasChunks = parseResult.chunks && parseResult.chunks.length > 0;
    const hasFacts = parseResult.facts && parseResult.facts.length > 0;

    if (parseResult.errors.length > 0 && !hasFacts && !hasChunks) {
      return {
        success: false,
        error: `Failed to parse ${adapter.displayName} data`,
        details: parseResult.errors,
      };
    }

    // Dry run: report what was parsed (chunks or facts)
    if (params.dry_run) {
      if (hasChunks) {
        const totalChunks = parseResult.chunks.length;
        const EXTRACTION_RATIO = 2.5; // avg facts per chunk, from empirical data
        const BATCH_SIZE = 25;
        const SECONDS_PER_BATCH = 45; // ~30s extraction + ~15s embed+store
        const estimatedFacts = Math.round(totalChunks * EXTRACTION_RATIO);
        const estimatedBatches = Math.ceil(totalChunks / BATCH_SIZE);
        const estimatedMinutes = Math.ceil(estimatedBatches * SECONDS_PER_BATCH / 60);

        return {
          success: true,
          dry_run: true,
          import_id: importId,
          source,
          total_chunks: totalChunks,
          total_messages: parseResult.totalMessages,
          estimated_facts: estimatedFacts,
          estimated_batches: estimatedBatches,
          estimated_minutes: estimatedMinutes,
          batch_size: BATCH_SIZE,
          preview: parseResult.chunks.slice(0, 5).map((c) => ({
            title: c.title,
            messages: c.messages.length,
            first_message: c.messages[0]?.text.slice(0, 100),
          })),
          note: `Estimated ${estimatedFacts} facts from ${totalChunks} chunks (~${estimatedMinutes} min). Confirm to start background import.`,
          warnings: parseResult.warnings,
        };
      }
      return {
        success: true,
        dry_run: true,
        import_id: importId,
        source,
        total_found: parseResult.facts.length,
        preview: parseResult.facts.slice(0, 10).map((f) => ({
          type: f.type,
          text: f.text.slice(0, 100),
          importance: f.importance,
        })),
        warnings: parseResult.warnings,
      };
    }

    // ── Path 1: Conversation chunks (ChatGPT, Claude, Gemini) — background execution ──
    if (hasChunks) {
      const totalChunks = parseResult.chunks.length;
      const BATCH_SIZE = 25;
      const SECONDS_PER_BATCH = 45;
      const estimatedBatches = Math.ceil(totalChunks / BATCH_SIZE);
      const estimatedMinutes = Math.ceil(estimatedBatches * SECONDS_PER_BATCH / 60);
      const estimatedTotalFacts = Math.round(totalChunks * 2.5);
      const now = new Date();

      const initialState: ImportState = {
        import_id: importId,
        source,
        status: 'running',
        started_at: now.toISOString(),
        last_updated: now.toISOString(),
        total_chunks: totalChunks,
        total_messages: parseResult.totalMessages,
        batch_done: 0,
        batch_total: estimatedBatches,
        facts_stored: 0,
        facts_extracted: 0,
        dups_skipped: 0,
        errors: [],
        file_path: params.file_path as string | undefined,
        estimated_total_facts: estimatedTotalFacts,
        estimated_minutes: estimatedMinutes,
        estimated_completion_iso: new Date(now.getTime() + estimatedBatches * SECONDS_PER_BATCH * 1000).toISOString(),
        disclosure_confirmed: !!(params.disclosure_confirmed),
      };
      writeImportState(initialState);
      logger.info(`Import ${importId}: background task started (${totalChunks} chunks, ~${estimatedMinutes}min)`);

      void handleChunkImport(
        parseResult.chunks, parseResult.totalMessages, source, logger, startTime, parseResult.warnings, importId,
      ).catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`Import ${importId}: background task failed: ${msg}`);
        const failedState = readImportState(importId);
        if (failedState && failedState.status === 'running') {
          writeImportState({ ...failedState, status: 'failed', errors: [...failedState.errors, msg] });
        }
      });

      return {
        import_id: importId,
        status: 'running',
        source,
        total_chunks: totalChunks,
        estimated_batches: estimatedBatches,
        estimated_minutes: estimatedMinutes,
        estimated_completion_iso: initialState.estimated_completion_iso,
        message: `Import started in background. ~${estimatedMinutes} min for ${totalChunks} chunks. Check progress with \`openclaw totalreclaw import status\` on the gateway host (or \`import status --id ${importId} --json\` from an agent shell).`,
        warnings: parseResult.warnings,
      };
    }

    // ── Path 2: Pre-structured facts (Mem0, MCP Memory) — direct store ──
    const extractedFacts: ExtractedFact[] = parseResult.facts.map((f) => ({
      text: f.text,
      type: f.type,
      importance: f.importance,
      action: 'ADD' as const,
    }));

    // Store in batches of 50. Stop on any batch failure to prevent
    // nonce zombies from blocking subsequent UserOps (AA25).
    let totalStored = 0;
    let storeError: string | undefined;
    const batchSize = 50;

    for (let i = 0; i < extractedFacts.length; i += batchSize) {
      const batch = extractedFacts.slice(i, i + batchSize);
      try {
        const stored = await storeExtractedFacts(batch, logger);
        totalStored += stored;

        logger.info(
          `Import progress: ${Math.min(i + batchSize, extractedFacts.length)}/${extractedFacts.length} processed, ${totalStored} stored`,
        );
      } catch (err: unknown) {
        storeError = err instanceof Error ? err.message : String(err);
        logger.warn(`Import stopped at batch ${Math.floor(i / batchSize) + 1}: ${storeError}`);
        break; // Stop processing further batches
      }
    }

    const importWarnings = [...parseResult.warnings];
    if (storeError) {
      importWarnings.push(`Import stopped early: ${storeError}`);
    }

    return {
      success: totalStored > 0,
      source,
      import_id: importId,
      total_found: parseResult.facts.length,
      imported: totalStored,
      skipped: parseResult.facts.length - totalStored,
      stopped_early: !!storeError,
      warnings: importWarnings,
      duration_ms: Date.now() - startTime,
    };
  } catch (e) {
    const msg = e instanceof Error ? e.message : 'Unknown error';
    logger.error(`Import failed: ${msg}`);
    return { success: false, error: `Import failed: ${msg}` };
  }
}

// ---------------------------------------------------------------------------
// Smart Import — Two-Pass Pipeline (Profile + Triage)
// ---------------------------------------------------------------------------

// Lazy-load WASM for smart import functions (same pattern as crypto.ts /
// subgraph-store.ts). Goes through __cjsRequire (createRequire(import.meta.url))
// declared at the top of the file — bare `require()` is undefined under
// pure-ESM Node, see issue #124.
let _smartImportWasm: typeof import('@totalreclaw/core') | null = null;
function getSmartImportWasm() {
  if (!_smartImportWasm) _smartImportWasm = __cjsRequire('@totalreclaw/core');
  return _smartImportWasm;
}

/**
 * Check whether the @totalreclaw/core WASM module exposes smart import functions.
 * Returns false if the module is an older version without smart import support.
 */
function hasSmartImportSupport(): boolean {
  try {
    const wasm = getSmartImportWasm();
    return typeof wasm.chunksToSummaries === 'function' &&
      typeof wasm.buildProfileBatchPrompt === 'function' &&
      typeof wasm.parseProfileBatchResponse === 'function' &&
      typeof wasm.buildTriagePrompt === 'function' &&
      typeof wasm.parseTriageResponse === 'function' &&
      typeof wasm.enrichExtractionPrompt === 'function';
  } catch {
    return false;
  }
}

// SmartImportContext — extracted to ./runtime/types.ts (imported above).

/**
 * Run the smart import two-pass pipeline: profile the user from conversation
 * summaries, then triage chunks as EXTRACT or SKIP.
 *
 * All prompt construction and response parsing happens in @totalreclaw/core WASM.
 * LLM calls use the plugin's existing chatCompletion() function.
 *
 * Returns null if smart import is unavailable (old WASM, no LLM config, etc.)
 * so the caller can fall back to blind extraction.
 */
async function runSmartImportPipeline(
  chunks: import('../import-adapters/types.js').ConversationChunk[],
  logger: { info: (msg: string) => void; warn: (msg: string) => void },
): Promise<SmartImportContext | null> {
  // Guard: WASM must have smart import functions
  if (!hasSmartImportSupport()) {
    logger.info('Smart import: WASM module does not support smart import, falling back to blind extraction');
    return null;
  }

  // Guard: LLM must be available
  const llmConfig = resolveLLMConfig();
  if (!llmConfig) {
    logger.info('Smart import: no LLM available, falling back to blind extraction');
    return null;
  }

  const pipelineStart = Date.now();
  const wasm = getSmartImportWasm();

  try {
    // Step 0: Convert chunks to compact summaries (first + last message)
    const wasmChunks = chunks.map((c, i) => ({
      index: i,
      title: c.title || 'Untitled',
      messages: c.messages.map((m) => ({ role: m.role, content: m.text })),
      timestamp: c.timestamp || null,
    }));
    const summaries = wasm.chunksToSummaries(JSON.stringify(wasmChunks));
    const summariesJson = JSON.stringify(summaries);

    // Step 1: Build user profile (batch summarize -> merge)
    const PROFILE_BATCH_SIZE = 50;
    const profileStart = Date.now();
    const partials: unknown[] = [];

    for (let i = 0; i < summaries.length; i += PROFILE_BATCH_SIZE) {
      const batch = summaries.slice(i, i + PROFILE_BATCH_SIZE);
      const prompt = wasm.buildProfileBatchPrompt(JSON.stringify(batch));
      const response = await chatCompletion(llmConfig, [
        { role: 'user', content: prompt },
      ], { maxTokens: 2048, temperature: 0 });

      if (!response) {
        logger.warn(`Smart import: LLM returned empty response for profile batch ${Math.floor(i / PROFILE_BATCH_SIZE) + 1}`);
        continue;
      }

      const partial = wasm.parseProfileBatchResponse(response);
      partials.push(partial);
    }

    if (partials.length === 0) {
      logger.warn('Smart import: no profile batches produced, falling back to blind extraction');
      return null;
    }

    let profile: unknown;
    if (partials.length === 1) {
      // Single batch — skip merge, promote partial to full profile
      // parseProfileBatchResponse returns a PartialProfile; convert to UserProfile shape
      const p = partials[0] as Record<string, unknown>;
      profile = {
        identity: p.identity ?? null,
        themes: p.themes ?? [],
        projects: p.projects ?? [],
        stack: p.stack ?? [],
        decisions: p.decisions ?? [],
        interests: p.interests ?? [],
        skip_patterns: p.skip_patterns ?? [],
      };
    } else {
      const mergePrompt = wasm.buildProfileMergePrompt(JSON.stringify(partials));
      const mergeResponse = await chatCompletion(llmConfig, [
        { role: 'user', content: mergePrompt },
      ], { maxTokens: 2048, temperature: 0 });

      if (!mergeResponse) {
        logger.warn('Smart import: LLM returned empty response for profile merge, falling back to blind extraction');
        return null;
      }

      profile = wasm.parseProfileResponse(mergeResponse);
    }

    const profileJson = JSON.stringify(profile);
    const profileDuration = Date.now() - profileStart;

    const p = profile as Record<string, unknown>;
    const themeCount = Array.isArray(p.themes) ? p.themes.length : 0;
    const skipPatternCount = Array.isArray(p.skip_patterns) ? p.skip_patterns.length : 0;
    logger.info(
      `Smart import: profile built in ${profileDuration}ms (themes=${themeCount}, skip_patterns=${skipPatternCount})`,
    );

    // Step 1.5: Chunk triage (EXTRACT or SKIP)
    const triageStart = Date.now();
    const allDecisions: Array<{ chunk_index: number; decision: string; reason: string }> = [];
    const TRIAGE_BATCH_SIZE = 50;

    for (let i = 0; i < summaries.length; i += TRIAGE_BATCH_SIZE) {
      const batch = summaries.slice(i, i + TRIAGE_BATCH_SIZE);
      const triagePrompt = wasm.buildTriagePrompt(profileJson, JSON.stringify(batch));
      const triageResponse = await chatCompletion(llmConfig, [
        { role: 'user', content: triagePrompt },
      ], { maxTokens: 4096, temperature: 0 });

      if (!triageResponse) {
        logger.warn(`Smart import: LLM returned empty response for triage batch ${Math.floor(i / TRIAGE_BATCH_SIZE) + 1}, defaulting to EXTRACT`);
        // Default all chunks in this batch to EXTRACT
        for (let j = i; j < Math.min(i + TRIAGE_BATCH_SIZE, summaries.length); j++) {
          allDecisions.push({ chunk_index: j, decision: 'EXTRACT', reason: 'triage LLM unavailable' });
        }
        continue;
      }

      const batchDecisions = wasm.parseTriageResponse(triageResponse) as Array<{
        chunk_index: number;
        decision: string;
        reason: string;
      }>;
      allDecisions.push(...batchDecisions);
    }

    const triageDuration = Date.now() - triageStart;

    const extractCount = allDecisions.filter((d) => d.decision !== 'SKIP').length;
    const skipCount = allDecisions.filter((d) => d.decision === 'SKIP').length;
    logger.info(
      `Smart import: triage complete in ${triageDuration}ms (extract=${extractCount}, skip=${skipCount}, total=${chunks.length})`,
    );

    // Step 2: Build enriched system prompt for extraction
    const enrichedSystemPrompt = wasm.enrichExtractionPrompt(profileJson, EXTRACTION_SYSTEM_PROMPT);

    const totalDuration = Date.now() - pipelineStart;
    logger.info(`Smart import: pipeline complete in ${totalDuration}ms`);

    return {
      profileJson,
      decisions: allDecisions,
      enrichedSystemPrompt,
      extractCount,
      skipCount,
      durationMs: totalDuration,
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Smart import: pipeline failed (${msg}), falling back to blind extraction`);
    return null;
  }
}

/**
 * Check if a chunk should be skipped based on triage decisions.
 * If no decision exists for the chunk index, defaults to EXTRACT (safe default).
 */
function isChunkSkipped(
  chunkIndex: number,
  decisions: Array<{ chunk_index: number; decision: string }>,
): { skipped: boolean; reason: string } {
  const decision = decisions.find((d) => d.chunk_index === chunkIndex);
  if (decision && decision.decision === 'SKIP') {
    return { skipped: true, reason: (decision as { reason?: string }).reason || 'triage: skip' };
  }
  return { skipped: false, reason: '' };
}

/**
 * Process a batch (slice) of conversation chunks from a file.
 * Called repeatedly by the agent for large imports.
 */
async function handleBatchImport(
  params: Record<string, unknown>,
  logger: OpenClawPluginApi['logger'],
): Promise<Record<string, unknown>> {
  _importInProgress = true;
  const source = params.source as string;
  const filePath = params.file_path as string | undefined;
  const content = params.content as string | undefined;
  const offset = (params.offset as number) ?? 0;
  const batchSize = (params.batch_size as number) ?? 25;

  const validSources = ['mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini'];
  if (!source || !validSources.includes(source)) {
    return { success: false, error: `Invalid source. Must be one of: ${validSources.join(', ')}` };
  }

  const startTime = Date.now();

  const { getAdapter } = await import('../import-adapters/index.js');
  const adapter = getAdapter(source as import('../import-adapters/types.js').ImportSource);

  const parseResult = await adapter.parse({ content, file_path: filePath });

  if (parseResult.errors.length > 0 && parseResult.chunks.length === 0) {
    return { success: false, error: parseResult.errors.join('; ') };
  }

  const totalChunks = parseResult.chunks.length;
  const slice = parseResult.chunks.slice(offset, offset + batchSize);
  const remaining = Math.max(0, totalChunks - offset - slice.length);

  // --- Smart Import: Profile + Triage ---
  // Build profile from ALL chunks (not just the slice) for full context,
  // then triage only the current slice. For simplicity, we rebuild on every
  // batch call — optimization (caching) can come later.
  const smartCtx = await runSmartImportPipeline(parseResult.chunks, logger);
  let chunksSkipped = 0;

  // Process the slice through the normal extraction + storage pipeline.
  // If a batch fails (nonce zombie, quota exceeded, etc.), stop immediately
  // to prevent subsequent UserOps from hitting AA25 nonce conflicts.
  let factsExtracted = 0;
  let factsStored = 0;
  let chunksProcessed = 0;
  let storeError: string | undefined;

  for (let i = 0; i < slice.length; i++) {
    const chunk = slice[i];
    const globalIndex = offset + i; // Index in the full chunks array

    // Smart import: skip chunks triaged as SKIP
    if (smartCtx) {
      const { skipped, reason } = isChunkSkipped(globalIndex, smartCtx.decisions);
      if (skipped) {
        logger.info(`Import: skipping chunk ${globalIndex + 1}/${totalChunks}: "${chunk.title}" (${reason})`);
        chunksSkipped++;
        chunksProcessed++;
        continue;
      }
    }

    logger.info(`Import: extracting facts from chunk ${globalIndex + 1}/${totalChunks}: "${chunk.title}"`);

    const messages = chunk.messages.map((m) => ({ role: m.role, content: m.text }));
    const facts = await extractFacts(
      messages,
      'full',
      undefined, // no existing memories for dedup during import
      smartCtx?.enrichedSystemPrompt, // profile-enriched extraction prompt
    );
    chunksProcessed++;

    if (facts.length > 0) {
      factsExtracted += facts.length;
      try {
        const stored = await storeExtractedFacts(facts, logger);
        factsStored += stored;
      } catch (err: unknown) {
        storeError = err instanceof Error ? err.message : String(err);
        logger.warn(`Import batch stopped at chunk ${globalIndex + 1}/${totalChunks}: ${storeError}`);
        break; // Stop processing further chunks — a zombie UserOp may block writes
      }
    }
  }

  return {
    success: factsStored > 0 || (!storeError && factsExtracted === 0),
    batch_offset: offset,
    batch_size: chunksProcessed,
    total_chunks: totalChunks,
    facts_extracted: factsExtracted,
    facts_stored: factsStored,
    chunks_skipped: chunksSkipped,
    remaining_chunks: remaining,
    is_complete: remaining === 0 && !storeError,
    stopped_early: !!storeError,
    error: storeError,
    smart_import: smartCtx ? {
      profile_duration_ms: smartCtx.durationMs,
      extract_count: smartCtx.extractCount,
      skip_count: smartCtx.skipCount,
    } : null,
    // Estimation for the full import
    estimated_total_facts: Math.round(totalChunks * 2.5),
    estimated_total_userops: Math.ceil(totalChunks * 2.5 / 15),
    estimated_minutes: Math.ceil(Math.ceil(totalChunks / batchSize) * 45 / 60),
    duration_ms: Date.now() - startTime,
  };
}

/**
 * Process conversation chunks through LLM extraction and store results.
 *
 * Each chunk is passed to extractFacts() — the same extraction pipeline used
 * for auto-extraction during live conversations. This ensures import quality
 * matches conversation extraction quality.
 */
async function handleChunkImport(
  chunks: import('../import-adapters/types.js').ConversationChunk[],
  totalMessages: number,
  source: string,
  logger: OpenClawPluginApi['logger'],
  startTime: number,
  warnings: string[],
  importId?: string,
): Promise<Record<string, unknown>> {
  let totalExtracted = 0;
  let totalStored = 0;
  let chunksProcessed = 0;
  let chunksSkipped = 0;
  const resolvedImportId = importId ?? crypto.randomUUID();

  let storeError: string | undefined;

  // --- Smart Import: Profile + Triage ---
  const smartCtx = await runSmartImportPipeline(chunks, logger);

  const CHECKPOINT_EVERY = 25; // write state file every N chunks

  for (let i = 0; i < chunks.length; i++) {
    // Check abort flag from state file before each chunk (background task may be cancelled).
    if (importId) {
      const currentState = readImportState(importId);
      if (currentState?.status === 'aborted') {
        logger.info(`Import ${importId}: abort flag detected at chunk ${i + 1}/${chunks.length}, stopping`);
        break;
      }
    }

    const chunk = chunks[i];
    chunksProcessed++;

    // Smart import: skip chunks triaged as SKIP
    if (smartCtx) {
      const { skipped, reason } = isChunkSkipped(i, smartCtx.decisions);
      if (skipped) {
        logger.info(
          `Import: skipping chunk ${chunksProcessed}/${chunks.length}: "${chunk.title}" (${reason})`,
        );
        chunksSkipped++;
        continue;
      }
    }

    logger.info(
      `Import: extracting facts from chunk ${chunksProcessed}/${chunks.length}: "${chunk.title}"`,
    );

    // Convert chunk messages to the format extractFacts() expects.
    // extractFacts() takes an array of message-like objects with { role, content }.
    const messages = chunk.messages.map((m) => ({
      role: m.role,
      content: m.text,
    }));

    // Use 'full' mode to extract ALL valuable memories from the chunk
    // (not just the last few messages like 'turn' mode does).
    // Smart import: pass enriched system prompt with user profile context.
    const facts = await extractFacts(
      messages,
      'full',
      undefined, // no existing memories for dedup during import
      smartCtx?.enrichedSystemPrompt, // profile-enriched extraction prompt
    );

    if (facts.length > 0) {
      totalExtracted += facts.length;

      try {
        // Store through the normal pipeline (dedup, encrypt, store).
        // storeExtractedFacts throws on batch failure to prevent nonce zombies.
        const stored = await storeExtractedFacts(facts, logger);
        totalStored += stored;

        logger.info(
          `Import chunk ${chunksProcessed}/${chunks.length}: extracted ${facts.length} facts, stored ${stored}`,
        );
      } catch (err: unknown) {
        storeError = err instanceof Error ? err.message : String(err);
        logger.warn(`Import stopped at chunk ${chunksProcessed}/${chunks.length}: ${storeError}`);
        break; // Stop processing further chunks — a zombie UserOp may block writes
      }
    }

    // Checkpoint state file periodically so _import_status reflects live progress.
    if (importId && chunksProcessed % CHECKPOINT_EVERY === 0) {
      const liveState = readImportState(importId);
      if (liveState) {
        const estimatedBatches = liveState.batch_total || 1;
        const doneBatches = Math.floor(chunksProcessed / CHECKPOINT_EVERY);
        const elapsed = Date.now() - new Date(liveState.started_at).getTime();
        const secPerBatch = doneBatches > 0 ? elapsed / 1000 / doneBatches : 45;
        const remaining = estimatedBatches - doneBatches;
        const etaMs = remaining * secPerBatch * 1000;
        writeImportState({
          ...liveState,
          batch_done: doneBatches,
          facts_stored: totalStored,
          facts_extracted: totalExtracted,
          estimated_completion_iso: new Date(Date.now() + etaMs).toISOString(),
        });
      }
    }
  }

  if (totalExtracted === 0 && chunks.length > 0 && !storeError && chunksSkipped < chunks.length) {
    warnings.push(
      `Processed ${chunks.length} conversation chunks (${totalMessages} messages) but the LLM ` +
      `did not extract any facts worth storing. This can happen if the conversations are mostly ` +
      `generic/ephemeral content without personal facts, preferences, or decisions.`,
    );
  }

  if (storeError) {
    warnings.push(`Import stopped early: ${storeError}. ${chunks.length - chunksProcessed} chunk(s) not processed.`);
  }

  // Final state file write for background imports.
  if (importId) {
    const finalState = readImportState(importId);
    if (finalState) {
      const finalStatus = storeError ? 'failed' : (finalState.status === 'aborted' ? 'aborted' : 'completed');
      writeImportState({
        ...finalState,
        status: finalStatus,
        batch_done: finalState.batch_total,
        facts_stored: totalStored,
        facts_extracted: totalExtracted,
        errors: storeError ? [...finalState.errors, storeError] : finalState.errors,
      });
    }
    _importInProgress = false;
  }

  return {
    success: totalStored > 0 || totalExtracted > 0,
    source,
    import_id: resolvedImportId,
    total_chunks: chunks.length,
    chunks_processed: chunksProcessed,
    chunks_skipped: chunksSkipped,
    total_messages: totalMessages,
    facts_extracted: totalExtracted,
    imported: totalStored,
    skipped: totalExtracted - totalStored,
    stopped_early: !!storeError,
    smart_import: smartCtx ? {
      profile_duration_ms: smartCtx.durationMs,
      extract_count: smartCtx.extractCount,
      skip_count: smartCtx.skipCount,
    } : null,
    warnings,
    duration_ms: Date.now() - startTime,
  };
}

// ---------------------------------------------------------------------------
// Import status + abort handlers
// ---------------------------------------------------------------------------

export async function handleImportStatus(
  params: Record<string, unknown>,
  logger: OpenClawPluginApi['logger'],
): Promise<Record<string, unknown>> {
  const importId = params.import_id as string | undefined;

  let state: ImportState | null;
  if (importId) {
    state = readImportState(importId);
    if (!state) return { error: `No import found with id: ${importId}` };
  } else {
    state = readMostRecentActiveImport();
    if (!state) return { status: 'no_active_import', message: 'No active import found. Start one with `openclaw totalreclaw import from <source>` on the gateway host. (Auto-resume still picks up running imports on gateway restart.)' };
  }

  // 1h freshness guard: mark stale imports as failed and prompt user to resume.
  if (state.status === 'running' && isImportStale(state)) {
    writeImportState({ ...state, status: 'failed', errors: [...state.errors, 'Stale: no progress in 1h'] });
    logger.info(`Import ${state.import_id}: marked stale (no progress in 1h)`);
    return {
      import_id: state.import_id,
      status: 'failed',
      stale: true,
      facts_stored: state.facts_stored,
      message: 'Import appears stale — no progress in 1 hour. Resume it with `openclaw totalreclaw import from <source> --file <path> --resume ' + state.import_id + '` on the gateway host, or restart the gateway to trigger auto-resume.',
      resume_id: state.import_id,
    };
  }

  const now = Date.now();
  const elapsedMs = now - new Date(state.started_at).getTime();
  const secPerBatch = state.batch_done > 0 ? elapsedMs / 1000 / state.batch_done : 45;
  const remaining = Math.max(0, state.batch_total - state.batch_done);
  const etaSeconds = state.status === 'running' ? Math.round(remaining * secPerBatch) : 0;

  return {
    import_id: state.import_id,
    status: state.status,
    batch_done: state.batch_done,
    batch_total: state.batch_total,
    facts_stored: state.facts_stored,
    dups_skipped: state.dups_skipped,
    eta_seconds: etaSeconds,
    completion_iso: state.status === 'running'
      ? new Date(now + etaSeconds * 1000).toISOString()
      : state.last_updated,
    source: state.source,
    started_at: state.started_at,
    errors: state.errors,
  };
}

export async function handleImportAbort(
  params: Record<string, unknown>,
  logger: OpenClawPluginApi['logger'],
): Promise<Record<string, unknown>> {
  const importId = params.import_id as string | undefined;
  if (!importId) return { error: 'import_id is required' };

  const state = readImportState(importId);
  if (!state) return { error: `No import found with id: ${importId}` };

  if (state.status === 'aborted') {
    return { aborted: true, idempotent: true, import_id: importId, facts_already_stored: state.facts_stored };
  }
  if (state.status === 'completed') {
    return { error: 'Import already completed — nothing to abort', import_id: importId, facts_stored: state.facts_stored };
  }

  writeImportState({ ...state, status: 'aborted' });
  logger.info(`Import ${importId}: abort requested (${state.facts_stored} facts already stored)`);

  return {
    aborted: true,
    import_id: importId,
    facts_already_stored: state.facts_stored,
    message: 'Import abort requested. The background task will stop at the next chunk boundary. Already-stored facts are kept.',
  };
}
