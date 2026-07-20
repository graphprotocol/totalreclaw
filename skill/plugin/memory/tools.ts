/**
 * tools — the agent-facing memory_search / memory_get tool factories.
 *
 * Task 2.6 of the OpenClaw native integration plan
 * (docs/plans/2026-06-21-openclaw-native-integration-plan.md, 2026-06-21).
 *
 * WHY THIS FILE EXISTS:
 *   OpenClaw's `active-memory` sub-agent and the host's memory subsystem
 *   call two tools by name: `memory_search` and `memory_get`. The bundled
 *   memory-core registers them via
 *     api.registerTool((ctx) => createLazyMemorySearchTool(
 *        resolveMemoryToolOptions(ctx)), { names: ["memory_search"] })
 *   and the matching Get call. For TR to be the memory backend, TR must
 *   register tools with the SAME names + parameter schema + return shape
 *   that route into TR's encrypted-vault pipeline instead of memory-core's
 *   file backend.
 *
 *   These factories produce those tools. They delegate to the TR
 *   MemorySearchManager obtained from the TR MemoryPluginRuntime (Task 2.1 /
 *   2.3 — `createTrMemoryPluginRuntime`), which synthesizes file-shaped
 *   results from decrypted facts so the agent sees a familiar memory corpus.
 *
 * MEMORY-CORE'S PATTERN (what this mirrors):
 *   Reverse-engineered from OpenClaw 2026.6.8's bundled memory-core at
 *     /tmp/tr-openclaw-probe/node_modules/openclaw/dist/tools-CT_OGlM3.js
 *     (createMemorySearchTool l.437, createMemoryGetTool l.631)
 *   and the memory-core registration at
 *     dist/extensions/memory-core/index.js (l.273-274).
 *
 *   Tool name + label + description: cribbed verbatim from memory-core so
 *   the active-memory sub-agent treats TR's tools as drop-in.
 *   Parameter schema: field-for-field match with memory-core's
 *     MemorySearchSchema = { query, maxResults?, minScore?, corpus? }
 *     MemoryGetSchema    = { path, from?, lines?, corpus? }
 *   Return shape:
 *     search  -> { results: MemorySearchResult[], provider?, mode?, debug? }
 *     get     -> { path, text, truncated?, from?, lines?, nextFrom? }
 *     unavailable -> { disabled:true, unavailable:true, error, warning, action }
 *       (mirrors memory-core's buildMemorySearchUnavailableResult so the
 *       agent surfaces the same warning/action guidance to the user).
 *
 * CAPTURED-RUNTIME DESIGN (vs ctx-resolved):
 *   memory-core resolves the manager from `ctx` via an internal helper
 *   `getMemoryManagerContextWithPurpose({cfg, agentId})` which calls
 *   `loadMemoryToolRuntime()` — a private import from a hashed dist chunk
 *   (`./tools.runtime.js`) that only bundled plugins can reach. A
 *   third-party plugin (TR) cannot import that chunk and cannot reach the
 *   memory runtime via `ctx`.
 *
 *   TR solves this by capturing the runtime at register() time. Task 2.7's
 *   register() creates the TR MemoryPluginRuntime via
 *   `createTrMemoryPluginRuntime(deps)` and hands it to BOTH:
 *     - api.registerMemoryCapability({ runtime, ... })   (the host surface)
 *     - createMemorySearchTool(runtime) / createMemoryGetTool(runtime)
 *       (the tool surface, captured here)
 *   so the tool handler calls `runtime.getMemorySearchManager(...)` to get
 *   the manager — the SAME surface the host calls. This is the sanctioned
 *   deviation; it is documented here so Task 2.7 knows the wiring.
 *
 *   What TR does NOT mirror from memory-core (and why):
 *     - corpus=wiki / corpus=all supplements: memory-core-only feature
 *       (registered compiled-wiki supplements). TR has no supplements; the
 *       param is accepted for schema parity but treated as memory-only.
 *     - qmd backend / dreaming / citations-mode: memory-core-only. TR's
 *       adapter is its own backend.
 *     - cooldown tracking on unavailable: memory-core caches "unavailable"
 *       for N seconds to avoid hammering a broken embedder. TR's adapter
 *       reaches the manager via a cheap closure (no embedder spin-up on
 *       each call), so the cooldown is not needed at this layer; the
 *       underlying recall pipeline already debounces.
 *
 * SCANNER-CLEAN HARD CONTRACT (env=N net=N):
 *   This file is pure orchestration. It touches NO host environment state
 *   and performs NO outbound network I/O. The manager arrives via the
 *   injected runtime; the tool handler only awaits its methods.
 *
 *   The TR adapter's content-read method is `readFile` (its canonical name).
 *   It is called here by that literal name — the scanner's per-file exfil
 *   rule requires env+net token co-occurrence to flag, and this file has no
 *   network token, so the bare method name is clean. (Earlier revisions
 *   obscured the name behind a `readContent`/`readFile` indirection on a
 *   mistaken belief that the name alone was a trigger; that indirection was
 *   dead code and has been removed.)
 *
 *   `npm run check-scanner` must remain 0 flags.
 *
 *   `npm run build` uses `--noCheck`, so structural/loose typing where
 *   OpenClaw's types aren't importable is safe (the plugin does not depend
 *   on OpenClaw's type package).
 */

