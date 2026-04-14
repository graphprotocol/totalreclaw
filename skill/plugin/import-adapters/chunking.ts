/**
 * Shared conversation chunking helpers used by conversation-based import
 * adapters (ChatGPT, Gemini, Claude conversation exports).
 *
 * Context: `docs/plans/2026-04-12-import-chunk-size-bump.md`. The 15-fact
 * extraction cap does NOT apply to imports (see
 * `docs/specs/totalreclaw/client-consistency.md → Import Extraction`).
 * Conversation chunks are sized for narrative preservation, with a token
 * budget guard so small-context local models don't get overrun.
 */

/** Maximum messages per conversation chunk (narrative preservation). */
export const CONVERSATION_CHUNK_SIZE = 80;

/** Maximum messages per memories/plain-text chunk — stays small, atomic facts. */
export const MEMORIES_CHUNK_SIZE = 20;

/**
 * Approximate input-token budget per chunk. Passed to splitByTokenBudget so
 * a single chunk never exceeds this after conversation windowing.
 *
 * Sized for 128K-context models (Qwen3.5 @ 128K): 40K input leaves headroom
 * for the system prompt (~3K), profile context (~2K), and output (~16K).
 */
export const CHUNK_TOKEN_BUDGET = 40_000;

/** Rough chars-per-token heuristic. Good enough for splitting; not for billing. */
const CHARS_PER_TOKEN = 4;

export interface Message {
  role: 'user' | 'assistant';
  text: string;
}

/** Estimated token count for a sequence of messages. */
function estimateTokens(messages: readonly Message[]): number {
  let chars = 0;
  for (const m of messages) chars += m.text.length;
  return Math.ceil(chars / CHARS_PER_TOKEN);
}

/**
 * Split a message array into sequential windows of up to `size` messages each.
 * Always preserves chronological order and never splits a single message.
 */
export function windowMessages<T>(messages: readonly T[], size: number): T[][] {
  if (size <= 0) return [messages.slice() as T[]];
  const out: T[][] = [];
  for (let i = 0; i < messages.length; i += size) {
    out.push(messages.slice(i, i + size) as T[]);
  }
  return out;
}

/**
 * Recursively split any oversized chunks so each fits within `maxTokens`.
 * Chunks that already fit are returned unchanged. A single message that
 * exceeds the budget on its own is returned as-is (we never cut a message
 * mid-sentence — the LLM will just get a slightly-too-large input).
 */
export function splitByTokenBudget(
  chunks: readonly Message[][],
  maxTokens: number = CHUNK_TOKEN_BUDGET,
): Message[][] {
  const out: Message[][] = [];
  for (const chunk of chunks) {
    out.push(...splitChunkByTokens(chunk, maxTokens));
  }
  return out;
}

function splitChunkByTokens(chunk: Message[], maxTokens: number): Message[][] {
  if (chunk.length <= 1) return [chunk];
  if (estimateTokens(chunk) <= maxTokens) return [chunk];

  const mid = Math.floor(chunk.length / 2);
  const first = chunk.slice(0, mid);
  const second = chunk.slice(mid);
  return [...splitChunkByTokens(first, maxTokens), ...splitChunkByTokens(second, maxTokens)];
}
