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

// ---------------------------------------------------------------------------
// Fixture (b) made REAL (A.2 Phase 2): a frozen 2-call PIN supersession batch —
// [tombstone(old fact), superseding pinned claim] — with pinned protobuf,
// executeBatch calldata, and v0.7 userOpHash. Regenerate deliberately with
// `scripts/gen-golden-pin.mjs` on an intentional wire change (note why).
// ---------------------------------------------------------------------------

const NEW_PIN_ID = "a2golden22222222222222222222feed";
const GOLDEN_ENC_KEY_HEX =
  "a58fdc56e1d768461d95cd46b49e03727b2eb342ac558b9f3ebf1255b871f703";

// The exact canonical claim JSON inside the frozen encrypted blob below
// (validate-then-reattach output — schema_version + Crystal metadata present).
const GOLDEN_PIN_CLAIM_JSON =
  "{\"id\":\"a2golden22222222222222222222feed\",\"text\":\"A2 golden pin fixture — keep me byte-stable\",\"type\":\"claim\",\"source\":\"user\",\"created_at\":\"2026-06-01T10:20:30.000Z\",\"scope\":\"work\",\"importance\":8,\"superseded_by\":\"a2golden00000000000000000000dead\",\"pin_status\":\"pinned\",\"schema_version\":\"1.0\",\"metadata\":{\"subtype\":\"session_crystal\",\"session_id\":\"0197f000-aaaa-7000-8000-abcdefabcdef\",\"key_outcomes\":[\"golden vector shipped\"]}}";

// Captured ONCE from encryptBlob(GOLDEN_PIN_CLAIM_JSON) with the public
// all-zeros-mnemonic test key — frozen because the XChaCha20 nonce is random.
const GOLDEN_PIN_BLOB_HEX =
  "007ec133b1c3d2273e001e2d284d14a29cf79cc4ecc9f33d43bc266d61668d1146a90d86c20736a591d6b6641a6ac63dbdee1dec9d363dcb523b7877b6888316782e67d9c647ec044cd485461ace626c5350a8e35b6ba0f6027b39d70c0bf375caa4f2b9ffd8c4eb2c4de37abadade3d58cd3db19d3002aa2a85af1bc17d9ea6c0a919c429b3fb4a6eca953d5a0cdfa0604297de862af291c0d63fb33b1ec8877bf74c8076465a9e0e1805a3eefacbc177d92871d701315a59b5496667065abb0212d34bce7deb31db7a59460e18be114d3fb2b4a66082f150877f513f974694b470338def5548c95d5727ed1fc3d206ebb397e36cdc6982c29d95e4a94206014614098ba096dae11395ede09db24b2ee9885a8f049f92b74de310bc146ce91bb0eeb9bc503730a333629a065fb289745929743a3a682d9add4c50ca35d58054f58a27cfa0f03d03d6a673eda437e140b2a4ac798113c5736e99f9165ca66a52bec7937339f555782e05c0b39aa7ceeb607218704424702e6baae3e1e79bfd4ee482dd5d9dcfc9d821f4efcbd8a849e0c5e35385c874a7f0b7c5fb2f0acd23e7a984997cdc8b38678b70a5326d8cbf0b37ccdf02533912b3f4e4e35fd248ba2ed5796f8e0e608f78ef048afd242bbf23f90744";