// ---------------------------------------------------------------------------
// Imports — taxonomy types + the entity validator from extractor.ts so the
// memory_save schema/forwarding is typed + validated against the single source
// of truth. Type-only imports are erased at build (mirrors memory/pin.ts:26);
// parseEntity/VALID_MEMORY_TYPES/VALID_MEMORY_SCOPES are pure constants/funcs
// with no env or net token, so this file's scanner-clean posture is unchanged
// (memory-runtime.ts already makes the same runtime import — see its l.68).
// ---------------------------------------------------------------------------
import {
  parseEntity,
  VALID_MEMORY_TYPES,
  VALID_MEMORY_SCOPES,
} from '../extraction/extractor.js';
import type {
  MemoryType,
  MemoryScope,
  ExtractedEntity,
} from '../extraction/extractor.js';
import type { TrMemorySaveFn, TrMemorySaveInput } from './memory-runtime.js';

// ---------------------------------------------------------------------------
// Types — loose on purpose. The plugin does not import OpenClaw's type
// package; the tool object returned is STRUCTURALLY compatible with
// OpenClaw's AnyAgentTool at runtime (same field names, same execute arity,
// same AgentToolResult shape). See dist/common-BYJ5YAFM.d.ts l.20 +
// dist/index-CB3EOAcX.d.ts l.406/422 for the canonical shapes.
// ---------------------------------------------------------------------------

/**
 * A search result hit surfaced to the agent. Mirrors memory-core's
 * MemorySearchResult — the agent parses this shape out of the JSON payload.
 * TR's TrMemorySearchManager already synthesizes hits in exactly this shape
 * (Task 2.1), so the tool just forwards what the manager returns.
 */
export interface MemorySearchResult {
  path: string;
  startLine: number;
  endLine: number;
  score: number;
  snippet: string;
  source: string;
  citation?: string;
  [k: string]: unknown;
}

/**
 * A read result returned by memory_get. Mirrors memory-core's
 * executeMemoryReadResult payload — { path, text, truncated?, from?, lines?,
 * nextFrom? } on success; { path, text:"", disabled:true, error } on failure.
 */
export interface MemoryGetResult {
  path: string;
  text: string;
  truncated?: boolean;
  from?: number;
  lines?: number;
  nextFrom?: number;
  [k: string]: unknown;
}

/**
 * The TR MemoryPluginRuntime surface these tools need. Exactly the shape
 * Task 2.1 / 2.3's `createTrMemoryPluginRuntime` returns. Kept loose so
 * tools.ts does not import memory-runtime.ts (avoids a cycle and keeps the
 * tool factories independently testable).
 */
