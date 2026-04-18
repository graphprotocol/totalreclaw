/**
 * `totalreclaw_set_scope` — set or change the v1 scope of an existing memory.
 *
 * Uses the same supersede pattern as `retype` and `pin`: rebuild the inner
 * claim with the scope override, tombstone the old fact id, write the new
 * one with `superseded_by` linking back.
 *
 * Spec: `docs/specs/totalreclaw/memory-taxonomy-v1.md` §3-new-MCP-tools.
 */

import {
  VALID_MEMORY_SCOPES,
  type MemoryScope,
} from '../v1-types.js';
import {
  executeMetadataOp,
  type MetadataOpDeps,
  type MetadataOpResult,
} from './retype.js';

const SET_SCOPE_DESCRIPTION =
  'Re-tag memory under different life-domain scope (work|personal|health|family|creative|finance|misc|unspecified).\n' +
  '\nINVOKE WHEN USER SAYS:\n' +
  '- "actually that\'s work context, not personal"\n' +
  '- "file that under health" / "move to finance"\n' +
  '- "wrong scope — about my kids, not work"\n' +
  '\nDOES: builds v1 claim with new scope, links via superseded_by, tombstones original. Idempotent.\n' +
  '\nWHEN NOT TO USE:\n' +
  '- memory wrong → totalreclaw_forget + totalreclaw_remember\n' +
  '- type wrong (not scope) → totalreclaw_retype';

export const setScopeToolDefinition = {
  name: 'totalreclaw_set_scope',
  description: SET_SCOPE_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      memory_id: {
        type: 'string',
        description: 'The ID of the memory to retag (from a prior totalreclaw_recall result).',
      },
      scope: {
        type: 'string',
        enum: [...VALID_MEMORY_SCOPES],
        description:
          'New scope value. One of: work, personal, health, family, creative, finance, misc, unspecified.',
      },
    },
    required: ['memory_id', 'scope'],
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
  },
};

interface ValidatedArgs {
  ok: boolean;
  memoryId: string;
  scope: MemoryScope;
  error: string;
}

export function validateSetScopeArgs(args: unknown): ValidatedArgs {
  if (!args || typeof args !== 'object') {
    return {
      ok: false,
      memoryId: '',
      scope: 'unspecified',
      error: 'Invalid input: memory_id and scope are required',
    };
  }
  const record = args as Record<string, unknown>;
  const memoryId = record.memory_id;
  if (typeof memoryId !== 'string' || memoryId.trim().length === 0) {
    return {
      ok: false,
      memoryId: '',
      scope: 'unspecified',
      error: 'Invalid input: memory_id must be a non-empty string',
    };
  }
  const scope = record.scope;
  if (typeof scope !== 'string' || !(VALID_MEMORY_SCOPES as readonly string[]).includes(scope)) {
    return {
      ok: false,
      memoryId: memoryId.trim(),
      scope: 'unspecified',
      error: `Invalid input: scope must be one of ${VALID_MEMORY_SCOPES.join(', ')}`,
    };
  }
  return {
    ok: true,
    memoryId: memoryId.trim(),
    scope: scope as MemoryScope,
    error: '',
  };
}

export async function executeSetScope(
  memoryId: string,
  newScope: MemoryScope,
  deps: MetadataOpDeps,
): Promise<MetadataOpResult> {
  return executeMetadataOp<MemoryScope>(
    memoryId,
    deps,
    (e) => e.scope ?? 'unspecified',
    (cur, next) => cur === next,
    (e, next) => ({
      text: e.text,
      type: e.type,
      source: e.source,
      scope: next,
      volatility: e.volatility,
      reasoning: e.reasoning,
      importance: e.importance,
      confidence: e.confidence,
      createdAt: e.createdAt,
      supersededBy: memoryId,
    }),
    newScope,
    'set_scope',
  );
}

export async function handleSetScope(
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const validation = validateSetScopeArgs(args);
  if (!validation.ok) {
    return errorResponse(validation.error);
  }
  return errorResponse(
    'Set-scope is only supported with the managed service. Self-hosted mode does not yet implement v1 supersession.',
  );
}

export async function handleSetScopeWithDeps(
  args: unknown,
  deps: MetadataOpDeps,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const validation = validateSetScopeArgs(args);
  if (!validation.ok) return errorResponse(validation.error);
  const result = await executeSetScope(validation.memoryId, validation.scope, deps);
  return { content: [{ type: 'text', text: JSON.stringify(result) }] };
}

function errorResponse(error: string): { content: Array<{ type: string; text: string }> } {
  return { content: [{ type: 'text', text: JSON.stringify({ success: false, error }) }] };
}
