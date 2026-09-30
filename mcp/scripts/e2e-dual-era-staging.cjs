#!/usr/bin/env node
/**
 * Staging E2E for DEP-15 (MCP dual-era protocol support).
 *
 * NOT part of `npm test`. Drives the BUILT stdio server (`dist/index.js`)
 * exactly as a host does — a child process speaking newline-delimited
 * JSON-RPC — in BOTH protocol eras, against the LIVE staging relay
 * (https://api-staging.totalreclaw.xyz). Requires `npm run build` first.
 * The first run downloads the Harrier embedding model (~344 MB) into
 * node_modules/@huggingface/transformers/.cache.
 *
 * Flow:
 *   1. Generate a FRESH throwaway BIP-39 mnemonic and register its vault
 *      with staging (`registerWithServer`, as scripts/e2e-bundle-smoke.cjs
 *      does) so the server's startup billing lookup resolves the staging
 *      DataEdge. The mnemonic reaches the child through its environment
 *      only — never logged, never written to disk.
 *   2. Legacy era (2025-11-25): spawn the server, run the preflight (the
 *      server's startup stderr line `dataEdge=0x…` MUST equal the STAGING
 *      DataEdge, else abort before any write), `initialize`,
 *      `totalreclaw_remember` a marker fact, then poll `totalreclaw_recall`
 *      until the marker comes back from the staging subgraph.
 *   3. Modern era (2026-07-28): spawn a FRESH server with the same mnemonic,
 *      preflight again, `server/discover`, `totalreclaw_recall` (with the
 *      per-request `_meta` envelope) must find the marker, then
 *      `totalreclaw_forget` it — a write in the modern era.
 *
 * Exit 0 = PASS. Never touches production: the relay URL is hard-coded to
 * staging and the preflight refuses to continue unless the server resolved
 * the staging DataEdge.
 */

'use strict';

const { spawn } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { generateMnemonic } = require('@scure/bip39');
const { wordlist } = require('@scure/bip39/wordlists/english.js');

const STAGING_URL = 'https://api-staging.totalreclaw.xyz';
const STAGING_DATA_EDGE = '0xE7a4D2677B686e13775Ba9092631089e35F0BB91';
const DIST_DIR = path.join(__dirname, '..', 'dist');
const DIST_ENTRY = path.join(DIST_DIR, 'index.js');
const MODERN_META = {
  'io.modelcontextprotocol/protocolVersion': '2026-07-28',
  'io.modelcontextprotocol/clientInfo': { name: 'dep15-e2e', version: '0.0.0' },
  'io.modelcontextprotocol/clientCapabilities': {},
};

function log(msg) {
  console.log(`[e2e-dual-era] ${msg}`);
}

// Failure-path cleanup state: every live child process and the throwaway
// HOME. The happy path tears these down in main()'s finally blocks, which
// process.exit() in fail() would skip — so fail() does it itself.
const activeChildren = new Set();
let activeHome;

