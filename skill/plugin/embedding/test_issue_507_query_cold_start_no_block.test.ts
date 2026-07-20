/**
 * test_issue_507_query_cold_start_no_block.test.ts — Regression for #507.
 *
 * S-PAIR-FRESH: on a fresh install the ~700 MB embedder bundle is still being
 * downloaded (background prefetch) when the first user message arrives. The
 * `before_agent_start` recall hook calls `generateEmbedding(prompt, { isQuery:
 * true })`, whose cold path used to `await loadEmbedder()` — blocking the whole
 * interactive turn on the download until the client's 180s timeout fired.
 *
 * The fix: query embeddings fail fast (throw `EmbedderNotReadyError`) when the
 * bundle isn't ready on disk / in memory, so the recall call site degrades to
 * word-only trapdoors and the turn returns promptly. Write/store embeddings
 * (no `isQuery`) still take the blocking load so facts are always embedded.
 *
 * These assertions are network-free: they exercise the readiness probe and the
 * fast-fail branch against a temp cache dir, never reaching GitHub Releases.
 *
 * Run with: npx tsx test_issue_507_query_cold_start_no_block.test.ts
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  configureEmbedder,
  generateEmbedding,
  isEmbedderReady,
  shouldDeferColdEmbedderLoad,
  EmbedderNotReadyError,
} from './embedding.ts';
import { resolveCacheLayout, BUNDLE_FORMAT_VERSION } from './embedder-cache.ts';

let passed = 0;
let failed = 0;

function assert(condition: boolean, name: string): void {
  const n = passed + failed + 1;
  if (condition) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

const HEX64 = 'a'.repeat(64);

/** A shape-valid manifest so `quickCacheProbe` reports the bundle present. */
function writeValidManifest(cacheRoot: string): void {
  const layout = resolveCacheLayout(cacheRoot);
  fs.mkdirSync(layout.versionRoot, { recursive: true });
  fs.writeFileSync(
    layout.manifestPath,
    JSON.stringify({
      version: BUNDLE_FORMAT_VERSION,
      model_id: 'harrier-oss-270m-q4',
      dimension: 640,
      tarball_sha256: HEX64,
      tarball_size_bytes: 0,
      files: [],
    }),
    'utf8',
  );
}

// ---------------------------------------------------------------------------
// 1. Bundle NOT ready (empty cache dir): probe is false, and only *queries*
//    defer the cold load — writes still take the blocking path.
// ---------------------------------------------------------------------------
{
  const emptyCache = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-507-empty-'));
  configureEmbedder({ cacheRoot: emptyCache, rcTag: '3.4.0-rc.2' });

  assert(isEmbedderReady() === false, 'not-ready: isEmbedderReady() is false with no bundle on disk');
  assert(
    shouldDeferColdEmbedderLoad({ isQuery: true }) === true,
    'not-ready: query embedding defers the cold load',
  );
  assert(
    shouldDeferColdEmbedderLoad({ isQuery: false }) === false,
    'not-ready: write embedding does NOT defer (still block-and-download)',
  );
  assert(
    shouldDeferColdEmbedderLoad(undefined) === false,
    'not-ready: default (no options) is treated as a write — no defer',
  );
}

// ---------------------------------------------------------------------------
// 2. A query embedding fails FAST (no network) when the bundle isn't ready —
//    this is the actual #507 fix: the interactive turn must not block on the
//    download.
// ---------------------------------------------------------------------------
{
  const emptyCache = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-507-query-'));
  configureEmbedder({ cacheRoot: emptyCache, rcTag: '3.4.0-rc.2' });

  let caught: unknown = null;
  const start = Date.now();
  try {
    await generateEmbedding('remember that my name is Pedro', { isQuery: true });
  } catch (err) {
    caught = err;
  }
  const elapsedMs = Date.now() - start;

  assert(caught instanceof EmbedderNotReadyError, 'query cold-start: throws EmbedderNotReadyError');
  assert(elapsedMs < 1_000, `query cold-start: fails fast (${elapsedMs}ms < 1000ms, no download attempted)`);
}

// ---------------------------------------------------------------------------
// 3. Bundle present on disk: probe flips to ready, and queries stop deferring
//    (they proceed to the normal in-memory load path once prefetch lands).
// ---------------------------------------------------------------------------
{
  const readyCache = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-507-ready-'));
  writeValidManifest(readyCache);
  configureEmbedder({ cacheRoot: readyCache, rcTag: '3.4.0-rc.2' });

  assert(isEmbedderReady() === true, 'ready: isEmbedderReady() is true once the bundle manifest is on disk');
  assert(
    shouldDeferColdEmbedderLoad({ isQuery: true }) === false,
    'ready: query embedding no longer defers — proceeds to normal load',
  );
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`# fail: ${failed}`);
console.log(`# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('SOME TESTS FAILED');
  process.exit(1);
}
console.log('ALL TESTS PASSED');
