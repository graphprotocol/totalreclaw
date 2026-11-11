import { describe, it, expect } from "vitest";
import { passkeyUserName, shortAddress, PASSKEY_NAME_FALLBACK } from "./passkeyName";

const SA = "0x1234567890abcdef1234567890abcdef12345678";

describe("passkeyUserName", () => {
  it("adds the short vault address so passkeys for different vaults are distinguishable", () => {
    expect(passkeyUserName(SA)).toBe("TotalReclaw vault · 0x1234…5678");
  });

  it("falls back to the plain name when the address is missing or malformed", () => {
    expect(passkeyUserName(null)).toBe(PASSKEY_NAME_FALLBACK);
    expect(passkeyUserName(undefined)).toBe(PASSKEY_NAME_FALLBACK);
    expect(passkeyUserName("not-an-address")).toBe(PASSKEY_NAME_FALLBACK);
  });

  it("shortAddress leaves non-addresses untouched", () => {
    expect(shortAddress(SA)).toBe("0x1234…5678");
    expect(shortAddress("abc")).toBe("abc");
  });
});
