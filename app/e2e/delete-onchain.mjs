/**
 * E2E (A.2 Phase 1): STAGING delete/tombstone round-trip on Gnosis mainnet.
 *
 * Exercises the exact SPA write wire: WASM core encodes the fact + tombstone
 * protobufs and the SimpleAccount.execute() calldata; the relay `/v1/bundler`
 * proxies bundler + paymaster; the UserOp hash + signature come from the same
 * core the browser ships. WebAuthn can't run headless, so this harness signs
 * with the throwaway account's EOA key DIRECTLY (phrase-safe) — the in-browser
 * path signs the identical hash via `withMasterKey` (PRF-unwrapped master key),
 * unit-tested separately in `src/lib/auth/master.test.ts`.
 *
 * PHRASE-SAFETY (L3): a fresh THROWAWAY mnemonic only, cached to a gitignored
 * `.e2e-secret` and reused across runs (staging /v1/register has an IP-global
 * ~19-min 429 window — never re-register). The mnemonic + private key are NEVER
 * printed or logged. Only public data (addresses, ids, userOpHash, DataEdge,
 * subgraph counts) is emitted.
 *
 * Run: cd app && node e2e/delete-onchain.mjs
 */
import { generateMnemonic } from "@scure/bip39";
import { wordlist } from "@scure/bip39/wordlists/english.js";
import { readFileSync, writeFileSync, existsSync, chmodSync } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const core = require("@totalreclaw/core");

const RELAY = process.env.RELAY_URL || "https://api-staging.totalreclaw.xyz";
const CHAIN_ID = 100; // Gnosis mainnet (single-chain)
const STAGING_DATA_EDGE = "0xE7a4D2677B686e13775Ba9092631089e35F0BB91";
const PROD_DATA_EDGE = "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca";
const SECRET_FILE = join(dirname(fileURLToPath(import.meta.url)), "..", ".e2e-secret");

const DUMMY_SIG =
  "0xfffffffffffffffffffffffffffffff0000000000000000000000000000000007aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1c";

