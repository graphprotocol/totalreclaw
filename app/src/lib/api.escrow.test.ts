import { describe, it, expect, vi, afterEach } from "vitest";
import { putEscrow, fetchEscrow, listEscrow, deleteEscrow } from "./api";
import type { SealedEscrow } from "./auth/escrow";
import type { SessionKeys } from "./types";

const KEYS: SessionKeys = {
  authKey: new Uint8Array(32).fill(1),
  encryptionKey: new Uint8Array(32).fill(2),
  authKeyHex: "aa".repeat(32),
  eoaAddress: "0x0",
  walletAddress: "0xabc0000000000000000000000000000000dead",
  chainId: 100,
};

const SEALED: SealedEscrow = {
  lookupHashHex: "11".repeat(32),
  ciphertextHex: "22".repeat(48),
  nonceHex: "33".repeat(24),
  aadVersion: 1,
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(impl: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(impl);
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("putEscrow", () => {
  it("sends Authorization + X-Wallet-Address and the sealed envelope fields", async () => {
    const fetchMock = stubFetch((_url, init) =>
      jsonResponse(200, { success: true, escrow_id: "esc-1", created: true }),
    );
    const result = await putEscrow(KEYS, SEALED, "Passkey backup");
    expect(result).toEqual({ escrowId: "esc-1", created: true });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/v1/escrow");
    expect(String(url)).not.toContain("/v1/escrow/fetch");
    const headers = init!.headers as Record<string, string>;
    expect(headers.Authorization).toBe(`Bearer ${KEYS.authKeyHex}`);
    expect(headers["X-Wallet-Address"]).toBe(KEYS.walletAddress);

    const body = JSON.parse(init!.body as string);
    expect(body).toEqual({
      lookup_hash: SEALED.lookupHashHex,
      ciphertext: SEALED.ciphertextHex,
      nonce: SEALED.nonceHex,
      aad_version: 1,
      label: "Passkey backup",
    });
  });

  it("sends label: null when omitted", async () => {
    const fetchMock = stubFetch(() => jsonResponse(200, { success: true, escrow_id: "e", created: true }));
    await putEscrow(KEYS, SEALED);
    const body = JSON.parse(fetchMock.mock.calls[0][1]!.body as string);
    expect(body.label).toBeNull();
  });

  it("throws on a non-2xx response, including the error_code", async () => {
    stubFetch(() => jsonResponse(409, { success: false, error_code: "ESCROW_HANDLE_CONFLICT" }));
    await expect(putEscrow(KEYS, SEALED)).rejects.toThrow(/ESCROW_HANDLE_CONFLICT/);
  });
});

describe("fetchEscrow", () => {
  const HANDLE = new Uint8Array(32).fill(0xab);
  const HANDLE_HEX = "ab".repeat(32);

  it("sends the handle in the BODY — the request URL never contains a 64-hex substring", async () => {
    const fetchMock = stubFetch(() =>
      jsonResponse(200, {
        success: true,
        ciphertext: "cc".repeat(10),
        nonce: "dd".repeat(24),
        aad_version: 1,
        created_at: "2026-08-02T10:00:00Z",
      }),
    );
    await fetchEscrow(HANDLE);

    const [url, init] = fetchMock.mock.calls[0];
    // Regression test for the single worst mistake available here (§10.1).
    expect(/[0-9a-f]{64}/i.test(String(url))).toBe(false);
    const body = JSON.parse(init!.body as string);
    expect(body.lookup_id).toBe(HANDLE_HEX);
  });

  it("sends NO Authorization header — the handle alone is the authorization", async () => {
    const fetchMock = stubFetch(() =>
      jsonResponse(200, { success: true, ciphertext: "cc", nonce: "dd", aad_version: 1, created_at: "x" }),
    );
    await fetchEscrow(HANDLE);
    const [, init] = fetchMock.mock.calls[0];
    const headers = init!.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    expect(headers["X-TotalReclaw-Client"]).toBeDefined();
  });

  it("returns null on 404 rather than throwing", async () => {
    stubFetch(() => jsonResponse(404, { success: false, error_code: "ESCROW_NOT_FOUND" }));
    await expect(fetchEscrow(HANDLE)).resolves.toBeNull();
  });

  it("returns the ciphertext/nonce/aadVersion on a hit", async () => {
    stubFetch(() =>
      jsonResponse(200, {
        success: true,
        ciphertext: "ee".repeat(10),
        nonce: "ff".repeat(24),
        aad_version: 1,
        created_at: "2026-08-02T10:00:00Z",
      }),
    );
    const result = await fetchEscrow(HANDLE);
    expect(result).toEqual({ ciphertextHex: "ee".repeat(10), nonceHex: "ff".repeat(24), aadVersion: 1 });
  });

  it("throws on a non-404 error status", async () => {
    stubFetch(() => jsonResponse(429, { success: false }));
    await expect(fetchEscrow(HANDLE)).rejects.toThrow(/429/);
  });
});

describe("listEscrow", () => {
  it("sends Authorization and maps records to camelCase", async () => {
    const fetchMock = stubFetch(() =>
      jsonResponse(200, {
        success: true,
        records: [
          {
            escrow_id: "e1",
            label: "Passkey backup",
            created_at: "2026-08-02T10:00:00Z",
            updated_at: "2026-08-02T10:00:00Z",
            last_fetched_at: null,
          },
        ],
      }),
    );
    const records = await listEscrow(KEYS);
    expect(records).toEqual([
      {
        escrowId: "e1",
        label: "Passkey backup",
        createdAt: "2026-08-02T10:00:00Z",
        updatedAt: "2026-08-02T10:00:00Z",
        lastFetchedAt: null,
      },
    ]);
    const [, init] = fetchMock.mock.calls[0];
    const headers = init!.headers as Record<string, string>;
    expect(headers.Authorization).toBe(`Bearer ${KEYS.authKeyHex}`);
    expect(headers["X-Wallet-Address"]).toBe(KEYS.walletAddress);
  });
});

describe("deleteEscrow", () => {
  it("sends Authorization and a DELETE to /v1/escrow/:id, returns the `deleted` flag", async () => {
    const fetchMock = stubFetch((url, init) => {
      expect(init?.method).toBe("DELETE");
      return jsonResponse(200, { success: true, deleted: true });
    });
    const result = await deleteEscrow(KEYS, "esc-1");
    expect(result).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/v1/escrow/esc-1");
    const headers = init!.headers as Record<string, string>;
    expect(headers.Authorization).toBe(`Bearer ${KEYS.authKeyHex}`);
  });

  it("returns false when the relay reports deleted: false (idempotent no-op)", async () => {
    stubFetch(() => jsonResponse(200, { success: true, deleted: false }));
    expect(await deleteEscrow(KEYS, "gone")).toBe(false);
  });
});
