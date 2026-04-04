/**
 * Tests for Client Batching — Multi-Call UserOperations.
 *
 * Tests cover:
 *   - Batch encoding (encodeBatchCalls)
 *   - Batch validation (validateBatchConfig)
 *   - Gas savings estimation (estimateGasSavings)
 *   - Module exports and types
 *   - Edge cases (empty payloads, max batch size, single-item batches)
 */

import {
  encodeBatchCalls,
  validateBatchConfig,
  estimateGasSavings,
  MAX_BATCH_SIZE,
  MIN_BATCH_SIZE,
} from "../src/userop/batcher";
import { encodeFactAsCalldata } from "../src/userop/builder";

// ---------------------------------------------------------------------------
// Test data helpers
// ---------------------------------------------------------------------------

function makePayload(size: number = 256, fill: number = 0xab): Buffer {
  return Buffer.alloc(size, fill);
}

function makePayloads(count: number, size: number = 256): Buffer[] {
  return Array.from({ length: count }, (_, i) =>
    Buffer.alloc(size, (i + 1) & 0xff)
  );
}

const TEST_DATA_EDGE = "0xababababababababababababababababababababab" as `0x${string}`;

// ---------------------------------------------------------------------------
// encodeBatchCalls
// ---------------------------------------------------------------------------

