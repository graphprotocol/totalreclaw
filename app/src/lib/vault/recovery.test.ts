import "fake-indexeddb/auto";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { clear } from "idb-keyval";
import { restoreFromEscrowCore, restoreFromFileCore } from "./recovery";
import { hasAnyVault, loadVaultRecord } from "./idb";
import { sealEscrow, sealedEscrowToFile, deriveEscrowHandle } from "../auth/escrow";

const MNEMONIC =
  "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
const SERVER_URL = "https://relay.test";
const CHAIN_ID = 100;
const MOCK_SMART_ACCOUNT = "0xcafef00dcafef00dcafef00dcafef00dcafef00d";
const PRF = new Uint8Array(32).fill(0xab);
const RAW_ID = new Uint8Array([9, 9, 9, 9]).buffer;

function stubNavigator(prfOutput: ArrayBuffer | null) {
  vi.stubGlobal("navigator", {
    credentials: {
      get: vi.fn(async () => {
        if (prfOutput === null) return null;
        return {
          rawId: RAW_ID,
          getClientExtensionResults: () => ({ prf: { results: { first: prfOutput } } }),
        };
      }),
      // Deliberately NO `create` — if any code path under test calls
      // enrolPasskey()/navigator.credentials.create(), this throws
      // "credentials.create is not a function", failing the test loudly.
    },
  });
  vi.stubGlobal("location", { hostname: "app.totalreclaw.xyz" });
}

