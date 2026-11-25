/**
 * Sensitive-data (PII / credential) screening for memory writes — PRD-04 DEP-4.
 *
 * Thin adapter over `@totalreclaw/core` `piiFilter`. Decision logic, category
 * tags and the user-facing block messages all live in core, so Hermes, the MCP
 * server and the enclave make identical decisions. Mirrors
 * `python/src/totalreclaw/pii.py` (Hermes 2.6.0).
 *
 * Semantics ("block-and-tell"):
 *   - automatic extraction stores a fact only when the decision is `allow`;
 *     `block` and `flag` are dropped (see {@link filterForAutoExtraction});
 *   - an explicit `totalreclaw_remember` stores `allow` and `flag`; for `block`
 *     it returns {@link sensitiveBlockNotice} instead of storing, unless the
 *     call carries `allow_sensitive=true` AND a `confirm_token` issued by an
 *     earlier block (see {@link screenExplicit} and {@link ConfirmationStore}).
 *
 * Logging rule: never log the screened text. Log the decision and category
 * only; {@link logLabel} gives a log-safe stand-in.
 *
 * If the core binding throws, {@link piiDecision} returns `flag` with category
 * `filter_error`: automatic extraction then drops the fact, while an explicit
 * remember still stores it (the user asked for it).
 */

import { randomBytes } from 'node:crypto';

/** Tool parameter that overrides a `block` on an explicit remember. */
export const ALLOW_SENSITIVE_PARAM = 'allow_sensitive';
/** Category reported when the core filter itself is unavailable or failed. */
export const FILTER_ERROR_CATEGORY = 'filter_error';

export interface PiiDecision {
  decision: 'allow' | 'block' | 'flag';
  category?: string;
  message?: string;
}

/** Shape of the block-and-tell tool result. Key order matches Hermes. */
export interface SensitiveBlockNotice {
  stored: false;
  blocked: 'sensitive';
  category?: string;
  message?: string;
  override: typeof ALLOW_SENSITIVE_PARAM;
  confirm_token?: string;
  next_step?: string;
}

/** Same text Hermes ships (`pii.py` CONFIRM_NEXT_STEP). */
export const CONFIRM_NEXT_STEP =
  'Relay message to the user and wait for their answer. Only if they confirm ' +
  'in a later message, call totalreclaw_remember again with ' +
  'allow_sensitive=true and this confirm_token.';

type PiiFilterFn = (text: string) => string;

let _filterOverride: PiiFilterFn | null = null;

/** Test hook: replace the core binding. Pass `null` to restore it. */
export function _setPiiFilterForTests(fn: PiiFilterFn | null): void {
  _filterOverride = fn;
}

function coreFilter(): PiiFilterFn {
  if (_filterOverride) return _filterOverride;
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const core = require('@totalreclaw/core') as typeof import('@totalreclaw/core');
  return core.piiFilter;
}

/** Run the core filter on `text`. Never throws. */
export function piiDecision(text: string): PiiDecision {
  try {
    const raw = JSON.parse(coreFilter()(text)) as Record<string, unknown>;
    const decision = String(raw.decision);
    if (decision !== 'allow' && decision !== 'block' && decision !== 'flag') {
      throw new Error('unknown decision');
    }
    return {
      decision,
      category: typeof raw.category === 'string' ? raw.category : undefined,
      message: typeof raw.message === 'string' ? raw.message : undefined,
    };
  } catch (err) {
    console.error(
      `[pii] pii_filter unavailable (${err instanceof Error ? err.name : 'error'}); ` +
        `treating text as flag/${FILTER_ERROR_CATEGORY}`,
    );
    return { decision: 'flag', category: FILTER_ERROR_CATEGORY };
  }
}

/**
 * Strict opt-in: only JSON `true` or the string "true" count. A stringly-typed
 * "false" must not override a block.
 */
export function parseAllowSensitive(value: unknown): boolean {
  if (value === true) return true;
  return typeof value === 'string' && value.trim().toLowerCase() === 'true';
}

/** Log-safe stand-in: a text preview when allowed, else `<redacted:category>`. */
export function logLabel(text: string, d: PiiDecision, limit = 60): string {
  if (d.decision === 'allow') return text.slice(0, limit);
  return `<redacted:${d.category}>`;
}

// ── Confirmation tokens ──────────────────────────────────────────────────────

/** Confirmation tokens live this long (ms). Same as Hermes (30 minutes). */
export const CONFIRM_TOKEN_TTL_MS = 30 * 60 * 1000;
/**
 * A token cannot be redeemed sooner than this after issue (ms).
 *
 * Hermes counts completed conversation turns and rejects a redeem in the same
 * turn. The MCP server has no turn signal, so a minimum age stands in for it:
 * a model retrying inside its own tool loop is rejected, a user answering is
 * not. This is a weaker guard than Hermes' turn counter.
 */