const out = (m) => console.log(`[e2e] ${m}`);
let failed = false;
const check = (name, cond) => {
  out(`${cond ? "PASS" : "FAIL"} — ${name}`);
  if (!cond) failed = true;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- throwaway account (phrase-safe cache) ---
function loadOrCreateMnemonic() {
  if (existsSync(SECRET_FILE)) {
    out("reusing cached throwaway account (.e2e-secret) — NOT re-registering");
    return { mnemonic: readFileSync(SECRET_FILE, "utf8").trim(), fresh: false };
  }
  const mnemonic = generateMnemonic(wordlist, 128);
  writeFileSync(SECRET_FILE, mnemonic + "\n", { mode: 0o600 });
  chmodSync(SECRET_FILE, 0o600);
  out("generated a fresh throwaway account → cached to gitignored .e2e-secret");
  return { mnemonic, fresh: true };
}

async function relayFetch(path, opts = {}, authKeyHex, wallet) {
  const headers = {
    "Content-Type": "application/json",
    "X-TotalReclaw-Client": "ts-spa-vault",
    "X-TotalReclaw-Test": "true",
    ...(authKeyHex ? { Authorization: `Bearer ${authKeyHex}` } : {}),
    ...(wallet ? { "X-Wallet-Address": wallet } : {}),
    ...(opts.headers || {}),
  };
  const res = await fetch(`${RELAY}${path}`, { ...opts, headers });
  const text = await res.text();
  return { ok: res.ok, status: res.status, text };
}

// Node reads (eth_call nonce + eth_getCode) go through the relay `/v1/bundler`
// too since relay#37 — same as the browser (src/lib/bundler.ts). No third-party
// RPC is contacted anywhere in this harness.
async function bundlerRpc(authKeyHex, wallet, method, params) {
  const res = await fetch(`${RELAY}/v1/bundler`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-TotalReclaw-Client": "ts-spa-vault",
      "X-TotalReclaw-Test": "true",
      Authorization: `Bearer ${authKeyHex}`,
      "X-Wallet-Address": wallet,
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const json = await res.json();
  if (json.error) throw new Error(`bundler ${method}: ${JSON.stringify(json.error)}`);
  return json.result;
}

async function subgraphFacts(authKeyHex, wallet, ownerLower, includeInactive) {
  const where = includeInactive ? `{ owner: "${ownerLower}" }` : `{ owner: "${ownerLower}", isActive: true }`;
  const query = `{ facts(where: ${where}, first: 1000) { id isActive } }`;
  const { text } = await relayFetch(
    "/v1/subgraph",
    { method: "POST", body: JSON.stringify({ query }) },
    authKeyHex,
    wallet,
  );
  const json = JSON.parse(text);
  return json.data?.facts ?? [];
}

/** Build → sponsor → sign → submit → confirm one execute() UserOp. */
async function submitUserOp(calldataBytes, { authKeyHex, wallet, eoa, dataEdge, needsInitCode }) {
  const entryPoint = core.getEntryPointAddress();
  const callData = `0x${Buffer.from(calldataBytes).toString("hex")}`;

  // initCode for a counterfactual (undeployed) SA — only on the first write.
  let factory = null;
  let factoryData = null;
  if (needsInitCode) {
    const code = await bundlerRpc(authKeyHex, wallet, "eth_getCode", [wallet, "latest"]);
    if (!code || code === "0x" || code === "0x0") {
      factory = core.getSimpleAccountFactory();
      const ownerPadded = eoa.address.slice(2).toLowerCase().padStart(64, "0");
      factoryData = `0x5fbfb9cf${ownerPadded}${"0".repeat(64)}`;
    }
  }

  const gas = await bundlerRpc(authKeyHex, wallet, "pimlico_getUserOperationGasPrice", []);
  const fast = gas.fast;

  const senderPadded = wallet.slice(2).toLowerCase().padStart(64, "0");
  const nonce =
    (await bundlerRpc(authKeyHex, wallet, "eth_call", [
      { to: entryPoint, data: `0x35567e1a${senderPadded}${"0".repeat(64)}` },
      "latest",
    ])) || "0x0";

  const op = {
    sender: wallet,
    nonce,
    callData,
    callGasLimit: "0x0",
    verificationGasLimit: "0x0",
    preVerificationGas: "0x0",
    maxFeePerGas: fast.maxFeePerGas,
    maxPriorityFeePerGas: fast.maxPriorityFeePerGas,
    signature: DUMMY_SIG,
  };
  if (factory) {
    op.factory = factory;
    op.factoryData = factoryData;
  }

  const sponsor = await bundlerRpc(authKeyHex, wallet, "pm_sponsorUserOperation", [op, entryPoint]);
  Object.assign(op, sponsor);

  const hashHex = core.hashUserOp(JSON.stringify(op), entryPoint, BigInt(CHAIN_ID));
  op.signature = `0x${core.signUserOp(hashHex, eoa.private_key)}`; // eoa.private_key never logged

  const userOpHash = await bundlerRpc(authKeyHex, wallet, "eth_sendUserOperation", [op, entryPoint]);

  let receipt = null;
  for (let i = 0; i < 90; i++) {
    await sleep(2000);
    try {
      receipt = await bundlerRpc(authKeyHex, wallet, "eth_getUserOperationReceipt", [userOpHash]);
      if (receipt) break;
    } catch {
      /* not mined yet */
    }
  }
  return { userOpHash, receipt };
}

// --------------------------------------------------------------------------

try {
  const { mnemonic, fresh } = loadOrCreateMnemonic();
  const keys = core.deriveKeysFromMnemonic(mnemonic); // { auth_key, encryption_key, dedup_key, salt }
  const eoa = core.deriveEoa(mnemonic); // { private_key, address } — private_key NEVER logged
  const authKeyHex = keys.auth_key;
  const authKeyHash = createHash("sha256").update(Buffer.from(authKeyHex, "hex")).digest("hex");

  // Smart Account (deterministic CREATE2, via relay).
  const saRes = await relayFetch(`/v1/smart-account?eoa=${eoa.address}&chain=${CHAIN_ID}`);
  const wallet = JSON.parse(saRes.text).smart_account.toLowerCase();
  out(`smart account: ${wallet}`);
  out(`eoa (public): ${eoa.address.toLowerCase()}`);

  // Register only when needed (rider 2: never re-register into the 429 window).
  const billingProbe = await relayFetch(`/v1/billing/status?wallet_address=${wallet}`, {}, authKeyHex, wallet);
  if (billingProbe.status === 401 || fresh) {
    out("registering throwaway account (once)");
    const reg = await relayFetch("/v1/register", {
      method: "POST",
      body: JSON.stringify({ auth_key_hash: authKeyHash, salt: keys.salt }),
    });
    check("register accepted", reg.ok || reg.status === 409);
    await sleep(1500);
  } else {
    out("account already registered — skipping /v1/register");
  }

  const billing = JSON.parse(
    (await relayFetch(`/v1/billing/status?wallet_address=${wallet}`, {}, authKeyHex, wallet)).text,
  );
  const dataEdge = billing.data_edge_address;
  out(`relay data_edge_address: ${dataEdge}  (chain_id ${billing.chain_id}, env ${billing.environment})`);
  check("relay reports the STAGING DataEdge (0xE7a4…), not prod (0xC445…)",
    dataEdge?.toLowerCase() === STAGING_DATA_EDGE.toLowerCase());

  // 1. WRITE a throwaway fact so there is something real to delete.
  const factId = randomUUID();
  const claim = {
    id: factId,
    text: `A2 delete E2E marker ${factId}`,
    type: "claim",
    source: "user",
    created_at: new Date().toISOString(),
    schema_version: "1.0",
  };
  const encB64 = core.encrypt(JSON.stringify(claim), keys.encryption_key);
  const factJson = JSON.stringify({
    id: factId,
    timestamp: claim.created_at,
    owner: wallet,
    encrypted_blob_hex: Buffer.from(encB64, "base64").toString("hex"),
    blind_indices: core.generateBlindIndices(claim.text),
    decay_score: 1.0,
    source: "user",
    content_fp: core.generateContentFingerprint(claim.text, keys.dedup_key),
    agent_id: "a2-delete-e2e",
    encrypted_embedding: null,
    version: 4,
  });
  const writeCalldata = core.encodeSingleCallTo(core.encodeFactProtobuf(factJson), dataEdge);
  out(`writing throwaway fact ${factId} …`);
  const write = await submitUserOp(writeCalldata, { authKeyHex, wallet, eoa, dataEdge, needsInitCode: true });
  check("write UserOp mined", !!write.receipt);
  out(`write userOpHash: ${write.userOpHash}`);

  // Wait for the subgraph to index the new active fact.
  let indexed = false;
  for (let i = 0; i < 30; i++) {
    await sleep(4000);
    const active = await subgraphFacts(authKeyHex, wallet, wallet, false);
    if (active.some((f) => f.id.toLowerCase() === factId.toLowerCase())) {
      indexed = true;
      break;
    }
  }
  check("fact indexed as isActive:true before delete", indexed);
  const activeBefore = (await subgraphFacts(authKeyHex, wallet, wallet, false)).length;
  out(`active facts before delete: ${activeBefore}`);

  // 2. DELETE (tombstone) — the A.2 Phase 1 write under test.
  const tombstone = core.encodeTombstoneProtobuf(factId, wallet, 4);
  const delCalldata = core.encodeSingleCallTo(tombstone, dataEdge);
  out(`deleting fact ${factId} …`);
  const del = await submitUserOp(delCalldata, { authKeyHex, wallet, eoa, dataEdge, needsInitCode: false });
  check("delete UserOp mined", !!del.receipt);
  out(`delete userOpHash: ${del.userOpHash}`);

  // Rider 3a: receipt targeted the STAGING DataEdge (a DataEdge log at 0xE7a4…).
  const logs = del.receipt?.logs ?? del.receipt?.receipt?.logs ?? [];
  const hitStaging = logs.some((l) => (l.address || "").toLowerCase() === STAGING_DATA_EDGE.toLowerCase());
  const hitProd = logs.some((l) => (l.address || "").toLowerCase() === PROD_DATA_EDGE.toLowerCase());
  check("delete receipt emitted a log at the STAGING DataEdge (0xE7a4…)", hitStaging);
  check("delete receipt did NOT touch the prod DataEdge (0xC445…)", !hitProd);

  // Rider 3b: subgraph flips the fact to isActive:false.
  let flipped = false;
  for (let i = 0; i < 30; i++) {
    await sleep(4000);
    const all = await subgraphFacts(authKeyHex, wallet, wallet, true);
    const row = all.find((f) => f.id.toLowerCase() === factId.toLowerCase());
    if (row && row.isActive === false) {
      flipped = true;
      break;
    }
  }
  check("subgraph flipped the fact to isActive:false", flipped);
  const activeAfter = (await subgraphFacts(authKeyHex, wallet, wallet, false)).length;
  out(`active facts after delete: ${activeAfter}`);
  check("active fact count dropped by 1", activeAfter === activeBefore - 1);

  out("--- REDACTED EVIDENCE (no secrets) ---");
  out(JSON.stringify({
    smartAccount: wallet,
    dataEdge,
    chainId: billing.chain_id,
    environment: billing.environment,
    factId,
    writeUserOpHash: write.userOpHash,
    deleteUserOpHash: del.userOpHash,
    activeBefore,
    activeAfter,
    receiptDataEdgeMatchesStaging: hitStaging,
    subgraphIsActiveFalse: flipped,
  }, null, 2));
} catch (e) {
  out(`EXCEPTION: ${e.message}`);
  failed = true;
}

out(failed ? "RESULT: FAIL" : "RESULT: PASS");
process.exit(failed ? 1 : 0);
