/**
 * E2E (A.2 Phase 2): STAGING pin/unpin round-trip — 2-call executeBatch
 * supersession on Gnosis mainnet via the staging relay.
 *
 * Exercises the exact SPA write wire: decrypt old blob → rebuild the claim
 * (full carry-forward + metadata verbatim, core-validated) → re-encrypt →
 * [tombstone(old,v4), newClaim(v4)] in ONE UserOp via encodeBatchCallTo →
 * relay /v1/bundler → subgraph. Embedding + blind indices are copied forward
 * from the old on-chain fact and asserted equal after indexing.
 *
 * WebAuthn can't run headless, so this harness signs with the throwaway
 * account's EOA key DIRECTLY (phrase-safe) — the in-browser path signs the
 * identical hash via `withMasterKey`, unit-tested in src/lib/auth/master.test.ts.
 *
 * PHRASE-SAFETY (L3): a THROWAWAY mnemonic only, cached to the gitignored
 * `.e2e-secret` and reused across runs (staging /v1/register has an IP-global
 * ~19-min 429 window — NEVER re-register). The mnemonic + private key are
 * NEVER printed or logged. Only public data is emitted.
 *
 * Run: cd app && node e2e/pin-onchain.mjs
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

async function subgraphQuery(authKeyHex, wallet, query, variables) {
  const { text } = await relayFetch(
    "/v1/subgraph",
    { method: "POST", body: JSON.stringify({ query, variables }) },
    authKeyHex,
    wallet,
  );
  const json = JSON.parse(text);
  if (json.errors?.length) throw new Error(`subgraph: ${json.errors.map((e) => e.message).join("; ")}`);
  return json.data;
}

async function factById(authKeyHex, wallet, id) {
  const data = await subgraphQuery(
    authKeyHex,
    wallet,
    `query F($id: ID!) { fact(id: $id) { id isActive encryptedBlob encryptedEmbedding blindIndexEntries { hash } } }`,
    { id },
  );
  return data.fact;
}

async function activeFacts(authKeyHex, wallet, ownerLower) {
  const data = await subgraphQuery(
    authKeyHex,
    wallet,
    `query A($owner: Bytes!) { facts(where: { owner: $owner, isActive: true }, first: 1000) { id } }`,
    { owner: ownerLower },
  );
  return data.facts ?? [];
}

/** Poll until `fn` returns truthy (subgraph indexing) or ~2 min elapse. */
async function waitFor(fn) {
  for (let i = 0; i < 30; i++) {
    await sleep(4000);
    const v = await fn();
    if (v) return v;
  }
  return null;
}

