import {
  encodeFactAsCalldata,
  ENTRYPOINT_V07_ADDRESS,
  SIMPLE_ACCOUNT_FACTORY_V07_ADDRESS,
  SIMPLE_ACCOUNT_IMPLEMENTATION_ADDRESS,
} from "../src/userop/builder";

describe("UserOperation Builder", () => {
  describe("encodeFactAsCalldata", () => {
    it("should encode encrypted blob as hex calldata", () => {
      const encryptedBlob = Buffer.from("test-encrypted-payload");
      const calldata = encodeFactAsCalldata(encryptedBlob);

      expect(calldata).toMatch(/^0x[0-9a-f]+$/);
      // Calldata should contain the raw encrypted bytes
      expect(calldata).toBe("0x" + encryptedBlob.toString("hex"));
    });

    it("should handle empty payload", () => {
      const calldata = encodeFactAsCalldata(Buffer.alloc(0));
      expect(calldata).toBe("0x");
    });

    it("should handle large payloads (1KB)", () => {
      const largePayload = Buffer.alloc(1024, 0xff);
      const calldata = encodeFactAsCalldata(largePayload);
      expect(calldata.length).toBe(2 + 1024 * 2); // 0x + hex chars
    });

    it("should correctly encode known bytes", () => {
      const payload = Buffer.from([0xde, 0xad, 0xbe, 0xef]);
      const calldata = encodeFactAsCalldata(payload);
      expect(calldata).toBe("0xdeadbeef");
    });
  });

  describe("well-known addresses", () => {
    it("should export the correct EntryPoint v0.7 address", () => {
      expect(ENTRYPOINT_V07_ADDRESS).toBe(
        "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
      );
    });

    it("should export the correct SimpleAccountFactory v0.7 address", () => {
      expect(SIMPLE_ACCOUNT_FACTORY_V07_ADDRESS).toBe(
        "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985"
      );
    });

    it("should export the correct SimpleAccount implementation address", () => {
      expect(SIMPLE_ACCOUNT_IMPLEMENTATION_ADDRESS).toBe(
        "0xe6Cae83BdE06E4c305530e199D7217f42808555B"
      );
    });
  });

  describe("buildUserOperation (mocked)", () => {
    // These tests validate the module structure and types.
    // Full integration tests require a live bundler and are in tests/e2e-functional/.

    it("should export buildUserOperation as a function", async () => {
      const { buildUserOperation } = await import("../src/userop/builder");
      expect(typeof buildUserOperation).toBe("function");
    });

    it("should export submitUserOperation as a function", async () => {
      const { submitUserOperation } = await import("../src/userop/builder");
      expect(typeof submitUserOperation).toBe("function");
    });

    it("should export getSmartAccountAddress as a function", async () => {
      const { getSmartAccountAddress } = await import("../src/userop/builder");
      expect(typeof getSmartAccountAddress).toBe("function");
    });

    it("should export getSmartAccountAddressFromKey as a function", async () => {
      const { getSmartAccountAddressFromKey } = await import(
        "../src/userop/builder"
      );
      expect(typeof getSmartAccountAddressFromKey).toBe("function");
    });

    it("should export sendFactOnChain as a function", async () => {
      const { sendFactOnChain } = await import("../src/userop/builder");
      expect(typeof sendFactOnChain).toBe("function");
    });

    // Chain ID validation removed — relay routes based on billing tier.
    // Any chain ID is accepted; the relay determines the correct chain.
  });
});
