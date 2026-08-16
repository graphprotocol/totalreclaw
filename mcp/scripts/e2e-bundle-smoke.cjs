#!/usr/bin/env node
/**
 * Staging E2E smoke test for Option E Phase 2 / #581, P2-13 (MCP
 * derived-bundle-v1 adapter).
 *
 * NOT part of `npm test` — a standalone script exercising the real
 * `dist/` code paths against the LIVE staging relay
 * (https://api-staging.totalreclaw.xyz). Requires `npm run build` first.
 *
 * Flow (mirrors index.ts's initSubgraphState / initSubgraphStateFromBundle
 * / handleRememberSubgraph / handleRecallSubgraph without needing to spin
 * up the stdio MCP transport):
 *
 *   1. Generate a FRESH throwaway test mnemonic (never reused — avoids
 *      collisions with other concurrent test runs against staging).
 *   2. Register that vault with staging via the LEGACY mnemonic path
 *      (registerWithServer) — bundle mode itself must NEVER call
 *      /v1/register (derived-bundle-v1.md §4.5's normative rule), so the
 *      vault has to already exist before a bundle can write to it.
 *   3. Derive a derived-bundle-v1 bundle from the SAME mnemonic
 *      (deriveBundleFromMnemonic) and parse+validate it (parseBundleV1) —
 *      exactly the two steps a real credentials.json `version: 2` file
 *      would go through when read back by `subgraph/credentials.ts`.
 *   4. Resolve chain + DataEdge via `fetchChainConfig`, using
 *      `bundle.account.chain_id` as the fallback — the exact same call
 *      `initSubgraphStateFromBundle` makes. ASSERTS the resolved DataEdge
 *      is the STAGING address, never the core prod default — this is the
 *      "Lesson 1" regression check, now proven against LIVE staging
 *      infrastructure rather than a mocked fetch.
 *   5. Build + encrypt a fact using the bundle's vault keys, submit it
 *      on-chain via `submitFactBatchOnChain` with `ownerPrivateKeyHex`
 *      (bundle-mode signing — never a mnemonic in this step).
 *   6. Poll the staging subgraph and recall the fact back, decrypt with
 *      the same bundle vault keys, and assert the plaintext round-trips.
 *
 * Never touches production. Never logs the mnemonic, the bundle JSON, or
 * any key material — only redacted summaries / addresses / tx hashes.
 */

'use strict';

const path = require('path');
const distDir = path.join(__dirname, '..', 'dist');

const bundleMod = require(path.join(distDir, 'subgraph/bundle.js'));
const chainConfigMod = require(path.join(distDir, 'subgraph/chain-config.js'));
const cryptoMod = require(path.join(distDir, 'subgraph/crypto.js'));
const storeMod = require(path.join(distDir, 'subgraph/store.js'));
const lshMod = require(path.join(distDir, 'subgraph/lsh.js'));
const embeddingMod = require(path.join(distDir, 'subgraph/embedding.js'));
const searchMod = require(path.join(distDir, 'subgraph/search.js'));
const setupMod = require(path.join(distDir, 'cli/setup.js'));
const claimsHelperMod = require(path.join(distDir, 'claims-helper.js'));

const { generateMnemonic } = require('@scure/bip39');
const { wordlist } = require('@scure/bip39/wordlists/english.js');
const { mnemonicToAccount } = require('viem/accounts');
const { createPublicClient, http } = require('viem');
const { baseSepolia } = require('viem/chains');
const { toSimpleSmartAccount } = require('permissionless/accounts');
const { entryPoint07Address } = require('viem/account-abstraction');
const crypto = require('node:crypto');

const STAGING_URL = 'https://api-staging.totalreclaw.xyz';
const STAGING_DATA_EDGE = '0xE7a4D2677B686e13775Ba9092631089e35F0BB91';
const PROD_DATA_EDGE = '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca';

function log(msg) {
  console.log(`[e2e-bundle-smoke] ${msg}`);
}

