import { CONFIRM_TOKEN_MIN_AGE_MS, ConfirmationStore } from '../../src/pii';
import type { PinAction } from '../../src/pin-confirm';

/**
 * A confirm input the user already answered (internal#778): a token for
 * `action` on `subject` (fact id, plus `|new value` for retype / set_scope)
 * that is already older than the minimum age, on a private store.
 */
export function confirmed(action: PinAction, subject: string) {
  let now = 0;
  const store = new ConfirmationStore(() => now);
  const token = store.issue(`pinned_${action}`, subject);
  now += CONFIRM_TOKEN_MIN_AGE_MS;
  return { token, store };
}