export interface TrMemoryPluginRuntimeLike {
  getMemorySearchManager(params: {
    cfg?: unknown;
    agentId?: string;
    purpose?: string;
  }): Promise<{
    manager: TrMemorySearchManagerLike | null;
    error?: string;
  }>;
}

/**
 * The subset of the TR MemorySearchManager these tools call. Methods the
 * tools do not use (status / probes / close) are omitted so the loose type
 * stays minimal. The content-read method is named the same as on the real
 * adapter (`readFile`) — this file is pure orchestration with no network
 * token, so the literal method name does not trip the scanner's per-file
 * exfil rule (which requires env+net co-occurrence).
 */
export interface TrMemorySearchManagerLike {
  search(
    query: string,
    opts?: {
      maxResults?: number;
      minScore?: number;
      signal?: AbortSignal;
      sessionKey?: string;
    },
  ): Promise<MemorySearchResult[]>;
  readFile(p: {
    relPath: string;
    from?: number;
    lines?: number;
  }): Promise<MemoryGetResult>;
}

/**
 * An AgentToolResult. `{ content: [{type:'text', text: JSON.stringify(payload)}] }`
 * — the agent parses the JSON in `text`. This matches memory-core's
 * `jsonResult(payload)` helper (dist/common-BYJ5YAFM.d.ts l.103) and the
 * shape the retired TR agent tools used to return (the convention is kept
 * for the native memory_search/memory_get wrappers).
 */
export interface AgentToolResultLike {
  content: Array<{ type: 'text'; text: string }>;
}

/**
 * The full tool object these factories return. Structurally compatible with
 * OpenClaw's AnyAgentTool: name + description + parameters (the Tool base,
 * dist/types-Boa_mcGH.d.ts l.216) + label + execute (AgentTool,
 * dist/index-CB3EOAcX.d.ts l.422). Task 2.7 passes this to
 * `api.registerTool(() => createMemorySearchTool(runtime), { names:[...] })`.
 */
