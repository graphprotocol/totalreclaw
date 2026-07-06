/**
 * WebAuthn passkey + PRF capability detection.
 *
 * The SPA's at-rest key model wraps the vault under a WebAuthn `prf`-derived
 * secret (see docs/plans/2026-06-07-spa-functional-build-design.md §4). On
 * platforms WITHOUT `prf`, bootstrap is HARD-GATED (PRD-02 R-1) — there is no
 * degraded fallback.
 *
 * This module is the pre-gate: it confirms a user-verifying platform
 * authenticator exists. Actual `prf`-output availability is confirmed at enrol
 * time via `getClientExtensionResults().prf` (see passkey.ts).
 */
export async function isPasskeyPrfAvailable(): Promise<boolean> {
  const g = globalThis as unknown as {
    window?: { PublicKeyCredential?: PublicKeyCredentialStatic };
    PublicKeyCredential?: PublicKeyCredentialStatic;
  };
  const PKC = g.window?.PublicKeyCredential ?? g.PublicKeyCredential;
  if (!PKC?.isUserVerifyingPlatformAuthenticatorAvailable) return false;
  try {
    return await PKC.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch {
    return false;
  }
}

type PublicKeyCredentialStatic = {
  isUserVerifyingPlatformAuthenticatorAvailable?: () => Promise<boolean>;
};
