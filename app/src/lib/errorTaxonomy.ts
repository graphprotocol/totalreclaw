/**
 * Unlock / recovery error taxonomy (spa-passkey-unlock.md §9, G-5).
 *
 * Replaces the raw `e.message` banner UnlockPage used to render — users
 * could not tell "you cancelled" from "your device can't do this" from
 * internal exception text. Pure + fully unit-testable; UnlockPage is thin
 * wiring around this.
 *
 * `context` picks the right generic-failure message when the underlying
 * exception carries no more specific signal (e.g. an AEAD failure means
 * something different depending on whether it happened unwrapping the
 * local VaultRecord, opening an escrow blob, or opening a backup file).
 */
import { PrfUnsupportedError } from "./auth/passkey";
import { EscrowVersionUnsupportedError } from "./auth/escrow";

export type UnlockErrorKind =
  | "cancelled"
  | "prf-unsupported"
  | "unwrap-failed"
  | "escrow-damaged"
  | "escrow-version-unsupported"
  | "offline"
  | "unknown";

export interface MappedUnlockError {
  kind: UnlockErrorKind;
  /** User-facing sentence — never the raw exception message except where it already is one (PrfUnsupportedError). */
  message: string;
}

export type UnlockErrorContext = "local" | "escrow" | "file";

const UNWRAP_FAILED_MESSAGES: Record<UnlockErrorContext, string> = {
  local: "Couldn’t unlock on this device. Try your recovery phrase.",
  escrow: "This backup couldn’t be opened and may be damaged.",
  file: "This backup was made with a different passkey.",
};

/**
 * Map an exception thrown by unlock()/restoreFromEscrow()/restoreFromFile()
 * to a taxonomy entry. Does NOT handle the non-exception outcomes those
 * functions already return as plain values ("no-escrow", "bad-file",
 * "wrong-passkey", "unsupported-version") — callers branch on those
 * directly; this function is for the `catch` path only.
 */
export function mapUnlockError(e: unknown, context: UnlockErrorContext = "local"): MappedUnlockError {
  if (e instanceof PrfUnsupportedError) {
    // §5.2 of webauthn-prf-support-matrix.md specs a fuller platform-detected
    // variant of this message; out of Phase 1's explicit step list (design
    // memo §9) — this surfaces the existing PrfUnsupportedError sentence.
    return { kind: "prf-unsupported", message: e.message };
  }
  if (e instanceof EscrowVersionUnsupportedError) {
    return {
      kind: "escrow-version-unsupported",
      message: "This backup was made by a newer version of TotalReclaw.",
    };
  }
  // Cancellation / timeout: the DOMException the WebAuthn API throws, or the
  // existing null-credential path in passkey.ts, which raises a plain Error
  // whose message contains "cancelled".
  if (e instanceof DOMException && e.name === "NotAllowedError") {
    return { kind: "cancelled", message: "Unlock cancelled." };
  }
  if (e instanceof Error && /cancelled/i.test(e.message)) {
    return { kind: "cancelled", message: "Unlock cancelled." };
  }
  // `fetch()` rejects with a TypeError when the network is unreachable
  // (Chrome: "Failed to fetch"; Firefox: "NetworkError when attempting to
  // fetch resource"). A relay-side non-2xx surfaces as a thrown Error with
  // a status code instead, which falls through to the context default below
  // rather than being misreported as "offline".
  if (e instanceof TypeError) {
    return { kind: "offline", message: "We can’t reach TotalReclaw right now." };
  }
  // Everything else reaching here is, by elimination, an AEAD failure
  // (tamper/corruption/wrong key) in whichever step the caller was
  // attempting — the context-specific message says which.
  return { kind: "unwrap-failed", message: UNWRAP_FAILED_MESSAGES[context] };
}
