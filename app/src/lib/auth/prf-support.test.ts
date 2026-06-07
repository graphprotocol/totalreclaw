import { describe, it, expect, vi, afterEach } from "vitest";
import { isPasskeyPrfAvailable } from "./prf-support";

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
