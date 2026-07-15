/**
 * Frozen golden vectors for the A.2 write wire format (defense in depth, PR-C
 * bip39-parity spirit).
 *
 * These are PINNED constants, NOT a live core-vs-core comparison. They exercise
 * the exact web-target `@totalreclaw/core` WASM the browser ships (the npm
 * `./web` subpath export), so a future core bump that shifts the wire format —
 * ABI `execute`/`executeBatch` calldata, the tombstone shape, the ERC-4337 v0.7
 * UserOp hash, or the ECDSA signature — fails LOUDLY here in the SPA's own CI,
 * not silently at the paymaster. On an intentional wire change, regenerate with
 * `scratchpad/gen-golden.mjs` and update these constants with a note on why.
 *
 * The hashUserOp + signUserOp vectors are independent viem-derived references
 * (verbatim from `rust/totalreclaw-core/src/userop.rs`), so they also cross-
 * check the shipped WASM against viem's `getUserOperationHash` / `signMessage`.
 * The signing key is the public "abandon…about" Hardhat test key — NOT a secret.
 */
import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

// npm web-target core (`./web` subpath), initialized synchronously from disk
// bytes (node) — the browser's `default()` fetch init doesn't apply here.
type Core = typeof import("@totalreclaw/core/web");
let core: Core;

beforeAll(async () => {
  const require = createRequire(import.meta.url);
  const dir = dirname(require.resolve("@totalreclaw/core/web"));
  core = (await import("@totalreclaw/core/web")) as unknown as Core;
  (core as unknown as { initSync: (m: { module: Uint8Array }) => void }).initSync({
    module: readFileSync(join(dir, "totalreclaw_core_bg.wasm")),
  });
});

const toHex = (u8: Uint8Array) => Buffer.from(u8).toString("hex");
const fromHex = (h: string) => new Uint8Array(Buffer.from(h, "hex"));

// --- frozen inputs (see scratchpad/gen-golden.mjs) ---
const STAGING_DATA_EDGE = "0xE7a4D2677B686e13775Ba9092631089e35F0BB91";
const PROD_DATA_EDGE = "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca";
const ID = "a2golden00000000000000000000dead";
const ID2 = "a2golden11111111111111111111beef";
const OWNER = "0x2c0cf74b2b76110708ca431796367779e3738250";

// A real v4 tombstone captured from `encodeTombstoneProtobuf(ID, OWNER, 4)`
// (field 2 = the capture-time RFC3339 timestamp, frozen here for determinism).
const GOLDEN_TOMBSTONE_HEX =
  "0a206132676f6c64656e303030303030303030303030303030303030303064656164121d323032362d30372d30395432323a30383a35342e3331332b30303a30301a2a307832633063663734623262373631313037303863613433313739363336373737396533373338323530220031000000000000000038004004";
const GOLDEN_TOMBSTONE2_HEX =
  "0a206132676f6c64656e313131313131313131313131313131313131313162656566121d323032362d30372d30395432323a30383a35342e3331342b30303a30301a2a307832633063663734623262373631313037303863613433313739363336373737396533373338323530220031000000000000000038004004";

// Fixture (a): single-call execute() calldata over the delete tombstone → staging DataEdge.
const GOLDEN_SINGLE_CALLDATA_HEX =
  "b61d27f6000000000000000000000000e7a4d2677b686e13775ba9092631089e35f0bb9100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000060000000000000000000000000000000000000000000000000000000000000007c0a206132676f6c64656e303030303030303030303030303030303030303064656164121d323032362d30372d30395432323a30383a35342e3331332b30303a30301a2a30783263306366373462326237363131303730386361343331373936333637373739653337333832353022003100000000000000003800400400000000";

// Fixture (b): 2-call executeBatch() calldata over two tombstones → staging DataEdge.
const GOLDEN_BATCH_CALLDATA_HEX =
  "47e1da2a000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000c000000000000000000000000000000000000000000000000000000000000001200000000000000000000000000000000000000000000000000000000000000002000000000000000000000000e7a4d2677b686e13775ba9092631089e35f0bb91000000000000000000000000e7a4d2677b686e13775ba9092631089e35f0bb910000000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000004000000000000000000000000000000000000000000000000000000000000000e0000000000000000000000000000000000000000000000000000000000000007c0a206132676f6c64656e303030303030303030303030303030303030303064656164121d323032362d30372d30395432323a30383a35342e3331332b30303a30301a2a30783263306366373462326237363131303730386361343331373936333637373739653337333832353022003100000000000000003800400400000000000000000000000000000000000000000000000000000000000000000000007c0a206132676f6c64656e313131313131313131313131313131313131313162656566121d323032362d30372d30395432323a30383a35342e3331342b30303a30301a2a30783263306366373462326237363131303730386361343331373936333637373739653337333832353022003100000000000000003800400400000000";

