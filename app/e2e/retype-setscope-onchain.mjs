/**
 * E2E (A.2 Phase 3): STAGING retype/set_scope round-trip — 2-call executeBatch
 * supersession on Gnosis mainnet via the staging relay.
 *
 * Exercises the exact SPA write wire (same engine as pin-onchain.mjs, with
 * `type` / `scope` mutated instead of `pin_status`): decrypt old blob →
 * rebuild the claim (full carry-forward + metadata verbatim, core-validated,
 * pin_status carried forward per #117) → re-encrypt → [tombstone(old,v4),
 * newClaim(v4)] in ONE UserOp via encodeBatchCallTo → relay /v1/bundler →
 * subgraph. Embedding + blind indices copied forward and asserted byte-equal.
 *
 * Flow: write Crystal-shaped fact → retype claim→preference → set_scope
 * →health → set_scope →"unspecified" (field OMITTED) → pin → retype while
 * pinned (#117 pin-survival) → idempotent retype (no UserOp) → cleanup.
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
 * Run: cd app && node e2e/retype-setscope-onchain.mjs
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

// --- claim rebuild (mirrors src/lib/claim.ts:rebuildClaimJson, Phase 3) ---
// pinStatus ABSENT → carried forward from the source verbatim (#117).
function rebuildClaimJson(rawClaimJson, { newId, supersededBy, pinStatus, newType, newScope }) {
  const source = JSON.parse(rawClaimJson);
  const input = { ...source };
  input.id = newId;
  input.superseded_by = supersededBy;
  if (pinStatus !== undefined) input.pin_status = pinStatus;
  if (newType !== undefined) input.type = newType;
  if (newScope !== undefined) input.scope = newScope;
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

/**
 * SPA-shaped curation supersession: idempotency guard (retype-to-same-type /
 * set-same-scope / pin-when-pinned → NO UserOp, mirroring api.ts) + the
 * 2-call batch. `mutation` = { pinStatus? | newType? | newScope? }.
 */