// Frozen protobuf-v4 fact payload over that blob: blind indices + encrypted
// embedding COPIED FORWARD (the pin write's zero-search-degradation contract),
// decay_score=1.0, source=spa_pin, agent_id=ts-spa-vault, frozen timestamp.
const GOLDEN_PIN_FACT_TS = "2026-07-15T00:00:00.000+00:00";
const GOLDEN_PIN_BLIND_INDICES = [
  "a2goldenblind00000000000000000001",
  "a2goldenblind00000000000000000002",
];
const GOLDEN_PIN_EMBEDDING = "a2goldenembeddingcopiedforward00";
const GOLDEN_PIN_FACT_PROTOBUF_HEX =
  "0a206132676f6c64656e323232323232323232323232323232323232323266656564121d323032362d30372d31355430303a30303a30302e3030302b30303a30301a2a30783263306366373462326237363131303730386361343331373936333637373739653337333832353022d303007ec133b1c3d2273e001e2d284d14a29cf79cc4ecc9f33d43bc266d61668d1146a90d86c20736a591d6b6641a6ac63dbdee1dec9d363dcb523b7877b6888316782e67d9c647ec044cd485461ace626c5350a8e35b6ba0f6027b39d70c0bf375caa4f2b9ffd8c4eb2c4de37abadade3d58cd3db19d3002aa2a85af1bc17d9ea6c0a919c429b3fb4a6eca953d5a0cdfa0604297de862af291c0d63fb33b1ec8877bf74c8076465a9e0e1805a3eefacbc177d92871d701315a59b5496667065abb0212d34bce7deb31db7a59460e18be114d3fb2b4a66082f150877f513f974694b470338def5548c95d5727ed1fc3d206ebb397e36cdc6982c29d95e4a94206014614098ba096dae11395ede09db24b2ee9885a8f049f92b74de310bc146ce91bb0eeb9bc503730a333629a065fb289745929743a3a682d9add4c50ca35d58054f58a27cfa0f03d03d6a673eda437e140b2a4ac798113c5736e99f9165ca66a52bec7937339f555782e05c0b39aa7ceeb607218704424702e6baae3e1e79bfd4ee482dd5d9dcfc9d821f4efcbd8a849e0c5e35385c874a7f0b7c5fb2f0acd23e7a984997cdc8b38678b70a5326d8cbf0b37ccdf02533912b3f4e4e35fd248ba2ed5796f8e0e608f78ef048afd242bbf23f907442a216132676f6c64656e626c696e6430303030303030303030303030303030303030312a216132676f6c64656e626c696e64303030303030303030303030303030303030303231000000000000f03f380140046a206132676f6c64656e656d62656464696e67636f70696564666f72776172643030";

// executeBatch([tombstone(v4), pinnedClaim(v4)]) → staging DataEdge.
const GOLDEN_PIN_BATCH_CALLDATA_HEX =
  "47e1da2a000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000c000000000000000000000000000000000000000000000000000000000000001200000000000000000000000000000000000000000000000000000000000000002000000000000000000000000e7a4d2677b686e13775ba9092631089e35f0bb91000000000000000000000000e7a4d2677b686e13775ba9092631089e35f0bb910000000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000004000000000000000000000000000000000000000000000000000000000000000e0000000000000000000000000000000000000000000000000000000000000007c0a206132676f6c64656e303030303030303030303030303030303030303064656164121d323032362d30372d30395432323a30383a35342e3331332b30303a30301a2a3078326330636637346232623736313130373038636134333137393633363737373965333733383235302200310000000000000000380040040000000000000000000000000000000000000000000000000000000000000000000002b80a206132676f6c64656e323232323232323232323232323232323232323266656564121d323032362d30372d31355430303a30303a30302e3030302b30303a30301a2a30783263306366373462326237363131303730386361343331373936333637373739653337333832353022d303007ec133b1c3d2273e001e2d284d14a29cf79cc4ecc9f33d43bc266d61668d1146a90d86c20736a591d6b6641a6ac63dbdee1dec9d363dcb523b7877b6888316782e67d9c647ec044cd485461ace626c5350a8e35b6ba0f6027b39d70c0bf375caa4f2b9ffd8c4eb2c4de37abadade3d58cd3db19d3002aa2a85af1bc17d9ea6c0a919c429b3fb4a6eca953d5a0cdfa0604297de862af291c0d63fb33b1ec8877bf74c8076465a9e0e1805a3eefacbc177d92871d701315a59b5496667065abb0212d34bce7deb31db7a59460e18be114d3fb2b4a66082f150877f513f974694b470338def5548c95d5727ed1fc3d206ebb397e36cdc6982c29d95e4a94206014614098ba096dae11395ede09db24b2ee9885a8f049f92b74de310bc146ce91bb0eeb9bc503730a333629a065fb289745929743a3a682d9add4c50ca35d58054f58a27cfa0f03d03d6a673eda437e140b2a4ac798113c5736e99f9165ca66a52bec7937339f555782e05c0b39aa7ceeb607218704424702e6baae3e1e79bfd4ee482dd5d9dcfc9d821f4efcbd8a849e0c5e35385c874a7f0b7c5fb2f0acd23e7a984997cdc8b38678b70a5326d8cbf0b37ccdf02533912b3f4e4e35fd248ba2ed5796f8e0e608f78ef048afd242bbf23f907442a216132676f6c64656e626c696e6430303030303030303030303030303030303030312a216132676f6c64656e626c696e64303030303030303030303030303030303030303231000000000000f03f380140046a206132676f6c64656e656d62656464696e67636f70696564666f727761726430300000000000000000";