export const CONFIRM_TOKEN_MIN_AGE_MS = 5 * 1000;
/** At most this many live tokens are kept; the oldest is dropped first. */
export const CONFIRM_TOKEN_MAX_LIVE = 32;

/**
 * Process-wide store of single-use confirmation tokens for sensitive writes.
 * A token records the category at issue time and is accepted only if it is
 * known, unexpired, old enough, and the category matches. Clock injectable.
 */
export class ConfirmationStore {
  private tokens = new Map<string, { category: string; issuedAt: number }>();

  constructor(private clock: () => number = () => Date.now()) {}

  issue(category: string | undefined): string {
    const token = randomBytes(8).toString('hex');
    this.tokens.set(token, { category: String(category), issuedAt: this.clock() });
    while (this.tokens.size > CONFIRM_TOKEN_MAX_LIVE) {
      const oldest = this.tokens.keys().next().value as string;
      this.tokens.delete(oldest);
    }
    return token;
  }

  /** Returns `null` and consumes the token if valid, else the reject reason. */
  redeem(token: unknown, category: string | undefined): string | null {
    if (typeof token !== 'string' || token === '') return 'no_token';
    const entry = this.tokens.get(token);
    if (!entry) return 'unknown_token';
    const age = this.clock() - entry.issuedAt;
    if (age >= CONFIRM_TOKEN_TTL_MS) {
      this.tokens.delete(token);
      return 'expired';
    }
    if (age < CONFIRM_TOKEN_MIN_AGE_MS) return 'too_soon';
    if (entry.category !== String(category)) return 'category_mismatch';
    this.tokens.delete(token);
    return null;
  }
}

/** Shared by every tool call (the server is one process). */
export const confirmations = new ConfirmationStore();

/** Block-and-tell tool result. `confirm_token` / `next_step` only with a token. */
export function sensitiveBlockNotice(d: PiiDecision, confirmToken?: string): SensitiveBlockNotice {
  const notice: SensitiveBlockNotice = {
    stored: false,
    blocked: 'sensitive',
    category: d.category,
    message: d.message,
    override: ALLOW_SENSITIVE_PARAM,
  };
  if (confirmToken !== undefined) {
    notice.confirm_token = confirmToken;
    notice.next_step = CONFIRM_NEXT_STEP;
  }
  return notice;
}

/**
 * Gate for one explicit remember text. Returns the decision and a notice to
 * return instead of storing, or `notice: null` when the write may proceed.
 * `allowSensitive` overrides a block only with a valid `confirmToken`.
 */
export function screenExplicit(
  text: string,
  allowSensitive: boolean,
  confirmToken?: unknown,
  store: ConfirmationStore = confirmations,
): { decision: PiiDecision; notice: SensitiveBlockNotice | null } {
  const decision = piiDecision(text);
  if (decision.decision === 'block') {
    try {
      if (allowSensitive) {
        const reason = store.redeem(confirmToken, decision.category);
        if (reason === null) {
          console.error(`[pii] remember storing sensitive text on explicit request (category=${decision.category})`);
          return { decision, notice: null };
        }
        console.error(`[pii] remember blocked: allow_sensitive without a valid confirmation (reason=${reason}, category=${decision.category})`);
      } else {
        console.error(`[pii] remember blocked: sensitive text (category=${decision.category})`);
      }
      return { decision, notice: sensitiveBlockNotice(decision, store.issue(decision.category)) };
    } catch (err) {
      // Fail closed: never store, never throw.
      console.error(`[pii] remember blocked: confirmation store error (${err instanceof Error ? err.name : 'error'})`);
      return { decision, notice: sensitiveBlockNotice(decision) };
    }
  }
  if (decision.decision !== 'allow') {
    console.error(`[pii] remember storing flagged text on explicit request (category=${decision.category})`);
  }
  return { decision, notice: null };
}

/**
 * Gate for automatic extraction: keep only `allow` items. Returns the kept
 * items and the categories of the dropped ones (for logging).
 */
export function filterForAutoExtraction<T extends { text: string }>(
  items: T[],
): { kept: T[]; droppedCategories: string[] } {
  const kept: T[] = [];
  const droppedCategories: string[] = [];
  for (const item of items) {
    const d = piiDecision(String(item.text ?? ''));
    if (d.decision === 'allow') {
      kept.push(item);
    } else {
      droppedCategories.push(String(d.category));
    }
  }
  if (droppedCategories.length > 0) {
    console.error(`[pii] auto-extraction dropped ${droppedCategories.length} fact(s) (categories=${droppedCategories.join(',')})`);
  }
  return { kept, droppedCategories };
}
