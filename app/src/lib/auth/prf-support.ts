/**
 * WebAuthn passkey + PRF capability detection.
 *
 * The SPA's at-rest key model wraps the vault under a WebAuthn `prf`-derived
 * secret (see docs/plans/2026-06-07-spa-functional-build-design.md §4). On
 * platforms WITHOUT `prf`, bootstrap is HARD-GATED (PRD-02 R-1) — there is no
 * degraded fallback.
 *
 * No browser API answers "does PRF work here?" without a passkey prompt, so
 * the pre-gate is a HINT (webauthn-prf-support-matrix.md §5.2):
 *
 *   - no WebAuthn at all              → "blocked"  (no path, hard block)
 *   - WebAuthn, no platform authn.    → "advisory" (a security key or a
 *                                        passkey manager may still work —
 *                                        warn, but let the user continue)
 *   - both                            → "ok"
 *
 * The AUTHORITATIVE check is unchanged and lives at enrol time:
 * `getClientExtensionResults().prf.enabled` in passkey.ts, which throws
 * PrfUnsupportedError.
 */

type PublicKeyCredentialStatic = {
  isUserVerifyingPlatformAuthenticatorAvailable?: () => Promise<boolean>;
};

function publicKeyCredential(): PublicKeyCredentialStatic | undefined {
  const g = globalThis as unknown as {
    window?: { PublicKeyCredential?: PublicKeyCredentialStatic };
    PublicKeyCredential?: PublicKeyCredentialStatic;
  };
  return g.window?.PublicKeyCredential ?? g.PublicKeyCredential;
}

/** True when the browser exposes WebAuthn at all. */
export function hasWebAuthn(): boolean {
  return !!publicKeyCredential();
}

/**
 * True when a user-verifying platform authenticator (Touch ID, Face ID,
 * Windows Hello, Android screen lock) is reported. This is a proxy, not a
 * PRF check — see the module docstring.
 */
export async function isPasskeyPrfAvailable(): Promise<boolean> {
  const PKC = publicKeyCredential();
  if (!PKC?.isUserVerifyingPlatformAuthenticatorAvailable) return false;
  try {
    return await PKC.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch {
    return false;
  }
}

export type PasskeySupport = "ok" | "advisory" | "blocked";

/** The §5.2 pre-gate. Never prompts. */
export async function assessPasskeySupport(): Promise<PasskeySupport> {
  if (!hasWebAuthn()) return "blocked";
  return (await isPasskeyPrfAvailable()) ? "ok" : "advisory";
}

/**
 * Platform buckets that get their own guidance line (support matrix §5.3).
 * UA sniffing is a heuristic: anything uncertain falls to "unknown", which
 * gets the generic line.
 */
export type PasskeyPlatform = "windows" | "firefox-android" | "apple-old" | "safari" | "unknown";

export function detectPasskeyPlatform(ua: string): PasskeyPlatform {
  if (/Windows NT/i.test(ua)) return "windows";
  if (/Android/i.test(ua) && /Firefox\//i.test(ua)) return "firefox-android";

  // iOS / iPadOS: every browser runs on the system WebKit, so the OS version
  // is what matters. PRF needs iOS 18+.
  const ios = ua.match(/(?:iPhone|iPad|iPod)[^;]*; CPU (?:iPhone )?OS (\d+)_/i);
  if (ios) return Number(ios[1]) < 18 ? "apple-old" : "safari";

  // Desktop Safari (Chromium-based and Firefox UAs also say "Safari").
  const isSafari =
    /Safari\//.test(ua) && !/(Chrome|Chromium|CriOS|FxiOS|Edg|OPR|Firefox|Android)\//.test(ua);
  if (isSafari) {
    const v = ua.match(/Version\/(\d+)/);
    if (v && Number(v[1]) < 18) return "apple-old";
    return "safari";
  }
  return "unknown";
}

/** Current browser's platform bucket ("unknown" outside a browser). */
export function currentPasskeyPlatform(): PasskeyPlatform {
  const ua = (globalThis as { navigator?: { userAgent?: string } }).navigator?.userAgent ?? "";
  return detectPasskeyPlatform(ua);
}