// v0.7 UserOp over the batch calldata (Gnosis chainId 100) — frozen hash.
const GOLDEN_PIN_USEROP_HASH =
  "fc974638d47b36133e53bbbec3b770a728a6e4bd37295612354859c0ab42adb6";

describe("golden vectors — fixture (b) REAL: 2-call pin supersession batch", () => {
  it("the frozen encrypted blob still decrypts to the exact pinned claim (metadata intact)", async () => {
    const { decryptBlob } = await import("./crypto");
    const key = fromHex(GOLDEN_ENC_KEY_HEX);
    const plaintext = decryptBlob(GOLDEN_PIN_BLOB_HEX, key);
    expect(plaintext).toBe(GOLDEN_PIN_CLAIM_JSON);
    const claim = JSON.parse(plaintext);
    expect(claim.pin_status).toBe("pinned");
    expect(claim.superseded_by).toBe(ID);
    expect(claim.metadata.subtype).toBe("session_crystal");
    expect(claim.metadata.session_id).toBe("0197f000-aaaa-7000-8000-abcdefabcdef");
  });

  it("the pinned-claim fact protobuf (v4, embedding+indices copied forward) is byte-frozen", () => {
    const got = toHex(
      core.encodeFactProtobuf(
        JSON.stringify({
          id: NEW_PIN_ID,
          timestamp: GOLDEN_PIN_FACT_TS,
          owner: OWNER,
          encrypted_blob_hex: GOLDEN_PIN_BLOB_HEX,
          blind_indices: GOLDEN_PIN_BLIND_INDICES,
          decay_score: 1.0,
          source: "spa_pin",
          content_fp: "",
          agent_id: "ts-spa-vault",
          encrypted_embedding: GOLDEN_PIN_EMBEDDING,
          version: 4,
        }),
      ),
    );
    expect(got).toBe(GOLDEN_PIN_FACT_PROTOBUF_HEX);
    // Copy-forward is visible on the wire: field 13 carries the old embedding.
    expect(got).toContain(Buffer.from(GOLDEN_PIN_EMBEDDING).toString("hex"));
  });

  it("executeBatch([tombstone(old), pinnedClaim(new)]) calldata is byte-frozen", () => {
    const got = toHex(
      core.encodeBatchCallTo(
        JSON.stringify([GOLDEN_TOMBSTONE_HEX, GOLDEN_PIN_FACT_PROTOBUF_HEX]),
        STAGING_DATA_EDGE,
      ),
    );
    expect(got).toBe(GOLDEN_PIN_BATCH_CALLDATA_HEX);
    expect(got.slice(0, 8)).toBe("47e1da2a"); // executeBatch(address[],uint256[],bytes[])
    // Both inner calls target the staging DataEdge.
    expect(got.split("e7a4d2677b686e13775ba9092631089e35f0bb91").length - 1).toBe(2);
  });

  it("the v0.7 userOpHash over the pin batch (Gnosis, chainId 100) is byte-frozen", () => {
    const op = {
      sender: OWNER,
      nonce: "0x1",
      callData: `0x${GOLDEN_PIN_BATCH_CALLDATA_HEX}`,
      callGasLimit: "0x30d40",
      verificationGasLimit: "0x186a0",
      preVerificationGas: "0xc350",
      maxFeePerGas: "0xf4240",
      maxPriorityFeePerGas: "0x7a120",
      paymaster: "0x0000000000000039cd5e8ae05257ce51c473ddd1",
      paymasterVerificationGasLimit: "0x186a0",
      paymasterPostOpGasLimit: "0xc350",
      paymasterData: "0xabcd",
      signature: "0x" + "00".repeat(65),
    };
    const hash = core.hashUserOp(
      JSON.stringify(op),
      "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
      100n,
    );
    expect(hash).toBe(GOLDEN_PIN_USEROP_HASH);
  });
});