export interface AgentToolLike {
  name: string;
  label: string;
  description: string;
  parameters: object;
  execute: (
    toolCallId: string,
    params: unknown,
    signal?: AbortSignal,
    onUpdate?: unknown,
  ) => Promise<AgentToolResultLike>;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build an AgentToolResult whose content text is the JSON-encoded payload.
 * Mirrors memory-core's `jsonResult` helper exactly. The agent reads
 * `content[0].text` and JSON-parses it.
 */
function jsonResult(payload: unknown): AgentToolResultLike {
  return {
    content: [{ type: 'text', text: JSON.stringify(payload) }],
  };
}

/**
 * Build the unavailable-result payload. Mirrors memory-core's
 * buildMemorySearchUnavailableResult (dist/tools-CT_OGlM3.js l.219): the
 * payload carries `disabled:true` + `unavailable:true` (the signals
 * active-memory keys off to tell the user memory is down) PLUS `warning`
 * and `action` guidance the agent forwards to the user.
 *
 * The warning/action wording is TR-adapted (TR has no embedding-quota state
 * at this layer; the error string is whatever the runtime/pipeline surfaced).
 */
function buildUnavailableResult(error: string | undefined): {
  disabled: true;
  unavailable: true;
  error: string;
  warning: string;
  action: string;
} {
  const reason = (error ?? 'memory search unavailable').trim() || 'memory search unavailable';
  return {
    disabled: true,
    unavailable: true,
    error: reason,
    warning: 'Memory recall is unavailable. You should tell the user that prior memories cannot be retrieved right now.',
    action: 'Let the user know memory recall is unavailable and suggest they retry or check their TotalReclaw pairing.',
  };
}

/**
 * Read a string param from a loosely-typed args object. Mirrors memory-core's
 * readStringParam contract (required + optional variants). Throws a plain
 * Error on missing-required; memory-core throws ToolInputError but the tool
 * handler catches and converts any thrown error into a disabled result, so
 * the error class does not matter at this boundary.
 */
function readStringParam(
  params: Record<string, unknown>,
  key: string,
  opts: { required?: true } = {},
): string | undefined {
  const v = params[key];
  if (typeof v === 'string') return v;
  if (opts.required === true) {
    throw new Error(`missing required string parameter: ${key}`);
  }
  return undefined;
}

/**
 * Read a positive-integer param. Mirrors memory-core's
 * readPositiveIntegerParam. Returns undefined when absent/invalid (the
 * manager applies its own default).
 */
function readPositiveIntegerParam(
  params: Record<string, unknown>,
  key: string,
): number | undefined {
  const v = params[key];
  if (typeof v === 'number' && Number.isInteger(v) && v >= 1) return v;
  return undefined;
}

/**
 * Read a finite-number param. Mirrors memory-core's readFiniteNumberParam
 * (used for minScore).
 */
function readFiniteNumberParam(params: Record<string, unknown>, key: string): number | undefined {
  const v = params[key];
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  return undefined;
}

/**
 * Read a string param that must be one of `allowed`. Returns undefined for
 * absent OR invalid values (the memory_save tool drops invalid optionals
 * rather than forwarding garbage — the store closure applies the sane default).
 */
function readStringEnum(
  params: Record<string, unknown>,
  key: string,
  allowed: readonly string[],
): string | undefined {
  const v = params[key];
  if (typeof v === 'string' && allowed.includes(v)) return v;
  return undefined;
}

/**
 * Coerce the unknown tool-params arg into a record. The agent emits a plain
 * object; this is a defensive cast that also tolerates null/undefined.
 */
function asParamsRecord(params: unknown): Record<string, unknown> {
  return (params ?? {}) as Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// createMemorySearchTool
// ---------------------------------------------------------------------------

/**
 * The memory_search tool factory. Returns a tool object structurally
 * compatible with OpenClaw's AnyAgentTool, delegating to the TR
 * MemorySearchManager obtained from the captured runtime.
 *
 * Wiring (Task 2.7):
 *   api.registerTool(() => createMemorySearchTool(runtime), { names: ["memory_search"] });
 *
 * Handler contract:
 *   - Resolves the manager via `runtime.getMemorySearchManager({purpose:"search"})`.
 *   - On { manager: null, error }: returns an unavailable result (disabled:true).
 *   - On a thrown manager.search error: returns an unavailable result.
 *   - On success: returns the manager's results wrapped in an AgentToolResult
 *     whose text payload is `{ results, provider, mode }` (memory-core shape).
 *
 * @param runtime  the TR MemoryPluginRuntime (captured at register() time)
 */
export function createMemorySearchTool(runtime: TrMemoryPluginRuntimeLike): AgentToolLike {
  return {
    name: 'memory_search',
    label: 'Memory Search',
    // Description is adapted from memory-core's to reflect TR's encrypted
    // vault model (the agent calls memory_search which decrypts on the fly;
    // it never sees files). The `corpus` param is accepted for schema parity
    // but TR has no wiki/sessions supplements — only memory.
    description:
      'Mandatory recall step: search the user’s encrypted TotalReclaw memory vault ' +
      'before answering questions about prior work, decisions, dates, people, ' +
      'preferences, or todos. Returns matching memories with citations you can ' +
      'deref via memory_get. If the response has disabled=true, memory recall ' +
      'is unavailable; tell the user and include the warning/action guidance.',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'The recall query.' },
        maxResults: { type: 'integer', minimum: 1, description: 'Cap on returned hits.' },
        minScore: { type: 'number', description: 'Drop hits below this score.' },
        corpus: {
          type: 'string',
          enum: ['memory'],
          description: 'TR only supports the memory corpus.',
        },
      },
      required: ['query'],
      additionalProperties: false,
    },
    execute: async (_toolCallId, params) => {
      const raw = asParamsRecord(params);
      // query is required — readStringParam throws on missing, caught below.
      let query: string;
      try {
        const q = readStringParam(raw, 'query', { required: true });
        if (q === undefined) throw new Error('query is required');
        query = q;
      } catch (e) {
        // A missing required param is a caller error — surface it as a
        // disabled result so the agent sees structured feedback rather than
        // a thrown exception out of the tool boundary.
        const msg = e instanceof Error ? e.message : String(e);
        return jsonResult(buildUnavailableResult(msg));
      }
      const maxResults = readPositiveIntegerParam(raw, 'maxResults');
      const minScore = readFiniteNumberParam(raw, 'minScore');

      let resolved;
      try {
        // NOTE: memory-core passes `undefined` here normally (and `"cli"` for
        // one-shot CLI runs); TR's runtime currently ignores `purpose`, so
        // this is a no-op today. Kept as "search" for parity with the
        // memory-core shape; flagged for correctness if Task 2.7 ever keys
        // behavior off `purpose`.
        resolved = await runtime.getMemorySearchManager({ purpose: 'search' });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        return jsonResult(buildUnavailableResult(msg));
      }
      if (!resolved.manager) {
        return jsonResult(buildUnavailableResult(resolved.error));
      }

      try {
        const results = await resolved.manager.search(query, {
          ...(maxResults !== undefined ? { maxResults } : {}),
          ...(minScore !== undefined ? { minScore } : {}),
        });
        // Payload shape mirrors memory-core: { results, provider?, mode? }.
        // TR's adapter is its own backend; we report provider:'totalreclaw'
        // so the agent's diagnostics attribute hits correctly.
        return jsonResult({
          results,
          provider: 'totalreclaw',
          mode: 'builtin',
        });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        return jsonResult(buildUnavailableResult(msg));
      }
    },
  };
}

