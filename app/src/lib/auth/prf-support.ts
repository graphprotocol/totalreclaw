/**
 * WebAuthn passkey + PRF capability detection.
 *
 * The SPA's at-rest key model wraps the vault under a WebAuthn `prf`-derived
 * secret (see docs/plans/2026-06-07-spa-functional-build-design.md §4). On
 * platforms WITHOUT `prf`, bootstrap is HARD-GATED (PRD-02 R-1) — there is no
 * degraded fallback.
 *
 * No browser API proves "PRF works here" without a passkey prompt, but some
 * can prove it DOESN'T. The pre-gate (webauthn-prf-support-matrix.md §5.2)
 * hard-blocks every definitive "no" before any recovery phrase is generated,
 * and only warns when the answer is genuinely unknown:
 *
 *   - no WebAuthn at all                       → "blocked"
 *   - browser says it can't do PRF             → "blocked"
 *     (`getClientCapabilities()["extension:prf"] === false`)
 *   - a platform with no PRF on any            → "blocked"
 *     authenticator (matrix §2: Safari/iOS
 *     before 18, Firefox on Android)
 *   - otherwise, no platform authenticator     → "advisory" (a security key
 *                                                 or passkey manager may still
 *                                                 work — warn, let them go on)
 *   - otherwise                                → "ok"
 *
 * The AUTHORITATIVE check is unchanged and lives at enrol time:
 * `getClientExtensionResults().prf.enabled` in passkey.ts, which throws
 * PrfUnsupportedError. An authenticator without `hmac-secret` behind a
 * PRF-capable browser (e.g. Windows Hello before KB5077181, a CTAP2 key
 * without the extension) is only detectable there.
 */

type PublicKeyCredentialStatic = {
  isUserVerifyingPlatformAuthenticatorAvailable?: () => Promise<boolean>;
  getClientCapabilities?: () => Promise<Record<string, boolean | undefined>>;
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

/**
 * The browser's own answer on PRF (WebAuthn L3 `getClientCapabilities()`):
 * `false` is definitive, `true` means the browser passes PRF through (the
 * authenticator may still lack it), `undefined` means it didn't say.
 */
export async function clientPrfCapability(): Promise<boolean | undefined> {
  const PKC = publicKeyCredential();
  if (!PKC?.getClientCapabilities) return undefined;
  try {
    const caps = await PKC.getClientCapabilities();
    const prf = caps?.["extension:prf"];
    return typeof prf === "boolean" ? prf : undefined;
  } catch {
    return undefined;
  }
}

export type PasskeySupport = "ok" | "advisory" | "blocked";

/** The §5.2 pre-gate. Never prompts. */
export async function assessPasskeySupport(
  platform: PasskeyPlatform = currentPasskeyPlatform(),
): Promise<PasskeySupport> {
  if (!hasWebAuthn()) return "blocked";
  if (platformLacksPrf(platform)) return "blocked";
  if ((await clientPrfCapability()) === false) return "blocked";
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

/**
 * Platforms where no authenticator can deliver PRF to the page (support
 * matrix §2): WebKit before 18 has no PRF at all, and Firefox on Android
 * doesn't implement it. These are hard blocks, not advisories.
 */
export function platformLacksPrf(platform: PasskeyPlatform): boolean {
  return platform === "apple-old" || platform === "firefox-android";
}

/** Current browser's platform bucket ("unknown" outside a browser). */
export function currentPasskeyPlatform(): PasskeyPlatform {
  const ua = (globalThis as { navigator?: { userAgent?: string } }).navigator?.userAgent ?? "";
  return detectPasskeyPlatform(ua);
}
