/**
 * One-time tip for Claude Desktop: how to make TotalReclaw Claude's main memory.
 *
 * Claude's own memory is on by default and is used before TotalReclaw. After
 * credentials were activated by a pairing in THIS process (not at startup),
 * the first successful `totalreclaw_account` / `totalreclaw_status` result gets
 * one extra final paragraph: a tip for the user (not an instruction to the
 * host). Once per pairing (a new pairing in the same process arms it again). No other tool is touched.
 *
 * Only Claude Desktop sees it (MCP `clientInfo.name` is exactly `claude-ai`).
 * For any other client, or when clientInfo is missing, the tip is dropped
 * silently and never shown.
 */

import type { ToolResponse } from './tools/types.js';

/** Approved user-facing copy. Do not edit without the owner's approval. */
export const SETUP_NOTE_TEXT =
  'Tip for the user: in Claude apps, Claude\'s own memory is usually on and is used before TotalReclaw. ' +
  'To have Claude use TotalReclaw automatically in future chats, just say: "Make TotalReclaw my main memory."';

/** MCP `clientInfo.name` that Claude Desktop sends. */
export const CLAUDE_DESKTOP_CLIENT_NAME = 'claude-ai';

let pending = false;

/** Arm the tip (called when a reload activated credentials after pairing). */
export function armSetupNote(): void {
  pending = true;
}

/** Test helper. */
export function resetSetupNote(): void {
  pending = false;
}

/** True when the result is a soft error: a text block holding a JSON object with an `error` key. */
function isSoftError(text: string): boolean {
  try {
    const v: unknown = JSON.parse(text);
    return typeof v === 'object' && v !== null && !Array.isArray(v) && 'error' in v;
  } catch {
    return false;
  }
}

/**
 * Append the tip to a successful result, once, for Claude Desktop only.
 * Errors (isError, or a JSON `{"error": ...}` text) pass through and keep it armed. For any other client the tip is
 * disarmed without a trace.
 */
export function withSetupNote(res: ToolResponse, clientName: string | undefined): ToolResponse {
  if (!pending || res.isError) return res;
  if (clientName !== CLAUDE_DESKTOP_CLIENT_NAME) {
    pending = false;
    return res;
  }
  const content = res.content ?? [];
  const idx = content.map((c) => c.type).lastIndexOf('text');
  if (idx < 0) return res;
  if (isSoftError((content[idx] as { type: 'text'; text: string }).text)) return res;
  pending = false;
  const next = content.slice();
  const last = next[idx] as { type: 'text'; text: string };
  next[idx] = { ...last, text: `${last.text}\n\n${SETUP_NOTE_TEXT}` };
  return { ...res, content: next };
}