// ---------------------------------------------------------------------------
// createMemoryGetTool
// ---------------------------------------------------------------------------

/**
 * The memory_get tool factory. Returns a tool object structurally compatible
 * with OpenClaw's AnyAgentTool, delegating to the TR MemorySearchManager's
 * content-read method.
 *
 * Wiring (Task 2.7):
 *   api.registerTool(() => createMemoryGetTool(runtime), { names: ["memory_get"] });
 *
 * Handler contract:
 *   - Resolves the manager via `runtime.getMemorySearchManager({purpose:"status"})`.
 *     (memory-core uses purpose:"status" for reads; we match that.)
 *   - On { manager: null, error }: returns a disabled result keyed on the
 *     requested path so the agent can correlate.
 *   - On a thrown read error: returns a disabled result keyed on the path.
 *   - On success: forwards the manager's read result in an AgentToolResult.
 *
 * The read is a direct `manager.readFile(...)` call — `readFile` is the
 * canonical method name on the TR adapter (TrMemorySearchManager). This file
 * holds no network token, so the literal method name does not trip the
 * scanner's per-file exfil rule (which requires env+net co-occurrence).
 *
 * @param runtime  the TR MemoryPluginRuntime (captured at register() time)
 */
export function createMemoryGetTool(runtime: TrMemoryPluginRuntimeLike): AgentToolLike {
  return {
    name: 'memory_get',
    label: 'Memory Get',
    description:
      'Read a single memory in full from the user’s encrypted TotalReclaw memory ' +
      'vault by its citation/path (as returned by memory_search). Supports ' +
      'optional from/lines pagination for large memories. Use this to pull the ' +
      'full text of a hit you found via memory_search. If the response has ' +
      'disabled=true, the read failed; surface the error to the user.',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'The memory citation/path to dereference.' },
        from: { type: 'integer', minimum: 1, description: '1-indexed start line.' },
        lines: { type: 'integer', minimum: 1, description: 'Max lines to return.' },
        corpus: {
          type: 'string',
          enum: ['memory'],
          description: 'TR only supports the memory corpus.',
        },
      },
      required: ['path'],
      additionalProperties: false,
    },
    execute: async (_toolCallId, params) => {
      const raw = asParamsRecord(params);
      let relPath: string;
      try {
        const p = readStringParam(raw, 'path', { required: true });
        if (p === undefined) throw new Error('path is required');
        relPath = p;
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        return jsonResult({ path: '', text: '', disabled: true, error: msg });
      }
      const from = readPositiveIntegerParam(raw, 'from');
      const lines = readPositiveIntegerParam(raw, 'lines');

      let resolved;
      try {
        resolved = await runtime.getMemorySearchManager({ purpose: 'status' });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        return jsonResult({ path: relPath, text: '', disabled: true, error: msg });
      }
      if (!resolved.manager) {
        return jsonResult({
          path: relPath,
          text: '',
          disabled: true,
          error: resolved.error ?? 'memory search unavailable',
        });
      }

      try {
        const result = await resolved.manager.readFile({
          relPath,
          ...(from !== undefined ? { from } : {}),
          ...(lines !== undefined ? { lines } : {}),
        });
        return jsonResult(result);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        return jsonResult({ path: relPath, text: '', disabled: true, error: msg });
      }
    },
  };
}

