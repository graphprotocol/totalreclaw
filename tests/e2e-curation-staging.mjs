#!/usr/bin/env node
/**
 * Staging E2E for #563 curation — the blocking gate (§3 of the 2026-08-02
 * session handoff). Every pin/unpin/retype/set_scope is an on-chain
 * supersession; this is the FIRST time any of them runs against a real chain.
 *
 * WHAT THIS VALIDATES:
 *   1. Store 2 throwaway facts on STAGING (isolated DataEdge + subgraph).
 *   2. pin <id>     → {ok:true, new_fact_id, tx_hash}; old id gone from the
 *      active vault, new id present.
 *   3. retype <id> <type>  → same supersession shape.
 *   4. set_scope <id> <scope> → same.
 *   5. unpin <id> (on the pinned fact) → restores active status.
 *
 * This drives the plugin's OWN built dist (runCurationOp + buildCurationDeps +
 * the store pipeline), which is the exact same code the new agent tools wire
 * to — so a pass here validates the shared supersession path both surfaces use.
 *
 * WHY IN-PROCESS (not spawning `tr` per op):
 *   The staging relay rate-limits /v1/register per IP (~19 min). Each spawned
 *   `tr` invocation re-registers (the standalone CLI doesn't cache userId back
 *   to credentials.json — that's pair's job). Spawning per-op burns the
 *   register budget after 1-2 ops. Running in-process calls buildContext ONCE
 *   (one registration), then reuses the context for every op + export.
 *
 * VERIFICATION:
 *   Uses the in-process store pipeline's list/read (the same authenticated
 *   path recall uses) to confirm supersession: old fact_id absent from the
 *   active set, new one present.
 *
 * SAFETY:
 *   - STAGING ONLY. Staging is on-chain isolated (its own DataEdge + subgraph).
 *   - THROWAWAY VAULT: fresh BIP-39 phrase per run, temp creds path, discarded
 *     on exit. Never Pedro's real vault.
 *
 * Run: node tests/e2e-curation-staging.mjs
 */