async function supersede(oldFactId, mutation, sourceTag, ctx) {
  const { authKeyHex, wallet, keys, dataEdge } = ctx;
  const oldFact = await factById(authKeyHex, wallet, oldFactId);
  if (!oldFact) throw new Error(`fact ${oldFactId} not found`);
  const plaintext = decryptHexBlob(oldFact.encryptedBlob, keys.encryption_key);
  const current = JSON.parse(plaintext);

  // Idempotency guards — mirror api.ts (setPinStatus / retypeFact / setFactScope).
  if (mutation.pinStatus !== undefined) {
    const cur = current.pin_status === "pinned" ? "pinned" : "unpinned";
    if (cur === mutation.pinStatus) return { idempotent: true };
  }
  if (mutation.newType !== undefined && current.type === mutation.newType)
    return { idempotent: true };
  if (mutation.newScope !== undefined && (current.scope ?? "unspecified") === mutation.newScope)
    return { idempotent: true };

  const newId = randomUUID();
  const canonicalJson = rebuildClaimJson(plaintext, {
    newId,
    supersededBy: oldFactId,
    ...mutation,
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
      source: sourceTag,
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

/** Assert the standard supersession shape after a mutation UserOp. */
async function assertSupersession(label, oldId, sup, ctx, { metadata, oldEmbedding }) {
  const { authKeyHex, wallet, keys } = ctx;
  check(`${label}: UserOp mined`, !sup.idempotent && !!sup.receipt);
  const logs = sup.receipt?.logs ?? sup.receipt?.receipt?.logs ?? [];
  check(`${label}: receipt log at STAGING DataEdge (0xE7a4…)`,
    logs.some((l) => (l.address || "").toLowerCase() === STAGING_DATA_EDGE.toLowerCase()));
  check(`${label}: receipt did NOT touch prod DataEdge (0xC445…)`,
    !logs.some((l) => (l.address || "").toLowerCase() === PROD_DATA_EDGE.toLowerCase()));

  const oldAfter = await waitFor(async () => {
    const f = await factById(authKeyHex, wallet, oldId);
    return f && f.isActive === false ? f : null;
  });
  check(`${label}: OLD fact flipped isActive:false`, !!oldAfter);

  const newFact = await waitFor(() => factById(authKeyHex, wallet, sup.newId));
  check(`${label}: superseding fact indexed + active`, !!newFact && newFact.isActive === true);
  let claim = null;
  if (newFact) {
    claim = JSON.parse(decryptHexBlob(newFact.encryptedBlob, keys.encryption_key));
    check(`${label}: superseded_by === old fact id`, claim.superseded_by === oldId);
    check(`${label}: METADATA INTACT (Crystal fields verbatim)`,
      JSON.stringify(claim.metadata) === JSON.stringify(metadata));
    check(`${label}: encryptedEmbedding byte-equal (copied forward)`,
      newFact.encryptedEmbedding === oldEmbedding);
  }
  return claim;
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

  // 1. WRITE a Crystal-shaped `claim` fact (metadata is the preservation
  //    proof) with a marker embedding so copy-forward is byte-assertable.
  const factId = randomUUID();
  const sessionId = randomUUID();
  const metadata = {
    subtype: "session_crystal",
    session_id: sessionId,
    key_outcomes: ["a2 phase3 e2e outcome"],
    open_threads: ["a2 phase3 e2e thread"],
    topics_discussed: ["retype", "set_scope"],
  };
  const claim = {
    id: factId,
    text: `A2 retype/set_scope E2E marker ${factId}`,
    type: "claim",
    source: "user",
    created_at: new Date().toISOString(),
    schema_version: "1.0",
    importance: 7,
    metadata,
  };
  const markerEmbedding = `a2p3E2Eembedding${factId.replaceAll("-", "")}`;
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
        source: "user",
        content_fp: core.generateContentFingerprint(claim.text, keys.dedup_key),
        agent_id: "a2-p3-e2e",
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
  check("fact indexed as isActive:true before retype", !!indexedOld);
  const oldEmbedding = indexedOld?.encryptedEmbedding ?? null;
  check("old fact carries the marker encryptedEmbedding", oldEmbedding === markerEmbedding);
  const shape = { metadata, oldEmbedding };

  // 2. RETYPE claim → preference.
  out(`retyping ${factId} claim → preference …`);
  const retype = await supersede(factId, { newType: "preference" }, "spa_retype", ctx);
  out(`retype userOpHash: ${retype.userOpHash}  new fact: ${retype.newId}`);
  const retypedClaim = await assertSupersession("retype", factId, retype, ctx, shape);
  check("retype: new claim type === 'preference'", retypedClaim?.type === "preference");
  check("retype: created_at preserved (original creation time)",
    retypedClaim?.created_at === claim.created_at);
  check("retype: pin_status not invented on an unpinned claim",
    retypedClaim ? !("pin_status" in retypedClaim) : false);

  // 3. SET_SCOPE → health.
  out(`setting scope of ${retype.newId} → health …`);
  const scopeHealth = await supersede(retype.newId, { newScope: "health" }, "spa_set_scope", ctx);
  out(`set_scope userOpHash: ${scopeHealth.userOpHash}  new fact: ${scopeHealth.newId}`);
  const healthClaim = await assertSupersession("set_scope(health)", retype.newId, scopeHealth, ctx, shape);
  check("set_scope(health): scope === 'health'", healthClaim?.scope === "health");
  check("set_scope(health): type still 'preference' (untouched)", healthClaim?.type === "preference");

  // 4. SET_SCOPE → "unspecified" — the field must be OMITTED on the wire.
  out(`setting scope of ${scopeHealth.newId} → unspecified …`);
  const scopeNone = await supersede(scopeHealth.newId, { newScope: "unspecified" }, "spa_set_scope", ctx);
  out(`set_scope userOpHash: ${scopeNone.userOpHash}  new fact: ${scopeNone.newId}`);
  const noneClaim = await assertSupersession("set_scope(unspecified)", scopeHealth.newId, scopeNone, ctx, shape);
  check("set_scope(unspecified): 'scope' field OMITTED from the decrypted claim",
    noneClaim ? !("scope" in noneClaim) : false);

  // 5. PIN, then RETYPE while pinned — the #117 pin-survival contract.
  out(`pinning ${scopeNone.newId} …`);
  const pin = await supersede(scopeNone.newId, { pinStatus: "pinned" }, "spa_pin", ctx);
  out(`pin userOpHash: ${pin.userOpHash}  new fact: ${pin.newId}`);
  const pinnedClaim = await assertSupersession("pin", scopeNone.newId, pin, ctx, shape);
  check("pin: pin_status === 'pinned'", pinnedClaim?.pin_status === "pinned");

  out(`retyping PINNED fact ${pin.newId} preference → commitment …`);
  const retypePinned = await supersede(pin.newId, { newType: "commitment" }, "spa_retype", ctx);
  out(`retype-pinned userOpHash: ${retypePinned.userOpHash}  new fact: ${retypePinned.newId}`);
  const survivedClaim = await assertSupersession("retype-while-pinned", pin.newId, retypePinned, ctx, shape);
  check("#117 PIN SURVIVED the retype (pin_status === 'pinned')",
    survivedClaim?.pin_status === "pinned");
  check("retype-while-pinned: type === 'commitment'", survivedClaim?.type === "commitment");

  // 6. IDEMPOTENT retype: same type → NO UserOp, no new on-chain fact.
  const activeBefore = (await activeFacts(authKeyHex, wallet, wallet)).length;
  const again = await supersede(retypePinned.newId, { newType: "commitment" }, "spa_retype", ctx);
  check("idempotent retype short-circuits (no UserOp)", again.idempotent === true);
  const activeAfter = (await activeFacts(authKeyHex, wallet, wallet)).length;
  check("idempotent retype produced NO new on-chain fact", activeAfter === activeBefore);

  // Cleanup: tombstone the surviving test fact.
  out("cleanup: tombstoning surviving test facts …");
  const survivors = [retypePinned.newId].filter(Boolean);
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
    retypedFactId: retype.newId,
    scopeHealthFactId: scopeHealth.newId,
    scopeUnspecifiedFactId: scopeNone.newId,
    pinnedFactId: pin.newId,
    retypedWhilePinnedFactId: retypePinned.newId,
    writeUserOpHash: write.userOpHash,
    retypeUserOpHash: retype.userOpHash,
    setScopeHealthUserOpHash: scopeHealth.userOpHash,
    setScopeUnspecifiedUserOpHash: scopeNone.userOpHash,
    pinUserOpHash: pin.userOpHash,
    retypePinnedUserOpHash: retypePinned.userOpHash,
    idempotentRetypeSkipped: again.idempotent === true,
    pinSurvivedRetype: survivedClaim?.pin_status === "pinned",
    scopeOmittedOnUnspecified: noneClaim ? !("scope" in noneClaim) : false,
    metadataIntactThroughout:
      JSON.stringify(survivedClaim?.metadata) === JSON.stringify(metadata),
  });
  out("--- REDACTED EVIDENCE (no secrets) ---");
  out(JSON.stringify(evidence, null, 2));
} catch (e) {
  out(`EXCEPTION: ${e.message}`);
  failed = true;
}

out(failed ? "RESULT: FAIL" : "RESULT: PASS");
process.exit(failed ? 1 : 0);