function fail(msg) {
  console.error(`[e2e-dual-era] FAIL: ${msg}`);
  for (const child of activeChildren) {
    try {
      child.stdin.end();
    } catch {
      // stream already destroyed
    }
    try {
      child.kill('SIGKILL');
    } catch {
      // already exited
    }
  }
  if (activeHome) {
    try {
      fs.rmSync(activeHome, { recursive: true, force: true });
    } catch {
      // best effort
    }
  }
  process.exit(1);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Spawn dist/index.js against staging; returns a tiny JSON-RPC driver. */
function startServer(mnemonic, home, sessionTag) {
  const child = spawn(process.execPath, [DIST_ENTRY], {
    env: {
      PATH: process.env.PATH || '',
      HOME: home,
      TOTALRECLAW_SERVER_URL: STAGING_URL,
      TOTALRECLAW_RECOVERY_PHRASE: mnemonic,
      TOTALRECLAW_SESSION_ID: sessionTag,
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  activeChildren.add(child);
  child.on('exit', () => activeChildren.delete(child));
  const redact = (text) => text.split(mnemonic).join('<redacted>');
  let stderrText = '';
  child.stderr.on('data', (chunk) => {
    stderrText += redact(chunk.toString('utf8'));
  });
  const waiters = new Map();
  let buffer = '';
  child.stdout.on('data', (chunk) => {
    buffer += chunk.toString('utf8');
    let newline = buffer.indexOf('\n');
    while (newline >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) {
        let msg;
        try {
          msg = JSON.parse(line);
        } catch {
          fail(`server wrote a non-JSON line to stdout: ${redact(line).slice(0, 200)}`);
        }
        const waiter = msg.id !== undefined ? waiters.get(msg.id) : undefined;
        if (waiter) {
          waiters.delete(msg.id);
          waiter(msg);
        }
      }
      newline = buffer.indexOf('\n');
    }
  });

  const write = (msg) => child.stdin.write(JSON.stringify(msg) + '\n');
  return {
    child,
    stderr: () => stderrText,
    notify: (method, params) => write({ jsonrpc: '2.0', method, ...(params ? { params } : {}) }),
    request: (id, method, params, timeoutMs) =>
      new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          waiters.delete(id);
          reject(new Error(`no response to ${method} within ${timeoutMs} ms`));
        }, timeoutMs);
        waiters.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
        write({ jsonrpc: '2.0', id, method, ...(params ? { params } : {}) });
      }),
    stop: () => {
      child.stdin.end();
      child.kill('SIGTERM');
    },
  };
}

/** Block until the server logged its resolved DataEdge; abort unless it is staging. */
async function preflight(server, label) {
  const deadline = Date.now() + 180000;
  for (;;) {
    const text = server.stderr();
    if (/PRODUCTION default/.test(text)) {
      server.stop();
      fail(`${label}: server fell back to the PRODUCTION DataEdge — aborting before any write`);
    }
    if (/fatal credential error/.test(text)) {
      server.stop();
      fail(`${label}: server refused the credential`);
    }
    const match = text.match(/dataEdge=(0x[0-9a-fA-F]{40})/);
    if (match) {
      if (match[1].toLowerCase() !== STAGING_DATA_EDGE.toLowerCase()) {
        server.stop();
        fail(`${label}: server resolved DataEdge ${match[1]}, expected staging ${STAGING_DATA_EDGE}`);
      }
      if (!/managed service/.test(text)) {
        await sleep(500);
        continue;
      }
      log(`${label}: preflight OK — DataEdge ${match[1]} (staging)`);
      return;
    }
    if (server.child.exitCode !== null) fail(`${label}: server exited during startup`);
    if (Date.now() > deadline) {
      server.stop();
      fail(`${label}: no dataEdge= line on stderr within 180 s`);
    }
    await sleep(500);
  }
}

function toolPayload(res, label) {
  if (res.error) fail(`${label}: JSON-RPC error ${JSON.stringify(res.error)}`);
  const text = res.result && res.result.content && res.result.content[0] && res.result.content[0].text;
  if (typeof text !== 'string') fail(`${label}: tool result has no text content`);
  return JSON.parse(text);
}

async function recallUntilFound(server, marker, params, label, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let attempt = 0;
  for (;;) {
    attempt += 1;
    const res = await server.request(`recall-${attempt}`, 'tools/call', params, 300000);
    const payload = toolPayload(res, `${label} recall #${attempt}`);
    const hit = (payload.memories || []).find((m) => typeof m.fact_text === 'string' && m.fact_text.includes(marker));
    if (hit) return { hit, res };
    if (Date.now() > deadline) fail(`${label}: marker not recalled within ${timeoutMs / 1000} s`);
    await sleep(10000);
  }
}