/** Build → sponsor → sign → submit → confirm one UserOp (execute or batch). */
async function submitUserOp(calldataBytes, { authKeyHex, wallet, eoa, needsInitCode }) {
  const entryPoint = core.getEntryPointAddress();
  const callData = `0x${Buffer.from(calldataBytes).toString("hex")}`;

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

  // Batches: estimate gas BEFORE sponsorship (mirror src/lib/userop.ts step 6).
  try {
    const est = await bundlerRpc(authKeyHex, wallet, "eth_estimateUserOperationGas", [op, entryPoint]);
    if (est.callGasLimit) op.callGasLimit = est.callGasLimit;
    if (est.verificationGasLimit) op.verificationGasLimit = est.verificationGasLimit;
    if (est.preVerificationGas) op.preVerificationGas = est.preVerificationGas;
  } catch {
    /* fall back to paymaster-provided limits */
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

// --- claim rebuild (mirrors src/lib/claim.ts:rebuildClaimJson) ---
function rebuildClaimJson(rawClaimJson, { newId, supersededBy, pinStatus }) {
  const source = JSON.parse(rawClaimJson);
  const input = { ...source };
  input.id = newId;
  input.superseded_by = supersededBy;
  input.pin_status = pinStatus;
  input.schema_version = "1.0";
  if (typeof input.created_at !== "string" || input.created_at === "")
    input.created_at = new Date().toISOString();
  if (input.scope === "unspecified") delete input.scope;
  if (input.volatility === "updatable") delete input.volatility;
  if (Array.isArray(input.entities) && input.entities.length === 0) delete input.entities;
  if (!input.reasoning) delete input.reasoning;
  if (!input.expires_at) delete input.expires_at;
  const parsed = JSON.parse(core.validateMemoryClaimV1(JSON.stringify(input)));
  parsed.schema_version = "1.0";
  if (input.metadata !== undefined && input.metadata !== null) parsed.metadata = input.metadata;
  for (const [k, v] of Object.entries(input)) if (!(k in parsed) && v !== undefined) parsed[k] = v;
  return JSON.stringify(parsed);
}

const decryptHexBlob = (hex, keyHex) => {
  const h = hex.startsWith("0x") ? hex.slice(2) : hex;
  return core.decrypt(Buffer.from(h, "hex").toString("base64"), keyHex);
};

/** SPA-shaped pin/unpin: idempotency guard + 2-call supersession batch. */
async function setPinStatus(oldFactId, target, ctx) {
  const { authKeyHex, wallet, keys, dataEdge } = ctx;
  const oldFact = await factById(authKeyHex, wallet, oldFactId);
  if (!oldFact) throw new Error(`fact ${oldFactId} not found`);
  const plaintext = decryptHexBlob(oldFact.encryptedBlob, keys.encryption_key);
  const current = JSON.parse(plaintext).pin_status === "pinned" ? "pinned" : "unpinned";
  if (current === target) return { idempotent: true };

  const newId = randomUUID();
  const canonicalJson = rebuildClaimJson(plaintext, {
    newId,
    supersededBy: oldFactId,
    pinStatus: target,
  });
  const blobHex = Buffer.from(core.encrypt(canonicalJson, keys.encryption_key), "base64").toString("hex");

  const tombstone = core.encodeTombstoneProtobuf(oldFactId, wallet, 4);
  const newFactPayload = core.encodeFactProtobuf(
    JSON.stringify({
      id: newId,
      timestamp: new Date().toISOString(),
      owner: wallet,
      encrypted_blob_hex: blobHex,
      blind_indices: (oldFact.blindIndexEntries ?? []).map((e) => e.hash), // copied forward
      decay_score: 1.0,
      source: target === "pinned" ? "spa_pin" : "spa_unpin",
      content_fp: "",
      agent_id: "ts-spa-vault",
      encrypted_embedding: oldFact.encryptedEmbedding ?? null, // copied forward
      version: 4,
    }),
  );
  const calldata = core.encodeBatchCallTo(
    JSON.stringify([Buffer.from(tombstone).toString("hex"), Buffer.from(newFactPayload).toString("hex")]),
    dataEdge,
  );
  const res = await submitUserOp(calldata, { ...ctx, needsInitCode: false });
  return { idempotent: false, newId, ...res };
}

// --------------------------------------------------------------------------

const evidence = {};
try {
  const { mnemonic, fresh } = loadOrCreateMnemonic();
  const keys = core.deriveKeysFromMnemonic(mnemonic);
  const eoa = core.deriveEoa(mnemonic); // private_key NEVER logged
  const authKeyHex = keys.auth_key;
  const authKeyHash = createHash("sha256").update(Buffer.from(authKeyHex, "hex")).digest("hex");

  const saRes = await relayFetch(`/v1/smart-account?eoa=${eoa.address}&chain=${CHAIN_ID}`);
  const wallet = JSON.parse(saRes.text).smart_account.toLowerCase();
  out(`smart account: ${wallet}`);

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

  const ctx = { authKeyHex, wallet, eoa, keys, dataEdge };

  // 1. WRITE a Crystal-shaped fact (metadata is the preservation proof) with a
  //    real encrypted_embedding value so copy-forward is assertable.
  const factId = randomUUID();
  const sessionId = randomUUID();
  const metadata = {
    subtype: "session_crystal",
    session_id: sessionId,
    key_outcomes: ["a2 pin e2e outcome"],
    open_threads: ["a2 pin e2e thread"],
    topics_discussed: ["supersession"],
  };
  const claim = {
    id: factId,
    text: `A2 pin E2E marker ${factId}`,
    type: "summary",
    source: "derived",
    created_at: new Date().toISOString(),
    schema_version: "1.0",
    importance: 9,
    metadata,
  };
  const markerEmbedding = `a2pinE2Eembedding${factId.replaceAll("-", "")}`;
  const encB64 = core.encrypt(JSON.stringify(claim), keys.encryption_key);
  const writeCalldata = core.encodeSingleCallTo(
    core.encodeFactProtobuf(
      JSON.stringify({
        id: factId,
        timestamp: claim.created_at,
        owner: wallet,
        encrypted_blob_hex: Buffer.from(encB64, "base64").toString("hex"),
        blind_indices: core.generateBlindIndices(claim.text),
        decay_score: 1.0,
        source: "derived",
        content_fp: core.generateContentFingerprint(claim.text, keys.dedup_key),
        agent_id: "a2-pin-e2e",
        encrypted_embedding: markerEmbedding,
        version: 4,
      }),
    ),
    dataEdge,
  );
  out(`writing throwaway Crystal fact ${factId} …`);
  const write = await submitUserOp(writeCalldata, { ...ctx, needsInitCode: true });
  check("write UserOp mined", !!write.receipt);
  out(`write userOpHash: ${write.userOpHash}`);

  const indexedOld = await waitFor(async () => {
    const f = await factById(authKeyHex, wallet, factId);
    return f && f.isActive ? f : null;
  });
  check("fact indexed as isActive:true before pin", !!indexedOld);
  const oldEmbedding = indexedOld?.encryptedEmbedding ?? null;
  check("old fact carries the marker encryptedEmbedding", oldEmbedding === markerEmbedding);
  const activeBefore = (await activeFacts(authKeyHex, wallet, wallet)).length;

  // 2. PIN — the 2-call supersession under test.
  out(`pinning fact ${factId} …`);
  const pin = await setPinStatus(factId, "pinned", ctx);
  check("pin UserOp mined", !pin.idempotent && !!pin.receipt);
  out(`pin userOpHash: ${pin.userOpHash}  new fact: ${pin.newId}`);

  // (1) receipt logs at the STAGING DataEdge only.
  const pinLogs = pin.receipt?.logs ?? pin.receipt?.receipt?.logs ?? [];
  check("pin receipt emitted a log at the STAGING DataEdge (0xE7a4…)",
    pinLogs.some((l) => (l.address || "").toLowerCase() === STAGING_DATA_EDGE.toLowerCase()));
  check("pin receipt did NOT touch the prod DataEdge (0xC445…)",
    !pinLogs.some((l) => (l.address || "").toLowerCase() === PROD_DATA_EDGE.toLowerCase()));

  // (2) old fact flips isActive:false.
  const oldAfterPin = await waitFor(async () => {
    const f = await factById(authKeyHex, wallet, factId);
    return f && f.isActive === false ? f : null;
  });
  check("subgraph flipped the OLD fact to isActive:false", !!oldAfterPin);

  // (3) new fact exists; decrypted claim: pin_status pinned, superseded_by, metadata intact.
  const newFact = await waitFor(() => factById(authKeyHex, wallet, pin.newId));
  check("superseding fact indexed", !!newFact && newFact.isActive === true);
  let pinnedClaim = null;
  if (newFact) {
    pinnedClaim = JSON.parse(decryptHexBlob(newFact.encryptedBlob, keys.encryption_key));
    check("new claim pin_status === 'pinned'", pinnedClaim.pin_status === "pinned");
    check("new claim superseded_by === old fact id", pinnedClaim.superseded_by === factId);
    check("METADATA INTACT through supersession (Crystal fields verbatim)",
      JSON.stringify(pinnedClaim.metadata) === JSON.stringify(metadata));
    check("text/type/source/created_at carried forward",
      pinnedClaim.text === claim.text && pinnedClaim.type === "summary" &&
      pinnedClaim.source === "derived" && pinnedClaim.created_at === claim.created_at);
    // (4) embedding copied forward byte-identically.
    check("new fact encryptedEmbedding EQUALS the old one (copied forward)",
      newFact.encryptedEmbedding === oldEmbedding);
  }

  // (6) idempotent pin: pin again while pinned → NO UserOp, no new fact.
  const activeAfterPin = (await activeFacts(authKeyHex, wallet, wallet)).length;
  const again = await setPinStatus(pin.newId, "pinned", ctx);
  check("idempotent pin short-circuits (no UserOp)", again.idempotent === true);
  const activeAfterIdem = (await activeFacts(authKeyHex, wallet, wallet)).length;
  check("idempotent pin produced NO new on-chain fact", activeAfterIdem === activeAfterPin);

  // (5) UNPIN — same supersession shape back to 'unpinned'.
  out(`unpinning fact ${pin.newId} …`);
  const unpin = await setPinStatus(pin.newId, "unpinned", ctx);
  check("unpin UserOp mined", !unpin.idempotent && !!unpin.receipt);
  out(`unpin userOpHash: ${unpin.userOpHash}  new fact: ${unpin.newId}`);
  const pinnedAfterUnpin = await waitFor(async () => {
    const f = await factById(authKeyHex, wallet, pin.newId);
    return f && f.isActive === false ? f : null;
  });
  check("pinned fact flipped isActive:false after unpin", !!pinnedAfterUnpin);
  const unpinnedFact = await waitFor(() => factById(authKeyHex, wallet, unpin.newId));
  check("unpinned superseding fact indexed", !!unpinnedFact && unpinnedFact.isActive === true);
  let unpinnedClaim = null;
  if (unpinnedFact) {
    unpinnedClaim = JSON.parse(decryptHexBlob(unpinnedFact.encryptedBlob, keys.encryption_key));
    check("unpinned claim pin_status === 'unpinned'", unpinnedClaim.pin_status === "unpinned");
    check("unpinned claim superseded_by === pinned fact id", unpinnedClaim.superseded_by === pin.newId);
    check("metadata STILL intact after second supersession",
      JSON.stringify(unpinnedClaim.metadata) === JSON.stringify(metadata));
    check("embedding still copied forward on unpin",
      unpinnedFact.encryptedEmbedding === oldEmbedding);
  }

  // Cleanup: tombstone the surviving test fact(s).
  out("cleanup: tombstoning surviving test facts …");
  const survivors = [unpin.newId].filter(Boolean);
  if (survivors.length > 0) {
    const payloads = survivors.map((id) =>
      Buffer.from(core.encodeTombstoneProtobuf(id, wallet, 4)).toString("hex"),
    );
    const cleanupCalldata =
      payloads.length === 1
        ? core.encodeSingleCallTo(Buffer.from(payloads[0], "hex"), dataEdge)
        : core.encodeBatchCallTo(JSON.stringify(payloads), dataEdge);
    const cleanup = await submitUserOp(cleanupCalldata, { ...ctx, needsInitCode: false });
    check("cleanup tombstone mined", !!cleanup.receipt);
  }

  Object.assign(evidence, {
    smartAccount: wallet,
    dataEdge,
    chainId: billing.chain_id,
    environment: billing.environment,
    oldFactId: factId,
    pinnedFactId: pin.newId,
    unpinnedFactId: unpin.newId,
    writeUserOpHash: write.userOpHash,
    pinUserOpHash: pin.userOpHash,
    unpinUserOpHash: unpin.userOpHash,
    idempotentPinSkipped: again.idempotent === true,
    embeddingCopiedForward: newFact?.encryptedEmbedding === oldEmbedding,
    metadataIntact: JSON.stringify(pinnedClaim?.metadata) === JSON.stringify(metadata),
    activeBefore,
  });
  out("--- REDACTED EVIDENCE (no secrets) ---");
  out(JSON.stringify(evidence, null, 2));
} catch (e) {
  out(`EXCEPTION: ${e.message}`);
  failed = true;
}

out(failed ? "RESULT: FAIL" : "RESULT: PASS");
process.exit(failed ? 1 : 0);
