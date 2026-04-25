/**
 * QA / observability session-tag reader.
 *
 * `TOTALRECLAW_SESSION_ID` is forwarded as the `X-TotalReclaw-Session`
 * header on every outbound relay call, so Axiom log filters and the
 * `qa-totalreclaw` skill can scope log searches per QA run.
 *
 * The variable was accidentally placed in the v1 REMOVED_ENV_VARS list
 * during the v1 cleanup and silently warned-and-dropped, breaking
 * traceability. Restored as SUPPORTED — see internal#127 and
 * `docs/guides/env-vars-reference.md`.
 *
 * Standalone module (no other imports) so any tool/handler can import it
 * without circular-dependency risk against `index.ts`.
 */

export function getSessionId(): string | null {
  const raw = process.env.TOTALRECLAW_SESSION_ID;
  if (raw === undefined) return null;
  const trimmed = raw.trim();
  return trimmed.length > 0 ? trimmed : null;
}