async function main() {
  if (!fs.existsSync(DIST_ENTRY)) fail('dist/index.js missing — run `npm run build` first');
  const mnemonic = generateMnemonic(wordlist, 128);
  const marker = crypto.randomBytes(6).toString('hex');
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-dep15-e2e-'));
  activeHome = home;
  const sessionTag = `dep15-e2e-${marker}`;
  const factText = `DEP-15 dual-era staging smoke: the marker word for this run is ${marker}.`;
  const query = `DEP-15 dual-era staging smoke marker word ${marker}`;
  log(`run ${sessionTag} against ${STAGING_URL}`);

  // Register the throwaway vault first: an unregistered auth key gets 401
  // from /v1/billing/status, and the server would then fall back to the
  // PRODUCTION DataEdge (the preflight would abort).
  const cryptoMod = require(path.join(DIST_DIR, 'subgraph', 'crypto.js'));
  const setupMod = require(path.join(DIST_DIR, 'cli', 'setup.js'));
  const { authKey, salt } = cryptoMod.deriveKeys(mnemonic);
  const userId = await setupMod.registerWithServer(
    STAGING_URL,
    cryptoMod.computeAuthKeyHash(authKey),
    Buffer.from(salt).toString('hex'),
  );
  if (!userId) fail('registration with the staging relay returned no user id');
  log(`registered throwaway vault (user ${String(userId).slice(0, 8)}…)`);

  // ── Legacy era ────────────────────────────────────────────────────────
  const legacy = startServer(mnemonic, home, sessionTag);
  try {
    await preflight(legacy, 'legacy');
    const init = await legacy.request(1, 'initialize', {
      protocolVersion: '2025-11-25',
      capabilities: {},
      clientInfo: { name: 'dep15-e2e', version: '0.0.0' },
    }, 30000);
    if (!init.result || init.result.protocolVersion !== '2025-11-25') fail(`legacy: bad initialize result ${JSON.stringify(init).slice(0, 200)}`);
    legacy.notify('notifications/initialized');
    const remember = await legacy.request(2, 'tools/call', {
      name: 'totalreclaw_remember',
      arguments: { fact: factText, importance: 6 },
    }, 600000);
    const stored = toolPayload(remember, 'legacy remember');
    if (!stored.success) fail(`legacy remember did not store: ${JSON.stringify(stored).slice(0, 300)}`);
    const txHash = stored.results && stored.results[0] && stored.results[0].tx_hash;
    log(`legacy: remember OK (tx ${txHash || 'n/a'})`);
    const { hit } = await recallUntilFound(
      legacy,
      marker,
      { name: 'totalreclaw_recall', arguments: { query, k: 8 } },
      'legacy',
      240000,
    );
    log(`legacy: recall OK (fact ${hit.fact_id})`);
  } finally {
    legacy.stop();
  }

  // ── Modern era ────────────────────────────────────────────────────────
  const modern = startServer(mnemonic, home, sessionTag);
  try {
    await preflight(modern, 'modern');
    const discover = await modern.request('discover-1', 'server/discover', { _meta: MODERN_META }, 30000);
    if (!discover.result || !Array.isArray(discover.result.supportedVersions) || !discover.result.supportedVersions.includes('2026-07-28')) {
      fail(`modern: bad server/discover result ${JSON.stringify(discover).slice(0, 200)}`);
    }
    const { hit, res } = await recallUntilFound(
      modern,
      marker,
      { name: 'totalreclaw_recall', arguments: { query, k: 8 }, _meta: MODERN_META },
      'modern',
      120000,
    );
    if (res.result.resultType !== 'complete') fail('modern: recall result lacks resultType "complete"');
    log(`modern: recall OK (fact ${hit.fact_id})`);
    const forget = await modern.request('forget-1', 'tools/call', {
      name: 'totalreclaw_forget',
      arguments: { fact_id: hit.fact_id },
      _meta: MODERN_META,
    }, 300000);
    const forgot = toolPayload(forget, 'modern forget');
    if (forgot.deleted_count !== 1) fail(`modern forget did not tombstone: ${JSON.stringify(forgot).slice(0, 300)}`);
    log(`modern: forget OK (tx ${forgot.tx_hash || 'n/a'})`);
  } finally {
    modern.stop();
  }

  fs.rmSync(home, { recursive: true, force: true });
  log('PASS — both eras wrote to and read from the STAGING DataEdge');
  process.exit(0);
}

main().catch((err) => fail(err && err.message ? err.message : String(err)));
