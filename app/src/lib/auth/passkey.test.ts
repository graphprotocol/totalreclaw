import { describe, it, expect, vi, afterEach } from "vitest";
import { enrolPasskey, getPrfSecret, PrfUnsupportedError } from "./passkey";

const RAW_ID = new Uint8Array([1, 2, 3, 4]).buffer;
const PRF_OUT = new Uint8Array(32).fill(0xab).buffer;

function stubNavigator(impl: { create?: unknown; get?: unknown }) {
  vi.stubGlobal("navigator", {
    credentials: {
      create: vi.fn(async () => impl.create),
      get: vi.fn(async () => impl.get),
    },
  });
  vi.stubGlobal("location", { hostname: "app.totalreclaw.xyz" });
}

afterEach(() => vi.unstubAllGlobals());

describe("enrolPasskey", () => {
  it("returns credentialId when prf is enabled", async () => {
    stubNavigator({
      create: {
        rawId: RAW_ID,
        getClientExtensionResults: () => ({ prf: { enabled: true } }),
      },
    });
    const { credentialId } = await enrolPasskey({ userId: new Uint8Array(16), userName: "vault" });
    expect(Array.from(credentialId)).toEqual([1, 2, 3, 4]);
  });

  it("throws PrfUnsupportedError when prf not enabled", async () => {
    stubNavigator({
      create: { rawId: RAW_ID, getClientExtensionResults: () => ({}) },
    });
    await expect(enrolPasskey({ userId: new Uint8Array(16), userName: "vault" })).rejects.toThrow(
      PrfUnsupportedError,
    );
  });

  it("throws when enrolment is cancelled (null credential)", async () => {
    stubNavigator({ create: null });
    await expect(enrolPasskey({ userId: new Uint8Array(16), userName: "vault" })).rejects.toThrow(
      /cancelled/,
    );
  });
});

describe("getPrfSecret", () => {
  it("returns a 32-byte prf secret + credentialId", async () => {
    stubNavigator({
      get: {
        rawId: RAW_ID,
        getClientExtensionResults: () => ({ prf: { results: { first: PRF_OUT } } }),
      },
    });
    const { prfSecret, credentialId } = await getPrfSecret();
    expect(prfSecret.length).toBe(32);
    expect(prfSecret[0]).toBe(0xab);
    expect(Array.from(credentialId)).toEqual([1, 2, 3, 4]);
  });

  it("throws PrfUnsupportedError when prf result missing", async () => {
    stubNavigator({
      get: { rawId: RAW_ID, getClientExtensionResults: () => ({}) },
    });
    await expect(getPrfSecret()).rejects.toThrow(PrfUnsupportedError);
  });

  it("throws PrfUnsupportedError when prf output is not 32 bytes", async () => {
    stubNavigator({
      get: {
        rawId: RAW_ID,
        getClientExtensionResults: () => ({ prf: { results: { first: new Uint8Array(16).buffer } } }),
      },
    });
    await expect(getPrfSecret()).rejects.toThrow(PrfUnsupportedError);
  });
});
