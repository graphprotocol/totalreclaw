import { describe, it, expect, vi, afterEach } from "vitest";
import {
  isPasskeyPrfAvailable,
  hasWebAuthn,
  assessPasskeySupport,
  detectPasskeyPlatform,
} from "./prf-support";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("isPasskeyPrfAvailable", () => {
  it("returns false when PublicKeyCredential is missing", async () => {
    vi.stubGlobal("window", {});
    vi.stubGlobal("PublicKeyCredential", undefined);
    expect(await isPasskeyPrfAvailable()).toBe(false);
  });

  it("returns false when no user-verifying platform authenticator", async () => {
    vi.stubGlobal("window", {
      PublicKeyCredential: {
        isUserVerifyingPlatformAuthenticatorAvailable: async () => false,
      },
    });
    expect(await isPasskeyPrfAvailable()).toBe(false);
  });

  it("returns false when the capability probe throws", async () => {
    vi.stubGlobal("window", {
      PublicKeyCredential: {
        isUserVerifyingPlatformAuthenticatorAvailable: async () => {
          throw new Error("not allowed");
        },
      },
    });
    expect(await isPasskeyPrfAvailable()).toBe(false);
  });

  it("returns true when a UV platform authenticator is available", async () => {
    vi.stubGlobal("window", {
      PublicKeyCredential: {
        isUserVerifyingPlatformAuthenticatorAvailable: async () => true,
      },
    });
    expect(await isPasskeyPrfAvailable()).toBe(true);
  });
});

describe("hasWebAuthn", () => {
  it("is false without PublicKeyCredential", () => {
    vi.stubGlobal("window", {});
    vi.stubGlobal("PublicKeyCredential", undefined);
    expect(hasWebAuthn()).toBe(false);
  });

  it("is true when PublicKeyCredential exists, even without the platform probe", () => {
    vi.stubGlobal("window", { PublicKeyCredential: {} });
    expect(hasWebAuthn()).toBe(true);
  });
});

describe("assessPasskeySupport (support matrix §5.2 pre-gate)", () => {
  it("blocks only when WebAuthn is missing outright", async () => {
    vi.stubGlobal("window", {});
    vi.stubGlobal("PublicKeyCredential", undefined);
    expect(await assessPasskeySupport()).toBe("blocked");
  });

  it("is advisory, not a block, when there is no platform authenticator (security keys may work)", async () => {
    vi.stubGlobal("window", {
      PublicKeyCredential: { isUserVerifyingPlatformAuthenticatorAvailable: async () => false },
    });
    expect(await assessPasskeySupport()).toBe("advisory");
  });

  it("is advisory when the platform probe is missing or throws", async () => {
    vi.stubGlobal("window", { PublicKeyCredential: {} });
    expect(await assessPasskeySupport()).toBe("advisory");
    vi.stubGlobal("window", {
      PublicKeyCredential: {
        isUserVerifyingPlatformAuthenticatorAvailable: async () => {
          throw new Error("nope");
        },
      },
    });
    expect(await assessPasskeySupport()).toBe("advisory");
  });

  it("is ok with a user-verifying platform authenticator", async () => {
    vi.stubGlobal("window", {
      PublicKeyCredential: { isUserVerifyingPlatformAuthenticatorAvailable: async () => true },
    });
    expect(await assessPasskeySupport()).toBe("ok");
  });
});

describe("detectPasskeyPlatform", () => {
  const UA = {
    win11Chrome:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    win11Firefox: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    androidFirefox: "Mozilla/5.0 (Android 15; Mobile; rv:140.0) Gecko/140.0 Firefox/140.0",
    androidChrome:
      "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
    ios17Safari:
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    ios17Chrome:
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/130.0.0.0 Mobile/15E148 Safari/604.1",
    ios18Safari:
      "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    ipad17:
      "Mozilla/5.0 (iPad; CPU OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    mac17Safari:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    mac18Safari:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    macChrome:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    linuxFirefox: "Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0",
  };

  it.each([
    ["win11Chrome", "windows"],
    ["win11Firefox", "windows"],
    ["androidFirefox", "firefox-android"],
    ["androidChrome", "unknown"],
    ["ios17Safari", "apple-old"],
    ["ios17Chrome", "apple-old"],
    ["ipad17", "apple-old"],
    ["ios18Safari", "safari"],
    ["mac17Safari", "apple-old"],
    ["mac18Safari", "safari"],
    ["macChrome", "unknown"],
    ["linuxFirefox", "unknown"],
  ] as const)("%s → %s", (key, expected) => {
    expect(detectPasskeyPlatform(UA[key])).toBe(expected);
  });

  it("empty UA is unknown", () => {
    expect(detectPasskeyPlatform("")).toBe("unknown");
  });
});