// ---------------------------------------------------------------------------
// createMemorySaveTool (internal#499)
// ---------------------------------------------------------------------------

/**
 * The memory_save tool factory. Returns a tool object structurally compatible
 * with OpenClaw's AnyAgentTool that persists ONE explicitly-remembered fact
 * through the SAME store path extraction/import use (storeExtractedFacts).
 *
 * WHY THIS TOOL EXISTS:
 *   The plugin registered only `memory_search` + `memory_get` — no write tool.
 *   So an explicit "remember X" had no agent-driven store path: the agent
 *   shelled out to `tr remember "X"` (GNU coreutils `tr`, NOT a TotalReclaw
 *   CLI), got no output, and reported "Saved" — silent data loss on the exact
 *   user intent the plugin exists to serve. memory_save closes that gap with a
 *   synchronous write that returns a truthful ok/stored the agent reports
 *   verbatim (no fabrication).
 *
 * CAPTURED-STORE DESIGN (vs captured-runtime):
 *   memory_search/memory_get capture the READ `runtime` (the
 *   MemorySearchManager surface). The write path is NOT on that runtime — it
 *   is the storeExtractedFacts pipeline, which closes over unexported index.ts
 *   state (encryptionKey/dedupKey/authKeyHex/apiClient). So memory_save
 *   captures a `store` closure instead, bound to that pipeline in index.ts's
 *   buildRecallDeps (exactly how recall/getById bind the read pipeline). The
 *   closure — NOT this tool — applies domain defaults (type → 'claim',
 *   importance → 8, source → 'user') and constructs the canonical ExtractedFact;
 *   this tool only validates + forwards, so it stays scanner-trivial.
 *
 * TRUTHFULNESS CONTRACT (the bug fix):
 *   - missing/empty text → ok:false, store NOT called (no silent no-op).
 *   - store ok:true + stored>=1 → "Saved" (true).
 *   - store ok:true + stored===0 → "near-duplicate, not stored" (the agent
 *     must NOT say "Saved" here).
 *   - store ok:false → the error is surfaced; the agent relays the failure
 *     instead of fabricating success.
 *
 * Wiring (internal#499):
 *   api.registerTool(() => createMemorySaveTool(store), { names: ['memory_save'] });
 *
 * @param store  the TrMemorySaveFn closure bound to storeExtractedFacts
 *               (captured at register() time, like recall/getById)
 */
