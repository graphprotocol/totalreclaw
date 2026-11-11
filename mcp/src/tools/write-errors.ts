/**
 * Shared error handling for every write path (remember, debrief, pin, unpin,
 * retype, set_scope).
 *
 * Two rules:
 *  1. A quota error (relay 403 `quota_exceeded`) must never be swallowed into a
 *     per-item failure. Write paths call `rethrowIfQuota` first so the dispatch
 *     guard returns the standard quota response (`isError: true`, upgrade hint).
 *  2. Any other submission failure is reported as ONE line (`shortErrorMessage`)
 *     — never viem's full request dump (callData, gas fields, signatures).
 */

/**
 * True when the error is the relay's "monthly write limit reached" answer.
 * Checks the full message (viem puts the relay body in `Details:` at the very
 * end), the `details` field and the cause chain.
 */
export function isQuotaExceededError(error: unknown): boolean {
  const seen = new Set<unknown>();
  let cur: unknown = error;
  for (let depth = 0; cur && depth < 5 && !seen.has(cur); depth++) {
    seen.add(cur);
    if (cur instanceof Error) {
      const texts = [cur.message, (cur as { details?: unknown }).details];
      for (const t of texts) {
        if (typeof t === 'string') {
          const lower = t.toLowerCase();
          if (lower.includes('quota_exceeded') || lower.includes('quota exceeded')) return true;
        }
      }
      cur = (cur as { cause?: unknown }).cause;
    } else {
      break;
    }
  }
  return false;
}

/** Rethrow quota errors so the dispatch guard turns them into the quota response. */
export function rethrowIfQuota(error: unknown): void {
  if (isQuotaExceededError(error)) throw error;
}

/**
 * One-line, hex-free description of a write failure: viem's `shortMessage`
 * when present, otherwise the first line of `message`.
 */
export function shortErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    const short = (error as { shortMessage?: unknown }).shortMessage;
    const raw = typeof short === 'string' && short.trim() ? short : error.message;
    const line = (raw.split('\n')[0] ?? '').trim();
    return line.length > 300 ? `${line.slice(0, 300)}...` : line || 'Unknown error';
  }
  const s = String(error ?? '').split('\n')[0]?.trim();
  return s ? (s.length > 300 ? `${s.slice(0, 300)}...` : s) : 'Unknown error';
}

/**
 * Full error text for operators (stderr only, never the tool response):
 * message, viem `details`, and the cause chain, so codes like AA25 survive the
 * one-line message the model sees. Callers must not pass phrase or key
 * material; calldata hex and signatures in viem dumps are acceptable here.
 */
export function describeErrorFull(error: unknown): string {
  const parts: string[] = [];
  const seen = new Set<unknown>();
  let cur: unknown = error;
  for (let depth = 0; cur && depth < 5 && !seen.has(cur); depth++) {
    seen.add(cur);
    if (cur instanceof Error) {
      parts.push(cur.message);
      const details = (cur as { details?: unknown }).details;
      if (typeof details === 'string' && details && !cur.message.includes(details)) {
        parts.push(`details: ${details}`);
      }
      cur = (cur as { cause?: unknown }).cause;
      if (cur) parts.push('caused by:');
    } else {
      parts.push(String(cur));
      break;
    }
  }
  return parts.join('\n') || 'Unknown error';
}

/** Log the full error to stderr, tagged with the operation. */
export function logWriteError(context: string, error: unknown): void {
  console.error(`${context}: ${describeErrorFull(error)}`);
}