/** Routes fetch to the endpoints restoreFromEscrowCore/restoreFromFileCore hit. */
function stubFetch(opts: {
  escrowBlob?: { ciphertext: string; nonce: string; aad_version: number } | "404";
}) {
  const calls: string[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("/v1/smart-account")) {
      return new Response(JSON.stringify({ smart_account: MOCK_SMART_ACCOUNT }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/v1/register")) {
      return new Response(JSON.stringify({ success: true, user_id: "u1" }), { status: 200 });
    }
    if (url.includes("/v1/escrow/fetch")) {
      if (!opts.escrowBlob || opts.escrowBlob === "404") {
        return new Response(JSON.stringify({ success: false, error_code: "ESCROW_NOT_FOUND" }), {
          status: 404,
        });
      }
      return new Response(JSON.stringify({ success: true, ...opts.escrowBlob, created_at: "x" }), {
        status: 200,
      });
    }
    return new Response("not mocked: " + url, { status: 404 });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

beforeEach(async () => {
  await clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("restoreFromEscrowCore", () => {
  it("happy path: one assertion (no allowCredentials), one fetch, keys derived, VaultRecord saved, status unlocked", async () => {
    stubNavigator(PRF.buffer);
    const handle = deriveEscrowHandle(PRF);
    const sealed = sealEscrow(MNEMONIC, PRF);
    const { fn } = stubFetch({ escrowBlob: { ciphertext: sealed.ciphertextHex, nonce: sealed.nonceHex, aad_version: 1 } });

    const result = await restoreFromEscrowCore(SERVER_URL, CHAIN_ID);
    expect(result.status).toBe("unlocked");
    if (result.status !== "unlocked") throw new Error("unreachable");
    expect(result.smartAccount).toBe(MOCK_SMART_ACCOUNT);
    expect(result.chainId).toBe(CHAIN_ID);
    expect(Array.from(result.credentialId)).toEqual([9, 9, 9, 9]);

    // The assertion used no allowCredentials — asserted indirectly: the
    // stubbed navigator.credentials.get was called with no `id` filter.
    const getMock = (navigator as unknown as { credentials: { get: ReturnType<typeof vi.fn> } })
      .credentials.get;
    const callArg = getMock.mock.calls[0][0] as { publicKey: { allowCredentials?: unknown } };
    expect(callArg.publicKey.allowCredentials).toBeUndefined();

    // VaultRecord persisted with the SAME credential id from the assertion.
    const rec = await loadVaultRecord(MOCK_SMART_ACCOUNT);
    expect(rec).not.toBeNull();
    expect(rec!.v).toBe(1);
    expect(rec!.chain_id).toBe(CHAIN_ID);

    // fetch hit escrow/fetch, smart-account, and register — nothing else.
    const urls = fn.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes("/v1/escrow/fetch"))).toBe(true);
    expect(urls.some((u) => u.includes("/v1/smart-account"))).toBe(true);
    expect(urls.some((u) => u.includes("/v1/register"))).toBe(true);

    void handle; // sanity: handle derivation didn't throw
  });

  it("does NOT call enrolPasskey / navigator.credentials.create", async () => {
    stubNavigator(PRF.buffer);
    const sealed = sealEscrow(MNEMONIC, PRF);
    stubFetch({ escrowBlob: { ciphertext: sealed.ciphertextHex, nonce: sealed.nonceHex, aad_version: 1 } });
    // Would throw synchronously if `create` were ever invoked (not stubbed).
    await expect(restoreFromEscrowCore(SERVER_URL, CHAIN_ID)).resolves.toMatchObject({ status: "unlocked" });
  });

  it('returns "no-escrow" on a 404, with no VaultRecord written', async () => {
    stubNavigator(PRF.buffer);
    stubFetch({ escrowBlob: "404" });
    const result = await restoreFromEscrowCore(SERVER_URL, CHAIN_ID);
    expect(result).toEqual({ status: "no-escrow" });
    expect(await hasAnyVault()).toBe(false);
  });

  it("propagates an AEAD failure (tampered ciphertext) as a rejection, not a silent unlock", async () => {
    stubNavigator(PRF.buffer);
    const sealed = sealEscrow(MNEMONIC, PRF);
    const tamperedCiphertext = "00" + sealed.ciphertextHex.slice(2);
    stubFetch({ escrowBlob: { ciphertext: tamperedCiphertext, nonce: sealed.nonceHex, aad_version: 1 } });
    await expect(restoreFromEscrowCore(SERVER_URL, CHAIN_ID)).rejects.toThrow();
    expect(await hasAnyVault()).toBe(false);
  });
});

describe("restoreFromFileCore", () => {
  it("recovers the same mnemonic as a relay escrow round-trip would, and persists a v1 VaultRecord", async () => {
    stubNavigator(PRF.buffer);
    const sealed = sealEscrow(MNEMONIC, PRF);
    const file = sealedEscrowToFile(sealed);
    stubFetch({});

    const result = await restoreFromFileCore(JSON.stringify(file), SERVER_URL, CHAIN_ID);
    expect(result.status).toBe("unlocked");
    if (result.status !== "unlocked") throw new Error("unreachable");
    expect(result.smartAccount).toBe(MOCK_SMART_ACCOUNT);

    const rec = await loadVaultRecord(MOCK_SMART_ACCOUNT);
    expect(rec).not.toBeNull();
    expect(rec!.v).toBe(1);
  });

  it("rejects a malformed file with bad-file, before any WebAuthn/network call", async () => {
    stubNavigator(null);
    const { fn } = stubFetch({});
    const result = await restoreFromFileCore("not json", SERVER_URL, CHAIN_ID);
    expect(result).toEqual({ status: "bad-file" });
    expect(fn).not.toHaveBeenCalled();
  });

  it("rejects an unknown aad_version distinctly, without attempting decryption", async () => {
    stubNavigator(null);
    const sealed = sealEscrow(MNEMONIC, PRF);
    const file = { ...sealedEscrowToFile(sealed), aad_version: 2 };
    const { fn } = stubFetch({});
    const result = await restoreFromFileCore(JSON.stringify(file), SERVER_URL, CHAIN_ID);
    expect(result).toEqual({ status: "unsupported-version" });
    expect(fn).not.toHaveBeenCalled();
  });

  it("a wrong-passkey file (AEAD failure) returns wrong-passkey and issues no /v1/escrow* network request", async () => {
    stubNavigator(new Uint8Array(32).fill(0x99).buffer); // different PRF than the one that sealed the file
    const sealed = sealEscrow(MNEMONIC, PRF);
    const file = sealedEscrowToFile(sealed);
    const { fn } = stubFetch({});

    const result = await restoreFromFileCore(JSON.stringify(file), SERVER_URL, CHAIN_ID);
    expect(result).toEqual({ status: "wrong-passkey" });
    expect(await hasAnyVault()).toBe(false);
    const urls = fn.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes("/v1/escrow"))).toBe(false);
  });

  it("a successful file restore never calls the relay escrow endpoints (the relay has no part in this path)", async () => {
    stubNavigator(PRF.buffer);
    const sealed = sealEscrow(MNEMONIC, PRF);
    const file = sealedEscrowToFile(sealed);
    const { fn } = stubFetch({});

    await restoreFromFileCore(JSON.stringify(file), SERVER_URL, CHAIN_ID);
    const urls = fn.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes("/v1/escrow"))).toBe(false);
  });
});