describe("encodeBatchCalls", () => {
  it("should encode a single payload as one call", () => {
    const payloads = [makePayload(32)];
    const calls = encodeBatchCalls(payloads, TEST_DATA_EDGE);

    expect(calls).toHaveLength(1);
    expect(calls[0].to).toBe(TEST_DATA_EDGE);
    expect(calls[0].value).toBe(0n);
    expect(calls[0].data).toMatch(/^0x[0-9a-f]+$/);
  });

  it("should encode multiple payloads as separate calls to the same target", () => {
    const payloads = makePayloads(5);
    const calls = encodeBatchCalls(payloads, TEST_DATA_EDGE);

    expect(calls).toHaveLength(5);
    for (const call of calls) {
      expect(call.to).toBe(TEST_DATA_EDGE);
      expect(call.value).toBe(0n);
      expect(call.data).toMatch(/^0x[0-9a-f]+$/);
    }
  });

  it("should produce unique calldata for different payloads", () => {
    const payloads = [
      Buffer.from("fact-one-encrypted"),
      Buffer.from("fact-two-encrypted"),
      Buffer.from("fact-three-encrypted"),
    ];
    const calls = encodeBatchCalls(payloads, TEST_DATA_EDGE);

    const callDataSet = new Set(calls.map((c) => c.data));
    expect(callDataSet.size).toBe(3);
  });

  it("should produce calldata consistent with encodeFactAsCalldata", () => {
    const payload = makePayload(128);
    const calls = encodeBatchCalls([payload], TEST_DATA_EDGE);
    const singleCalldata = encodeFactAsCalldata(payload);

    expect(calls[0].data).toBe(singleCalldata);
  });

  it("should handle maximum batch size", () => {
    const payloads = makePayloads(MAX_BATCH_SIZE);
    const calls = encodeBatchCalls(payloads, TEST_DATA_EDGE);

    expect(calls).toHaveLength(MAX_BATCH_SIZE);
  });

  it("should handle payloads of varying sizes", () => {
    const payloads = [
      makePayload(32),
      makePayload(128),
      makePayload(512),
      makePayload(1024),
    ];
    const calls = encodeBatchCalls(payloads, TEST_DATA_EDGE);

    expect(calls).toHaveLength(4);
    // Each calldata length should be 2 (0x prefix) + 2 * payload bytes
    expect(calls[0].data.length).toBe(2 + 32 * 2);
    expect(calls[1].data.length).toBe(2 + 128 * 2);
    expect(calls[2].data.length).toBe(2 + 512 * 2);
    expect(calls[3].data.length).toBe(2 + 1024 * 2);
  });

  it("should encode an empty array as empty calls array", () => {
    const calls = encodeBatchCalls([], TEST_DATA_EDGE);
    expect(calls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// validateBatchConfig
// ---------------------------------------------------------------------------

describe("validateBatchConfig", () => {
  it("should accept a valid batch of 1", () => {
    expect(() => validateBatchConfig([makePayload()])).not.toThrow();
  });

  it("should accept a valid batch of MAX_BATCH_SIZE", () => {
    expect(() =>
      validateBatchConfig(makePayloads(MAX_BATCH_SIZE))
    ).not.toThrow();
  });

  it("should reject an empty batch", () => {
    expect(() => validateBatchConfig([])).toThrow(
      "Batch must contain at least 1 encrypted payload"
    );
  });

  it("should reject a null/undefined batch", () => {
    expect(() => validateBatchConfig(null as any)).toThrow(
      "Batch must contain at least 1 encrypted payload"
    );
  });

  it("should reject batch exceeding MAX_BATCH_SIZE", () => {
    const oversized = makePayloads(MAX_BATCH_SIZE + 1);
    expect(() => validateBatchConfig(oversized)).toThrow(
      `Batch size ${MAX_BATCH_SIZE + 1} exceeds maximum of ${MAX_BATCH_SIZE}`
    );
  });

  it("should reject batch with empty payload at any index", () => {
    const payloads = [makePayload(64), Buffer.alloc(0), makePayload(64)];
    expect(() => validateBatchConfig(payloads)).toThrow(
      "Payload at index 1 is empty"
    );
  });

  it("should reject batch where first payload is empty", () => {
    const payloads = [Buffer.alloc(0), makePayload(64)];
    expect(() => validateBatchConfig(payloads)).toThrow(
      "Payload at index 0 is empty"
    );
  });

  it("should reject batch where last payload is empty", () => {
    const payloads = [makePayload(64), makePayload(64), Buffer.alloc(0)];
    expect(() => validateBatchConfig(payloads)).toThrow(
      "Payload at index 2 is empty"
    );
  });
});

// ---------------------------------------------------------------------------
// estimateGasSavings
// ---------------------------------------------------------------------------

describe("estimateGasSavings", () => {
  it("should show no savings for single fact (multicall overhead negates)", () => {
    const result = estimateGasSavings(1);
    // Single fact: batched is actually slightly more expensive due to
    // per-call multicall overhead (2,600 gas) without tx overhead savings.
    // savingsPercent is clamped to 0 (never negative).
    expect(result.savingsPercent).toBe(0);
    expect(result.individualGas).toBeGreaterThan(0);
    expect(result.batchedGas).toBeGreaterThan(0);
  });

  it("should show positive savings for batch of 2+", () => {
    const result = estimateGasSavings(2);
    expect(result.savingsPercent).toBeGreaterThan(0);
    expect(result.batchedGas).toBeLessThan(result.individualGas);
  });

  it("should show increasing savings with larger batches", () => {
    const savings3 = estimateGasSavings(3);
    const savings5 = estimateGasSavings(5);
    const savings10 = estimateGasSavings(10);
    const savings15 = estimateGasSavings(15);

    expect(savings5.savingsPercent).toBeGreaterThan(savings3.savingsPercent);
    expect(savings10.savingsPercent).toBeGreaterThan(savings5.savingsPercent);
    expect(savings15.savingsPercent).toBeGreaterThanOrEqual(
      savings10.savingsPercent
    );
  });

  it("should show significant savings for extraction-cycle batch (15 facts)", () => {
    const result = estimateGasSavings(15);
    // With 15 facts, we expect > 50% gas savings
    expect(result.savingsPercent).toBeGreaterThan(50);
  });

  it("should handle batch size of 0", () => {
    const result = estimateGasSavings(0);
    expect(result.savingsPercent).toBe(0);
    expect(result.individualGas).toBe(0);
    expect(result.batchedGas).toBe(0);
  });

  it("should handle negative batch size gracefully", () => {
    const result = estimateGasSavings(-1);
    expect(result.savingsPercent).toBe(0);
  });

  it("should account for payload size", () => {
    const small = estimateGasSavings(5, 128);
    const large = estimateGasSavings(5, 512);

    // Larger payloads mean more gas per fact, but savings % should be similar
    expect(large.individualGas).toBeGreaterThan(small.individualGas);
    expect(large.batchedGas).toBeGreaterThan(small.batchedGas);
  });
});

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

describe("batch constants", () => {
  it("MAX_BATCH_SIZE should be 15 (matches extraction cap)", () => {
    expect(MAX_BATCH_SIZE).toBe(15);
  });

  it("MIN_BATCH_SIZE should be 1", () => {
    expect(MIN_BATCH_SIZE).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Module exports
// ---------------------------------------------------------------------------

describe("batcher module exports", () => {
  it("should export buildBatchUserOperation as a function", async () => {
    const { buildBatchUserOperation } = await import("../src/userop/batcher");
    expect(typeof buildBatchUserOperation).toBe("function");
  });

  it("should export sendBatchOnChain as a function", async () => {
    const { sendBatchOnChain } = await import("../src/userop/batcher");
    expect(typeof sendBatchOnChain).toBe("function");
  });

  it("should export encodeBatchCalls as a function", async () => {
    const { encodeBatchCalls } = await import("../src/userop/batcher");
    expect(typeof encodeBatchCalls).toBe("function");
  });

  it("should export validateBatchConfig as a function", async () => {
    const { validateBatchConfig } = await import("../src/userop/batcher");
    expect(typeof validateBatchConfig).toBe("function");
  });

  it("should export estimateGasSavings as a function", async () => {
    const { estimateGasSavings } = await import("../src/userop/batcher");
    expect(typeof estimateGasSavings).toBe("function");
  });
});

// ---------------------------------------------------------------------------
// buildBatchUserOperation (input validation only — no live bundler)
// ---------------------------------------------------------------------------

describe("buildBatchUserOperation (validation)", () => {
  it("should reject empty batch", async () => {
    const { buildBatchUserOperation } = await import("../src/userop/batcher");

    await expect(
      buildBatchUserOperation({
        privateKey: Buffer.alloc(32, 0x01),
        dataEdgeAddress: TEST_DATA_EDGE,
        chainId: 84532,
        encryptedPayloads: [],
        serverUrl: "http://localhost:8000",
      })
    ).rejects.toThrow("Batch must contain at least 1 encrypted payload");
  });

  it("should reject batch exceeding MAX_BATCH_SIZE", async () => {
    const { buildBatchUserOperation } = await import("../src/userop/batcher");

    await expect(
      buildBatchUserOperation({
        privateKey: Buffer.alloc(32, 0x01),
        dataEdgeAddress: TEST_DATA_EDGE,
        chainId: 84532,
        encryptedPayloads: makePayloads(MAX_BATCH_SIZE + 1),
        serverUrl: "http://localhost:8000",
      })
    ).rejects.toThrow(`exceeds maximum of ${MAX_BATCH_SIZE}`);
  });

  // Chain ID validation removed — relay routes based on billing tier.

  it("should reject batch with empty payload", async () => {
    const { buildBatchUserOperation } = await import("../src/userop/batcher");

    await expect(
      buildBatchUserOperation({
        privateKey: Buffer.alloc(32, 0x01),
        dataEdgeAddress: TEST_DATA_EDGE,
        chainId: 84532,
        encryptedPayloads: [makePayload(128), Buffer.alloc(0)],
        serverUrl: "http://localhost:8000",
      })
    ).rejects.toThrow("Payload at index 1 is empty");
  });
});

// ---------------------------------------------------------------------------
// Index re-exports
// ---------------------------------------------------------------------------

describe("userop/index re-exports batching", () => {
  it("should re-export all batch functions from userop/index", async () => {
    const userop = await import("../src/userop/index");

    expect(typeof userop.buildBatchUserOperation).toBe("function");
    expect(typeof userop.sendBatchOnChain).toBe("function");
    expect(typeof userop.encodeBatchCalls).toBe("function");
    expect(typeof userop.validateBatchConfig).toBe("function");
    expect(typeof userop.estimateGasSavings).toBe("function");
    expect(userop.MAX_BATCH_SIZE).toBe(15);
    expect(userop.MIN_BATCH_SIZE).toBe(1);
  });
});
