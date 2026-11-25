/**
 * Confirmation step before the agent changes a user-pinned fact (internal#778).
 *
 * `totalreclaw_forget`, `totalreclaw_unpin`, `totalreclaw_retype` and
 * `totalreclaw_set_scope` stop on a pinned fact: nothing changes, and the
 * result is `confirmation_required` with a `confirm_token`. The change happens
 * only when the agent calls again with the same arguments and the token, after
 * asking the user.
 *
 * Same token store as the sensitive-data override (`pii.ts`), so the same
 * limits apply: MCP has no notion of conversation turns, so a token only has
 * to be CONFIRM_TOKEN_MIN_AGE_MS (5 s) old. An agent could still confirm
 * without asking the user (documented as a Known Gap in CLAUDE.md). Hermes
 * waits for a real user turn instead.
 */

import { confirmations, type ConfirmationStore } from './pii.js';

export type PinAction = 'forget' | 'unpin' | 'retype' | 'set_scope';

const ACTIONS: Record<PinAction, { tool: string; message: string }> = {
  forget: { tool: 'totalreclaw_forget', message: 'This memory is pinned. Forget it anyway?' },
  unpin: { tool: 'totalreclaw_unpin', message: 'This memory is pinned. Unpin it anyway?' },
  retype: { tool: 'totalreclaw_retype', message: 'This memory is pinned. Change its type anyway?' },
  set_scope: { tool: 'totalreclaw_set_scope', message: 'This memory is pinned. Change its scope anyway?' },
};

/** Optional caller context for a gated operation. */
export interface PinConfirmInput {
  /** The `confirm_token` the agent passed, if any. */
  token?: unknown;
  /** Injectable for tests; defaults to the process-wide store. */
  store?: ConfirmationStore;
}

export function pinCategory(action: PinAction): string {
  return `pinned_${action}`;
}

export interface PinConfirmationFields {
  confirmation_required: string;
  message: string;
  confirm_token: string;
  next_step: string;
}

/**
 * `null` when the change may proceed (a valid token at least the minimum age
 * old), else the fields to merge into the tool result. Call only once the
 * target is known to be pinned. `subject` binds the token (fact id, plus the
 * new value for retype / set_scope).
 */
export function pinConfirmation(
  action: PinAction,
  subject: string,
  input: PinConfirmInput | undefined,
): PinConfirmationFields | null {
  const store = input?.store ?? confirmations;
  const category = pinCategory(action);
  const { tool, message } = ACTIONS[action];
  if (typeof input?.token === 'string' && input.token !== '') {
    const reason = store.redeem(input.token, category, subject);
    if (reason === null) {
      console.error(`[pin-confirm] ${action} on a pinned memory: proceeding after confirmation`);
      return null;
    }
    console.error(`[pin-confirm] ${action} on a pinned memory: confirm_token refused (reason=${reason})`);
  } else {
    console.error(`[pin-confirm] ${action} on a pinned memory: confirmation required`);
  }
  return {
    confirmation_required: category,
    message,
    confirm_token: store.issue(category, subject),
    next_step:
      'Relay message to the user and wait for their answer. Only if they confirm in a later message, ' +
      `call ${tool} again with the same arguments and this confirm_token.`,
  };
}

/** Schema fragment for the optional `confirm_token` argument. */
export const pinConfirmTokenSchema = {
  type: 'string',
  description:
    'Only for a pinned memory. The first call returns confirmation_required with a message and a ' +
    'confirm_token and changes nothing. Ask the user; only after they said yes, call again with the ' +
    'same arguments and this token.',
} as const;