describe("golden vectors — tombstone shape (fixture a input is a real delete)", () => {
  it("the frozen tombstone is a v4 tombstone (is_active=false, version=4, empty blob)", () => {
    const h = GOLDEN_TOMBSTONE_HEX;
    expect(h.startsWith("0a20" + Buffer.from(ID).toString("hex"))).toBe(true); // field 1 = id
    expect(h.includes("2200")).toBe(true); // field 4 = empty encrypted_blob
    expect(h.endsWith("38004004")).toBe(true); // field 7 is_active=0, field 8 version=4
  });

  it("the LIVE encoder still emits the same tombstone shape (only field-2 timestamp differs)", () => {
    const live = toHex(core.encodeTombstoneProtobuf(ID, OWNER, 4));
    const field1 = "0a20" + Buffer.from(ID).toString("hex");
    const tail =
      "1a2a" + Buffer.from(OWNER).toString("hex") + "2200" + "31" + "00".repeat(8) + "3800" + "4004";
    expect(live.startsWith(field1)).toBe(true);
    expect(live.endsWith(tail)).toBe(true);
  });
});

describe("golden vectors — calldata (execute / executeBatch) → staging DataEdge", () => {
  it("(a) single-call execute() over the delete tombstone is byte-frozen", () => {
    const got = toHex(core.encodeSingleCallTo(fromHex(GOLDEN_TOMBSTONE_HEX), STAGING_DATA_EDGE));
    expect(got).toBe(GOLDEN_SINGLE_CALLDATA_HEX);
    expect(got.slice(0, 8)).toBe("b61d27f6"); // execute(address,uint256,bytes)
    expect(got).toContain("e7a4d2677b686e13775ba9092631089e35f0bb91"); // staging DataEdge
  });

  it("(b) 2-call executeBatch() over two tombstones is byte-frozen", () => {
    const got = toHex(
      core.encodeBatchCallTo(
        JSON.stringify([GOLDEN_TOMBSTONE_HEX, GOLDEN_TOMBSTONE2_HEX]),
        STAGING_DATA_EDGE,
      ),
    );
    expect(got).toBe(GOLDEN_BATCH_CALLDATA_HEX);
    expect(got.slice(0, 8)).toBe("47e1da2a"); // executeBatch(address[],uint256[],bytes[])
  });

  it("a different DataEdge (prod) yields different calldata — no silent wrong-target write", () => {
    const staging = toHex(core.encodeSingleCallTo(fromHex(GOLDEN_TOMBSTONE_HEX), STAGING_DATA_EDGE));
    const prod = toHex(core.encodeSingleCallTo(fromHex(GOLDEN_TOMBSTONE_HEX), PROD_DATA_EDGE));
    expect(staging).not.toBe(prod);
    expect(prod).toContain("c445af1d4eb9fce4e1e61fe96ea7b8febf03c5ca");
  });

  it("distinct ids produce distinct tombstones (batch fixture isn't a duplicate)", () => {
    expect(GOLDEN_TOMBSTONE_HEX).not.toBe(GOLDEN_TOMBSTONE2_HEX);
    expect(GOLDEN_TOMBSTONE_HEX).toContain(Buffer.from(ID).toString("hex"));
    expect(GOLDEN_TOMBSTONE2_HEX).toContain(Buffer.from(ID2).toString("hex"));
  });
});

describe("golden vectors — UserOp hash + signature (viem-derived references)", () => {
  const VIEM_USEROP = {
    sender: "0x949bc374325a4f41e46e8e78a07d910332934542",
    nonce: "0x0",
    factory: "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985",
    factoryData:
      "0x5fbfb9cf0000000000000000000000008eb626f727e92a73435f2b85dd6fd0c6da5dbb720000000000000000000000000000000000000000000000000000000000000000",
    callData: "0xb61d27f6",
    callGasLimit: "0x186a0",
    verificationGasLimit: "0x30d40",
    preVerificationGas: "0xc350",
    maxFeePerGas: "0xf4240",
    maxPriorityFeePerGas: "0x7a120",
    paymaster: "0x0000000000000039cd5e8ae05257ce51c473ddd1",
    paymasterVerificationGasLimit: "0x186a0",
    paymasterPostOpGasLimit: "0xc350",
    paymasterData: "0xabcd",
    signature: "0x" + "00".repeat(65),
  };

  it("hashUserOp matches viem's getUserOperationHash (v0.7)", () => {
    const hash = core.hashUserOp(
      JSON.stringify(VIEM_USEROP),
      "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
      84532n,
    );
    expect(hash).toBe("4525d2a8a555a1a56f6313735b83fe3ee55f81d504d905ea85613524973f97c2");
  });

  it("signUserOp matches viem's signMessage for the public abandon-mnemonic test key", () => {
    // NOT a real vault key — the universally-known Hardhat/abandon test key.
    const sig = core.signUserOp(
      "6de60c2ca586227294ffce39e30a3c6ec8ddf6ae01d0d579344e8d2e2dbf8b26",
      "1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727",
    );
    expect(sig).toBe(
      "a5ad7388dd018236a6cfc25556f35d0d05fff7a9a59ef29fef65b1855298f767107418521a5ca48e56a4d5de67e954df5d6dd49fe98eba3d1c45ad22eeae3fd11c",
    );
  });
});
