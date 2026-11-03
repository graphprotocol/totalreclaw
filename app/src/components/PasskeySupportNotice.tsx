/**
 * Browser-support screens for vault creation (webauthn-prf-support-matrix.md
 * §5.2 + §5.3). Presentational only — the gate decision comes from
 * `assessPasskeySupport()` / `PrfUnsupportedError`, the platform bucket from
 * `detectPasskeyPlatform()`.
 */
import type { PasskeyPlatform } from "../lib/auth/prf-support";

/** Alternative entry that needs no browser passkey (matrix §3, B-1 mitigation). */
export const HERMES_GUIDE_URL =
  "https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/hermes-setup.md";

/** §5.3 replacement copy: one extra line per detected platform. */
export const UNSUPPORTED_PLATFORM_LINE: Record<PasskeyPlatform, string> = {
  windows:
    "On Windows 11, update to version 25H2 and install the latest Windows update, then use Chrome, Edge, or Firefox. Windows 10 isn’t supported.",
  "firefox-android": "Firefox on Android doesn’t support this yet. Chrome, Edge, or Samsung Internet will work.",
  "apple-old": "Update to iOS 18 or macOS 15 (or later), then try again in Safari.",
  safari:
    "Safari can’t use security keys for this. Chrome, Edge, or Firefox can — or use a passkey stored on this device instead.",
  unknown: "Try a recent Chrome, Edge, or Firefox, or Safari 18 or later on iOS 18 / macOS 15 or later.",
};

function HermesLink() {
  return (
    <p className="mt-6 text-sm text-ink-muted">
      You can also use TotalReclaw through the Hermes agent, which doesn’t need a browser passkey.{" "}
      <a
        href={HERMES_GUIDE_URL}
        target="_blank"
        rel="noreferrer"
        className="font-semibold text-clay-deep underline decoration-dotted hover:text-clay"
      >
        Set up Hermes
      </a>
    </p>
  );
}

/** Hard block: no WebAuthn at all, or PRF confirmed missing at enrol. */
export function PasskeyUnsupportedNotice({ platform }: { platform: PasskeyPlatform }) {
  return (
    <>
      <h1 className="font-display text-2xl font-semibold text-ink">Your browser can’t secure a vault yet</h1>
      <p className="mt-3 text-ink-muted">
        TotalReclaw locks your vault with a passkey, which needs a browser feature your setup doesn’t
        support yet.
      </p>
      <p className="mt-3 text-ink-muted">{UNSUPPORTED_PLATFORM_LINE[platform]}</p>
      <HermesLink />
    </>
  );
}

/**
 * Advisory: WebAuthn is present but no built-in passkey was reported. A
 * security key or a passkey manager may still work, so the user can go on —
 * the enrol-time PRF check is the authoritative answer.
 */
export function PasskeyAdvisoryNotice({
  platform,
  onContinue,
}: {
  platform: PasskeyPlatform;
  onContinue: () => void;
}) {
  return (
    <>
      <h1 className="font-display text-2xl font-semibold text-ink">You may need a security key</h1>
      <p className="mt-3 text-ink-muted">
        We couldn’t find a passkey built into this device (Touch ID, Face ID, or Windows Hello). You
        can still continue with a security key or a passkey manager such as 1Password.
      </p>
      {platform !== "unknown" && <p className="mt-3 text-ink-muted">{UNSUPPORTED_PLATFORM_LINE[platform]}</p>}
      <button
        onClick={onContinue}
        className="mt-6 w-full rounded-control bg-clay px-5 py-3 font-semibold text-warm-white transition duration-200 ease-keeper hover:bg-clay-deep"
      >
        Continue anyway
      </button>
      <HermesLink />
    </>
  );
}