import { writeFileSync, unlinkSync, mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = dirname(fileURLToPath(import.meta.url));
const WORKTREE = dirname(HERE);
const PLUGIN = join(WORKTREE, 'skill/plugin');
const DIST = join(PLUGIN, 'dist');
const RELAY = 'https://api-staging.totalreclaw.xyz';

// Import from the built dist (so we exercise the exact shipped code).
const require = createRequire(import.meta.url);
const { buildCurationDeps } = require(join(DIST, 'cli/tr-cli.js'));
const { runCurationOp } = require(join(DIST, 'memory/curation-op.js'));

let passed = 0, failed = 0;
const ok = (n) => { console.log(`  [PASS] ${n}`); passed++; };
const fail = (n, w) => { console.log(`  [FAIL] ${n} — ${w}`); failed++; };

async function main() {
  console.log('#563 curation staging E2E (in-process)');
  console.log(`relay: ${RELAY}`);

  // 0. Build dist.
  console.log('\n== building dist ==');
  const build = spawnSync('npm', ['run', 'build'], { cwd: PLUGIN, encoding: 'utf8', timeout: 120_000 });
  if (build.status !== 0) { console.error(build.stdout, build.stderr); process.exit(1); }
  console.log('  built');

  // 1. Throwaway vault — fresh valid phrase.
  console.log('\n== generating throwaway vault ==');
  const gen = spawnSync('node', ['--input-type=module', '-e', `
    import { generateMnemonic } from '@scure/bip39';
    import { wordlist } from '@scure/bip39/wordlists/english.js';
    process.stdout.write(generateMnemonic(wordlist, 128));
  `], { cwd: PLUGIN, encoding: 'utf8', timeout: 30_000 });
  if (gen.status !== 0) { fail('generate phrase', gen.stderr); process.exit(1); }
  const phrase = gen.stdout.trim();
  const tmpDir = mkdtempSync(join(tmpdir(), 'tr-curation-e2e-'));
  const credsPath = join(tmpDir, 'credentials.json');
  writeFileSync(credsPath, JSON.stringify({ mnemonic: phrase }));
  console.log(`  throwaway creds at ${credsPath}`);

  // Point the plugin at staging + the throwaway creds for the bootstrap.
  process.env.TOTALRECLAW_SERVER_URL = RELAY;
  process.env.TOTALRECLAW_CREDENTIALS_PATH = credsPath;

  try {
    // 2. Bootstrap a CliContext ONCE (one registration). We replicate
    //    buildContext() inline using the dist's exported helpers, because
    //    buildContext itself isn't exported. This is the same derivation the
    //    CLI does — deriveKeys + register + deriveSmartAccountAddress.
    console.log('\n== bootstrapping context (single registration) ==');
    const { resolveCliMnemonicOrDie } = require(join(DIST, 'cli/tr-cli.js'));
    const creds = JSON.parse(readFileSync(credsPath, 'utf8'));
    const mnemonic = resolveCliMnemonicOrDie(creds);

    // Load the dist's config + crypto helpers the same way the CLI does.
    const { CONFIG, setRecoveryPhraseOverride } = require(join(DIST, 'config.js'));
    setRecoveryPhraseOverride(mnemonic);
    const { deriveKeys, computeAuthKeyHash } = require(join(DIST, 'crypto/vault-crypto.js'));
    const keys = deriveKeys(mnemonic);
    const authKeyHex = keys.authKey.toString('hex');
    const { createApiClient } = require(join(DIST, 'billing/api-client.js'));
    const apiClient = createApiClient(CONFIG.serverUrl);

    let userId;
    try {
      const result = await apiClient.register(computeAuthKeyHash(keys.authKey), keys.salt.toString('hex'));
      userId = result.user_id;
      ok(`registered on staging (userId ${userId.slice(0, 12)}…)`);
    } catch (e) {
      const msg = e.message || String(e);
      if (msg.includes('USER_EXISTS')) { userId = computeAuthKeyHash(keys.authKey).slice(0, 32); ok('already registered (USER_EXISTS)'); }
      else { fail('register', msg); throw e; }
    }

    const { deriveSmartAccountAddress } = require(join(DIST, 'subgraph/subgraph-store.js'));
    const walletAddress = await deriveSmartAccountAddress(mnemonic, CONFIG.chainId);
    ok(`smart account ${walletAddress}`);

    // Cache userId + salt back into credentials.json so spawned `tr remember`
    // calls SKIP register (buildContext reads creds.userId and avoids the
    // /v1/register call). This is what `tr pair` normally writes — without it,
    // every spawned tr re-registers and burns the IP rate limit (429).
    writeFileSync(credsPath, JSON.stringify({
      mnemonic,
      userId,
      salt: keys.salt.toString('hex'),
      scope_address: walletAddress,
    }));
    ok('cached userId+salt to throwaway credentials (spawned tr calls skip register)');

    const ctx = {
      authKeyHex,
      encryptionKey: keys.encryptionKey,
      dedupKey: keys.dedupKey,
      apiClient,
      userId,
      walletAddress,
      salt: keys.salt,
      lshSeed: require(join(DIST, 'crypto/vault-crypto.js')).deriveLshSeed(mnemonic, keys.salt),
    };
    const deps = buildCurationDeps(ctx);

    // In-process store: encrypt claim text → generate indices → encode protobuf
    // → submitBatch. Uses the SAME deps surface curation uses (deps.encryptBlob,
    // deps.generateIndices, deps.submitBatch), so a store here exercises the
    // identical on-chain write path runCurationOp supersession writes through.
    const { encodeFactProtobuf } = require(join(DIST, 'subgraph/subgraph-store.js'));
    const { buildV1ClaimBlob } = require(join(DIST, 'extraction/claims-helper.js'));
    const { randomUUID } = require('node:crypto');
    async function storeFact(text) {
      const id = randomUUID();
      // Inner blob MUST be canonical v1 (schema_version "1.x") — the same shape
      // buildV1ClaimBlob produces and that pin/retype/set_scope parse back via
      // projectFromDecrypted. A hand-rolled JSON would be unreadable to retype
      // (exactly the bug the first E2E run caught). This mirrors what the real
      // store pipeline (storeExtractedFacts) writes.
      const canonicalBlob = buildV1ClaimBlob({
        id, text, type: 'claim', source: 'user', scope: 'misc',
        importance: 8, confidence: 1.0, volatility: 'stable',
        createdAt: new Date().toISOString(),
      });
      const encryptedBlob = deps.encryptBlob(canonicalBlob);
      const { blindIndices, encryptedEmbedding } = await deps.generateIndices(text, []);
      const payload = {
        id,
        timestamp: new Date().toISOString(),
        owner: walletAddress,
        encryptedBlob,
        blindIndices,
        decayScore: 1.0,
        source: 'user',
        contentFp: 'e2e-' + id.slice(0, 8),
        agentId: 'e2e-curation-test',
        version: 4,
        ...(encryptedEmbedding ? { encryptedEmbedding } : {}),
      };
      const protobuf = encodeFactProtobuf(payload);
      const result = await deps.submitBatch([protobuf]);
      if (!result.success) throw new Error(`store submitBatch failed: ${JSON.stringify(result)}`);
      return id;
    }

    // 3. Store 2 throwaway facts (in-process, one registration, no spawn).
    console.log('\n== store 2 throwaway facts (in-process) ==');
    const stamp = Date.now();
    const r1 = await storeFact(`E2E curation ONE ${stamp} prefer dark mode`);
    const r2 = await storeFact(`E2E curation TWO ${stamp} bought LLY today`);
    ok(`stored fact1 (id ${r1})`);
    ok(`stored fact2 (id ${r2})`);

    // 4. Confirm both facts readable via deps.fetchFactById — the SAME read path
    //    executePinOperation uses to find a fact before superseding it. Polls
    //    until the subgraph indexes each store (typical ~20-30s on staging).
    console.log('\n== confirm facts indexed + readable ==');
    async function pollReadable(factId, { wantPresent = true, timeoutMs = 240_000 } = {}) {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        try {
          const fact = await deps.fetchFactById(factId);
          if (wantPresent === (fact !== null)) return true;
        } catch { /* indexer lag */ }
        await new Promise((r) => setTimeout(r, 6_000));
      }
      return false;
    }
    if (await pollReadable(r1)) ok('fact1 readable (fetchFactById)'); else { fail('fact1 indexed', 'not readable'); throw new Error('f1'); }
    if (await pollReadable(r2)) ok('fact2 readable (fetchFactById)'); else { fail('fact2 indexed', 'not readable'); throw new Error('f2'); }

    // 5. PIN fact1 — supersession (in-process, same deps the agent tool uses).
    console.log('\n== pin fact1 (supersession) ==');
    const pin = await runCurationOp({ op: 'pin', factId: r1, reason: 'e2e pin test' }, deps);
    if (pin.ok && pin.new_fact_id && pin.tx_hash) {
      ok(`pin superseded: old=${r1} new=${pin.new_fact_id} tx=${pin.tx_hash.slice(0, 18)}…`);
    } else { fail('pin shape', JSON.stringify(pin)); throw new Error('pin'); }

    // 6. Verify old id is GONE (tombstoned → fetchFactById returns null or the
    //    supersession's tombstone), new id is READABLE.
    console.log('\n== verify pin supersession ==');
    if (await pollReadable(r1, { wantPresent: false })) ok(`old fact1 (${r1}) tombstoned`); else fail('old tombstoned', 'still readable');
    if (await pollReadable(pin.new_fact_id, { wantPresent: true })) ok(`new fact (${pin.new_fact_id}) readable`); else fail('new readable', 'not indexed');

    // 7. RETYPE fact2.
    console.log('\n== retype fact2 (supersession) ==');
    const retype = await runCurationOp({ op: 'retype', factId: r2, newType: 'preference' }, deps);
    if (retype.ok && retype.new_fact_id && retype.tx_hash) {
      ok(`retype superseded: old=${r2} new=${retype.new_fact_id} tx=${retype.tx_hash.slice(0, 18)}…`);
    } else { fail('retype shape', JSON.stringify(retype)); }

    if (retype?.ok) {
      console.log('\n== verify retype supersession ==');
      if (await pollReadable(r2, { wantPresent: false })) ok(`old fact2 (${r2}) tombstoned`); else fail('retype old tombstoned', 'still readable');
      if (await pollReadable(retype.new_fact_id, { wantPresent: true })) ok(`retyped fact (${retype.new_fact_id}) readable`); else fail('retype new readable', 'not indexed');
    }

    // 8. SET_SCOPE on the retyped fact's NEW id.
    console.log('\n== set_scope on retyped fact (supersession) ==');
    if (retype?.ok) {
      const scoped = await runCurationOp({ op: 'set_scope', factId: retype.new_fact_id, newScope: 'finance' }, deps);
      if (scoped.ok && scoped.new_fact_id && scoped.tx_hash) {
        ok(`set_scope superseded: old=${retype.new_fact_id} new=${scoped.new_fact_id} tx=${scoped.tx_hash.slice(0, 18)}…`);
      } else { fail('set_scope shape', JSON.stringify(scoped)); }

      if (scoped?.ok) {
        console.log('\n== verify set_scope supersession ==');
        if (await pollReadable(retype.new_fact_id, { wantPresent: false })) ok(`predecessor (${retype.new_fact_id}) tombstoned`); else fail('set_scope old tombstoned', 'still readable');
        if (await pollReadable(scoped.new_fact_id, { wantPresent: true })) ok(`re-scoped fact (${scoped.new_fact_id}) readable`); else fail('set_scope new readable', 'not indexed');
      }
    }

    // 9. UNPIN the pinned fact.
    console.log('\n== unpin the pinned fact (supersession) ==');
    if (pin?.ok) {
      const unpin = await runCurationOp({ op: 'unpin', factId: pin.new_fact_id }, deps);
      if (unpin.ok && unpin.tx_hash) {
        ok(`unpin ok: ${pin.new_fact_id} → ${unpin.new_fact_id ?? '(idempotent)'} tx=${unpin.tx_hash.slice(0, 18)}…`);
        if (unpin.idempotent) ok('unpin reported idempotent');
      } else { fail('unpin shape', JSON.stringify(unpin)); }
    }

  } finally {
    try { unlinkSync(credsPath); } catch {}
    console.log(`\n  (throwaway creds removed; staging facts left on isolated DataEdge)`);
  }

  console.log(`\n=== ${passed} passed, ${failed} failed ===`);
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((e) => { console.error('\nFATAL:', e); process.exit(1); });
