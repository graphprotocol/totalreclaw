/**
 * Shared runtime helpers for the TotalReclaw MCP tool handlers.
 *
 * Two concerns live here so the whole tool surface stays consistent:
 *
 * 1. Memory-identifier resolution. The canonical parameter name is
 *    `memory_id`; `fact_id` is accepted everywhere as a documented back-compat
 *    alias (older hosts + the pin/forget tools predate the v1 rename). A single
 *    resolver keeps the precedence rule (canonical wins) in one place.
 *
 * 2. The tool-response error envelope. Every handler returns the same
 *    `{ content: [{ type: 'text', text: JSON.stringify({ success:false, error }) }] }`
 *    shape on failure; `toolError` builds it so call sites don't re-spell it.
 */

import type { ToolResponse } from './types.js';

export const MEMORY_ID_REQUIRED_MESSAGE =
  'Invalid input: memory_id (or its back-compat alias fact_id) must be a non-empty string';

/**
 * Extract the memory identifier from tool args, accepting `memory_id`
 * (canonical) or `fact_id` (alias). Returns `undefined` when neither is a
 * non-empty string — callers that also accept other selectors (e.g. forget's
 * `query`) use this soft form.
 */
export function pickMemoryId(args: unknown): string | undefined {
  if (!args || typeof args !== 'object') return undefined;
  const record = args as Record<string, unknown>;
  const raw =
    typeof record.memory_id === 'string' && record.memory_id.trim().length > 0
      ? record.memory_id
      : typeof record.fact_id === 'string' && record.fact_id.trim().length > 0
        ? record.fact_id
        : undefined;
  return raw?.trim();
}

export interface ResolvedMemoryId {
  ok: boolean;
  memoryId: string;
  error: string;
}

/**
 * Strict resolver: a non-empty `memory_id`/`fact_id` is required. Returns a
 * flat `{ ok, memoryId, error }` (matching the per-tool `Validated*Args`
 * shapes) so callers can early-return the error envelope.
 */
export function resolveMemoryId(args: unknown): ResolvedMemoryId {
  const id = pickMemoryId(args);
  if (!id) return { ok: false, memoryId: '', error: MEMORY_ID_REQUIRED_MESSAGE };
  return { ok: true, memoryId: id, error: '' };
}

/**
 * Build the standard failure envelope. `extra` merges additional fields into
 * the JSON payload (e.g. forget's `deleted_count` / `fact_ids`).
 */
export function toolError(
  error: string,
  extra: Record<string, unknown> = {},
): ToolResponse {
  return {
    content: [
      {
        type: 'text',
        text: JSON.stringify({ success: false, ...extra, error }),
      },
    ],
  };
}