function assert(cond, msg) {
  if (!cond) {
    throw new Error(`ASSERTION FAILED: ${msg}`);
  }
  log(`OK: ${msg}`);
}

async function deriveSmartAccountAddress(mnemonic) {
  const entryPointAddr = entryPoint07Address;
  const ownerAccount = mnemonicToAccount(mnemonic);
  const publicClient = createPublicClient({ chain: baseSepolia, transport: http() });
  const smartAccount = await toSimpleSmartAccount({
    client: publicClient,
    owner: ownerAccount,
    entryPoint: { address: entryPointAddr, version: '0.7' },
  });
  return smartAccount.address.toLowerCase();
}

async function main() {
  log(`Starting against ${STAGING_URL} — never production.`);

  // 1. Fresh throwaway test mnemonic.
  const mnemonic = generateMnemonic(wordlist, 128);
  const { authKey, encryptionKey, dedupKey, salt } = cryptoMod.deriveKeys(mnemonic);
  const lshSeed = cryptoMod.deriveLshSeed(mnemonic, salt);
  const authKeyHex = Buffer.from(authKey).toString('hex');
  const authKeyHash = cryptoMod.computeAuthKeyHash(authKey);
  const smartAccountAddress = await deriveSmartAccountAddress(mnemonic);
  log(`Fresh test vault: smart_account=${smartAccountAddress}`);

  // 2. Register via the LEGACY mnemonic path. Bundle mode never does this.
  const saltHex = Buffer.from(salt).toString('hex');
  const userId = await setupMod.registerWithServer(STAGING_URL, authKeyHash, saltHex);
  assert(!!userId, `registered with staging relay (user_id=${userId.slice(0, 8)}…)`);

  // 3. Derive + parse the bundle from the SAME mnemonic. This is the
  //    exact WASM path `subgraph/credentials.ts` uses when it reads a
  //    real `credentials.json` `version: 2` file.
  const bundleJson = bundleMod.deriveBundleFromMnemonic(
    mnemonic,
    100,
    'local-migration',
    smartAccountAddress,
  );
  const bundle = bundleMod.parseBundleV1(bundleJson);
  assert(bundle.account.smart_account.toLowerCase() === smartAccountAddress, 'bundle.account.smart_account matches derived SA');
  assert(bundle.signing.kind === 'owner-eoa', 'bundle.signing.kind === owner-eoa (Phase 2 shipping config)');
  log(`Bundle parsed OK — ${JSON.stringify(bundleMod.redactedBundleSummary(bundle))}`);

  // 4. THE LOAD-BEARING CHECK (Lesson 1): resolve chain + DataEdge via the
  //    real fetchChainConfig against LIVE staging billing. Must be the
  //    STAGING DataEdge, never the core prod default.
  const resolved = await chainConfigMod.fetchChainConfig({
    serverUrl: STAGING_URL,
    smartAccountAddress,
    authKeyHex,
    fallbackChainId: bundle.account.chain_id,
    onResolved: (billing, r) => {
      log(`billing resolved: tier=${billing.tier}, chain=${r.chainId}, dataEdge=${r.dataEdgeAddress}`);
    },
  });
  assert(resolved.chainId === 100, `resolved chainId is 100 (Gnosis) — got ${resolved.chainId}`);
  assert(
    resolved.dataEdgeAddress === STAGING_DATA_EDGE,
    `resolved dataEdgeAddress is the STAGING DataEdge (${STAGING_DATA_EDGE}), not prod — got ${resolved.dataEdgeAddress}`,
  );
  assert(resolved.dataEdgeAddress !== PROD_DATA_EDGE, 'resolved dataEdgeAddress is NOT the prod default');

  // 5. Build + encrypt a fact, submit on-chain via bundle-mode signing
  //    (ownerPrivateKeyHex, never a mnemonic).
  const vaultBuffers = bundleMod.bundleVaultToBuffers(bundle);
  const dims = embeddingMod.getEmbeddingDims();
  const lshHasher = new lshMod.LSHHasher(vaultBuffers.lshSeed, dims);

  const factText = `P2-13 MCP bundle-mode E2E smoke test — ${new Date().toISOString()}`;
  const wordIndices = cryptoMod.generateBlindIndices(factText);
  const embedding = await embeddingMod.generateEmbedding(factText);
  const lshIndices = lshHasher.hash(embedding);
  const allIndices = [...wordIndices, ...lshIndices];

  const blobPlaintext = claimsHelperMod.buildV1ClaimBlob({
    text: factText,
    type: 'claim',
    source: 'user',
    scope: 'unspecified',
    importance: 5,
  });
  const encryptedBlob = cryptoMod.encrypt(blobPlaintext, vaultBuffers.encryptionKey);
  const contentFp = cryptoMod.generateContentFingerprint(factText, vaultBuffers.dedupKey);

  const factId = crypto.randomUUID();
  const factPayload = {
    id: factId,
    timestamp: new Date().toISOString(),
    owner: smartAccountAddress,
    encryptedBlob: Buffer.from(encryptedBlob, 'base64').toString('hex'),
    blindIndices: allIndices,
    decayScore: 0.5,
    source: 'mcp_bundle_e2e_smoke',
    contentFp,
    agentId: 'mcp-server-e2e-smoke',
  };

  const storeConfig = storeMod.getSubgraphConfig({
    relayUrl: STAGING_URL,
    ownerPrivateKeyHex: bundle.signing.private_key,
    authKeyHex,
    walletAddress: smartAccountAddress,
    chainId: resolved.chainId,
    dataEdgeAddress: resolved.dataEdgeAddress,
  });
  // Proves resolveOwnerAccount actually resolves a signer from the bundle
  // private key, not a mnemonic, before we spend a real UserOp on it.
  const ownerAccount = storeMod.resolveOwnerAccount(storeConfig);
  assert(ownerAccount.address.toLowerCase() === bundle.signing.address.toLowerCase(), 'resolveOwnerAccount(bundle-mode config) matches bundle.signing.address');

  log('Submitting fact on-chain (bundle-mode signing)…');
  const protobuf = storeMod.encodeFactProtobuf(factPayload);
  const batchResult = await storeMod.submitFactBatchOnChain([protobuf], storeConfig);
  assert(batchResult.success, `on-chain submission succeeded (tx=${batchResult.txHash})`);
  log(`tx_hash=${batchResult.txHash}`);

  // 6. Poll the staging subgraph and recall the fact back.
  log('Polling staging subgraph for the fact…');
  let found = null;
  for (let attempt = 0; attempt < 12 && !found; attempt++) {
    await new Promise((r) => setTimeout(r, 5000));
    const candidates = await searchMod.searchSubgraph(
      smartAccountAddress,
      allIndices.slice(0, 5),
      50,
      STAGING_URL,
      authKeyHex,
    );
    found = candidates.find((c) => c.id === factId) || null;
    log(`attempt ${attempt + 1}: ${candidates.length} candidates, found=${!!found}`);
  }
  assert(!!found, 'fact indexed by the staging subgraph within polling window');

  const blobHex = found.encryptedBlob.startsWith('0x') ? found.encryptedBlob.slice(2) : found.encryptedBlob;
  const blobBase64 = Buffer.from(blobHex, 'hex').toString('base64');
  const decryptedBlob = cryptoMod.decrypt(blobBase64, vaultBuffers.encryptionKey);
  const doc = claimsHelperMod.readBlobUnified(decryptedBlob);
  assert(doc.text === factText, `decrypted fact text round-trips byte-identically via bundle-mode vault keys`);

  log('');
  log('ALL CHECKS PASSED.');
  log(`Vault: ${smartAccountAddress}`);
  log(`Tx: ${batchResult.txHash}`);
  log(`DataEdge used: ${resolved.dataEdgeAddress} (staging, verified != prod)`);
}

main().catch((err) => {
  console.error('[e2e-bundle-smoke] FAILED:', err && err.stack ? err.stack : err);
  process.exit(1);
});