export function createMemorySaveTool(store: TrMemorySaveFn): AgentToolLike {
  return {
    name: 'memory_save',
    label: 'Memory Save',
    description:
      'Store ONE explicitly-remembered fact to the user’s encrypted TotalReclaw ' +
      'memory vault. Use this when the user explicitly asks to remember / save / ' +
      'note / not-forget something ("remember X", "save X", "note X", "don’t ' +
      'forget X"). Returns a truthful ok + stored count: relay that verbatim — ' +
      'say "Saved" only when stored >= 1; if stored is 0 the fact was a ' +
      'near-duplicate; if ok is false, tell the user the store failed. NEVER ' +
      'shell out to `tr` or any CLI to store a memory — this tool is the only ' +
      'write path. Do NOT use this for background capture (that is automatic).',
    parameters: {
      type: 'object',
      properties: {
        text: {
          type: 'string',
          description: 'The fact to remember, stated as a self-contained assertion.',
        },
        type: {
          type: 'string',
          enum: [...VALID_MEMORY_TYPES],
          description: 'Memory taxonomy type. Omit to let the store default it.',
        },
        importance: {
          type: 'integer',
          minimum: 1,
          maximum: 10,
          description: 'Salience 1-10. Omit to let the store default it (explicit-remember weight).',
        },
        scope: {
          type: 'string',
          enum: [...VALID_MEMORY_SCOPES],
          description: 'Life-domain scope. Omit to let the store default it.',
        },
        reasoning: {
          type: 'string',
          maxLength: 256,
          description: 'Optional "because Y" clause for decision-style claims.',
        },
        entities: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              name: { type: 'string' },
              type: { type: 'string', enum: ['person', 'project', 'tool', 'company', 'concept', 'place'] },
              role: { type: 'string' },
            },
            required: ['name', 'type'],
          },
          description: 'Optional structured entities to aid search trapdoors.',
        },
      },
      required: ['text'],
      additionalProperties: false,
    },
    execute: async (_toolCallId, params) => {
      const raw = asParamsRecord(params);

      // text is required + must be non-empty after trim. A missing/empty text
      // is a caller error — return ok:false WITHOUT calling store so the agent
      // can never drive this tool into a silent no-op it then misreports.
      const text = typeof raw.text === 'string' ? raw.text.trim() : '';
      if (!text) {
        return jsonResult({
          ok: false,
          stored: 0,
          error: 'memory_save requires a non-empty "text" field.',
          message: 'Could not store that memory: no fact text was provided.',
        });
      }

      // Forward only the optionals the agent supplied AND that pass validation.
      // Invalid optionals are dropped (not forwarded as garbage) — the store
      // closure then applies the canonical default. Defaults are the closure's
      // responsibility, never the tool's.
      const input: TrMemorySaveInput = { text };

      const type = readStringEnum(raw, 'type', VALID_MEMORY_TYPES) as MemoryType | undefined;
      if (type) input.type = type;

      // importance must be a positive integer within the 1-10 band.
      const importanceRaw = readPositiveIntegerParam(raw, 'importance');
      if (importanceRaw !== undefined && importanceRaw <= 10) input.importance = importanceRaw;

      const scope = readStringEnum(raw, 'scope', VALID_MEMORY_SCOPES) as MemoryScope | undefined;
      if (scope) input.scope = scope;

      const reasoning = readStringParam(raw, 'reasoning');
      if (reasoning) input.reasoning = reasoning.slice(0, 256);

      const entitiesRaw = Array.isArray(raw.entities) ? raw.entities : [];
      const entities: ExtractedEntity[] = [];
      for (const e of entitiesRaw) {
        // parseEntity validates {name, type, role} against the taxonomy and
        // returns null on any invalid entity — same validator the historic
        // totalreclaw_remember handler used (regression-guarded by
        // store-dedup-wiring.test.ts scenario 6b).
        const parsed = parseEntity(e);
        if (parsed) entities.push(parsed);
      }
      if (entities.length > 0) input.entities = entities;

      try {
        const result = await store(input);
        // Truthful message the agent can relay verbatim. The branching here is
        // the heart of the fix: stored>=1 is the ONLY "Saved" case.
        const message = !result.ok
          ? `Could not store that memory: ${result.error ?? 'unknown error'}.`
          : result.stored > 0
            ? `Saved ${result.stored} memor${result.stored === 1 ? 'y' : 'ies'} to the encrypted vault.`
            : 'No new memory stored — it is a near-duplicate of one already in the vault.';
        return jsonResult({
          ok: result.ok,
          stored: result.stored,
          ...(result.error ? { error: result.error } : {}),
          message,
        });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        return jsonResult({
          ok: false,
          stored: 0,
          error: msg,
          message: `Could not store that memory: ${msg}.`,
        });
      }
    },
  };
}
