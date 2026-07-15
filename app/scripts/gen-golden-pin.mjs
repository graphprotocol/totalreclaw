/**
 * Generator for the fixture-(b) PIN golden vectors in
 * `src/lib/userop.golden.test.ts` (A.2 Phase 2).
 *
 * Run ONLY when the wire format changes intentionally, then paste the printed
 * constants into the test with a note on why:
 *   cd app && node scripts/gen-golden-pin.mjs
 *
 * Everything here is deterministic EXCEPT the XChaCha20 nonce inside the
 * encrypted claim blob — that's why the blob is captured once and frozen as a
 * constant (the test proves it still decrypts to the exact pinned claim, and
 * that the protobuf/calldata/userOpHash over it are byte-stable).
 *
 * Uses the public all-zeros-mnemonic test encryption key (crypto.test.ts) —
 * NOT a secret.
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { xchacha20poly1305 } from "@noble/ciphers/chacha";
import { randomBytes } from "node:crypto";

const require = createRequire(import.meta.url);
const dir = dirname(require.resolve("@totalreclaw/core/web"));
const core = await import("@totalreclaw/core/web");
core.initSync({ module: readFileSync(join(dir, "totalreclaw_core_bg.wasm")) });

const STAGING_DATA_EDGE = "0xE7a4D2677B686e13775Ba9092631089e35F0BB91";
const OWNER = "0x2c0cf74b2b76110708ca431796367779e3738250";
const OLD_ID = "a2golden00000000000000000000dead"; // fixture (a)'s tombstoned id
const NEW_ID = "a2golden22222222222222222222feed";
const FROZEN_TS = "2026-07-15T00:00:00.000+00:00";
const GOLDEN_ENC_KEY_HEX =
  "a58fdc56e1d768461d95cd46b49e03727b2eb342ac558b9f3ebf1255b871f703";

// The superseding pinned claim — the shape rebuildClaimJson emits (validated
// through the same core validator, schema_version + metadata re-attached).
const claimInput = {
  id: NEW_ID,
  text: "A2 golden pin fixture — keep me byte-stable",
  type: "claim",
  source: "user",
  created_at: "2026-06-01T10:20:30.000Z",
  schema_version: "1.0",
  scope: "work",
  importance: 8,
  superseded_by: OLD_ID,
  pin_status: "pinned",
};
const metadata = {
  subtype: "session_crystal",
  session_id: "0197f000-aaaa-7000-8000-abcdefabcdef",
  key_outcomes: ["golden vector shipped"],
};
const validated = JSON.parse(core.validateMemoryClaimV1(JSON.stringify(claimInput)));
validated.schema_version = "1.0";
validated.metadata = metadata;
const canonicalJson = JSON.stringify(validated);

// Encrypt (hex wire: nonce||tag||ct) — mirrors src/lib/crypto.ts encryptBlob.
const key = Uint8Array.from(Buffer.from(GOLDEN_ENC_KEY_HEX, "hex"));
const nonce = randomBytes(24);
const enc = xchacha20poly1305(key, nonce).encrypt(new TextEncoder().encode(canonicalJson));
const ct = enc.slice(0, enc.length - 16);
const tag = enc.slice(enc.length - 16);
const blobHex = Buffer.concat([nonce, tag, ct]).toString("hex");

// Tombstone (frozen field-2 timestamp comes from fixture (a) — regenerate live
// then splice? No: reuse the SAME frozen tombstone constant from the test.)
const GOLDEN_TOMBSTONE_HEX =
  "0a206132676f6c64656e303030303030303030303030303030303030303064656164121d323032362d30372d30395432323a30383a35342e3331332b30303a30301a2a307832633063663734623262373631313037303863613433313739363336373737396533373338323530220031000000000000000038004004";

const pinFactJson = JSON.stringify({
  id: NEW_ID,
  timestamp: FROZEN_TS,
  owner: OWNER,
  encrypted_blob_hex: blobHex,
  blind_indices: ["a2goldenblind00000000000000000001", "a2goldenblind00000000000000000002"],
  decay_score: 1.0,
  source: "spa_pin",
  content_fp: "",
  agent_id: "ts-spa-vault",
  encrypted_embedding: "a2goldenembeddingcopiedforward00",
  version: 4,
});
const pinFactHex = Buffer.from(core.encodeFactProtobuf(pinFactJson)).toString("hex");

const batchHex = Buffer.from(
  core.encodeBatchCallTo(JSON.stringify([GOLDEN_TOMBSTONE_HEX, pinFactHex]), STAGING_DATA_EDGE),
).toString("hex");

const entryPoint = core.getEntryPointAddress();
const op = {
  sender: OWNER,
  nonce: "0x1",
  callData: `0x${batchHex}`,
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
const userOpHash = core.hashUserOp(JSON.stringify(op), entryPoint, 100n);

console.log("GOLDEN_PIN_CLAIM_JSON =", JSON.stringify(canonicalJson));
console.log("GOLDEN_PIN_BLOB_HEX =", JSON.stringify(blobHex));
console.log("GOLDEN_PIN_FACT_PROTOBUF_HEX =", JSON.stringify(pinFactHex));
console.log("GOLDEN_PIN_BATCH_CALLDATA_HEX =", JSON.stringify(batchHex));
console.log("ENTRYPOINT =", entryPoint);
console.log("GOLDEN_PIN_USEROP_HASH =", JSON.stringify(userOpHash));
