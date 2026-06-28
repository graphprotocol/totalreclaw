/**
 * embedder-loader.test.ts — full end-to-end regression for the lazy-fetch
 * embedder loader (3.3.1-rc.22+).
 *
 * Failure mode this guards against:
 *   rc.21 baseline has no embedder-loader.ts. The plugin's `embed()` path
 *   eagerly imports `@huggingface/transformers` at install time, which
 *   OOM-kills the OpenClaw gateway on small VPS hosts. This test
 *   exercises the lazy-fetch flow with a mocked fetch + synthetic
 *   bundle. On rc.21 the imports below fail (module not found).
 *
 * Coverage:
 *   1. Cache miss → manifest fetched → bundle downloaded → extracted →
 *      verified → cache hit on the second call (no re-download).
 *   2. Manifest-shape failure short-circuits before any tarball is fetched.
 *   3. Bundle hash mismatch (manifest says X, tarball hashes to Y) errors
 *      out without leaving a partially-extracted cache that future boots
 *      would treat as valid.
 *
 * Run with: `npx tsx embedder-loader.test.ts`
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import zlib from 'node:zlib';

import { loadEmbedder } from './embedder-loader.js';
import { resolveCacheLayout, type BundleManifest } from './embedder-cache.js';

let passed = 0;
let failed = 0;

function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

function mkTmpRoot(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'tr-embedder-loader-'));
}

function rmrf(p: string): void {
  try { fs.rmSync(p, { recursive: true, force: true }); } catch { /* ignore */ }
}

function tarHeader(name: string, size: number): Buffer {
  const header = Buffer.alloc(512, 0);
  header.write(name, 0, 100, 'utf8');
  header.write('0000644 \0', 100, 8, 'utf8');
  header.write('0000000\0', 108, 8, 'utf8');
  header.write('0000000\0', 116, 8, 'utf8');
  header.write(size.toString(8).padStart(11, '0') + ' ', 124, 12, 'utf8');
  header.write(Math.floor(Date.now() / 1000).toString(8).padStart(11, '0') + ' ', 136, 12, 'utf8');
  header.write('        ', 148, 8, 'utf8');
  header[156] = '0'.charCodeAt(0);
  header.write('ustar  \0', 257, 8, 'utf8');
  let sum = 0;
  for (let i = 0; i < 512; i++) sum += header[i];
  header.write(sum.toString(8).padStart(6, '0') + '\0 ', 148, 8, 'utf8');
  return header;
}

function makeTarBuffer(entries: Array<{ name: string; content: Buffer }>): Buffer {
  const chunks: Buffer[] = [];
  for (const { name, content } of entries) {
    chunks.push(tarHeader(name, content.length));
    chunks.push(content);
    const pad = Math.ceil(content.length / 512) * 512 - content.length;
    if (pad > 0) chunks.push(Buffer.alloc(pad, 0));
  }
  chunks.push(Buffer.alloc(1024, 0));
  return Buffer.concat(chunks);
}

function makeTarGz(entries: Array<{ name: string; content: Buffer }>): Buffer {
  return zlib.gzipSync(makeTarBuffer(entries), { level: 6 });
}

function sha256(buf: Buffer): string {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function bytesSha(content: Buffer): string {
  return crypto.createHash('sha256').update(content).digest('hex');
}

function buildSyntheticBundle(): {
  tarGz: Buffer;
  manifest: BundleManifest;
} {
  // Synthetic @huggingface/transformers that mirrors the REAL v4 shape:
  //   - `"type": "module"` (ESM-first package).
  //   - `exports` with separate `import` (ESM `.mjs`) and `require` (CJS
  //     `.cjs`) conditions.
  //   - Named exports `AutoTokenizer`, `AutoModel`, `pipeline`.
  //
  // The CJS entry deliberately re-exports only a SUBSET of the ESM named
  // exports — mirroring the Node 24 interop regression where
  // `require('@huggingface/transformers').AutoModel` is `undefined` even
  // though the ESM `import()` path resolves it. The pre-fix loader used
  // `cacheRequire` and so the destructured `AutoModel` was `undefined`
  // at runtime. The post-fix loader uses `cacheImport` (ESM dynamic
  // import of the resolved file URL) and `AutoModel` resolves.
  const transformersPkg = Buffer.from(
    JSON.stringify({
      name: '@huggingface/transformers',
      type: 'module',
      main: './dist/transformers.node.cjs',
      exports: {
        node: {
          import: { default: './dist/transformers.node.mjs' },
          require: { default: './dist/transformers.node.cjs' },
        },
        default: { default: './dist/transformers.node.mjs' },
      },
    }),
    'utf8',
  );
  // ESM entry — exposes ALL named exports (this is what production uses).
  const transformersEsm = Buffer.from(
    "export const AutoTokenizer = { from_pretrained: async () => ({}) };\n" +
      "export const AutoModel = { from_pretrained: async () => ({ sentence_embedding: { data: new Float32Array(640) } }) };\n" +
      "export const pipeline = async () => ({ data: new Float32Array(640) });\n",
    'utf8',
  );
  // CJS entry — simulates the Node 24 interop regression: returns the
  // namespace object but leaves the named ESM-first exports undefined.
  // Pre-fix, the loader hit this path and `AutoModel` was undefined.
  const transformersCjs = Buffer.from(
    "// CJS entry — Node 24 require() interop leaves named ESM exports undefined.\n" +
      "module.exports = { AutoTokenizer: { from_pretrained: async () => ({}) } /* AutoModel MISSING */ };\n",
    'utf8',
  );
  const modelCfg = Buffer.from(JSON.stringify({ dim: 640 }), 'utf8');

  const entries: Array<{ name: string; content: Buffer }> = [
    { name: 'node_modules/@huggingface/transformers/package.json', content: transformersPkg },
    { name: 'node_modules/@huggingface/transformers/dist/transformers.node.mjs', content: transformersEsm },
    { name: 'node_modules/@huggingface/transformers/dist/transformers.node.cjs', content: transformersCjs },
    { name: 'node_modules/@huggingface/transformers/index.js', content: transformersCjs },
    { name: 'model/config.json', content: modelCfg },
  ];
  const tarGz = makeTarGz(entries);
  const manifest: BundleManifest = {
    version: 'v1',
    model_id: 'harrier-oss-270m-q4',
    dimension: 640,
    tarball_sha256: sha256(tarGz),
    tarball_size_bytes: tarGz.length,
    files: entries.map((e) => ({
      path: e.name,
      sha256: bytesSha(e.content),
      size: e.content.length,
    })),
  };
  return { tarGz, manifest };
}

function makeFetchMock(routes: Map<string, Buffer | string>, fetchCounter: { count: number }): typeof fetch {
  return (async (url: string | URL | Request) => {
    fetchCounter.count++;
    const u = typeof url === 'string' ? url : (url as URL).toString();
    const body = routes.get(u);
    if (body === undefined) {
      return new Response(null, { status: 404, statusText: 'Not Found' });
    }
    if (typeof body === 'string') {
      return new Response(body, { status: 200 });
    }
    return new Response(body, { status: 200 });
  }) as typeof fetch;
}

const RC_TAG = '3.3.1-rc.22';
const BUNDLE_URL_TEMPLATE = 'https://test.invalid/{rcTag}/embedder-{bundleVersion}.tar.gz';
const MANIFEST_URL_TEMPLATE = 'https://test.invalid/{rcTag}/embedder-{bundleVersion}.manifest.json';

function expectedBundleUrl(): string {
  return BUNDLE_URL_TEMPLATE.replace('{rcTag}', RC_TAG).replace('{bundleVersion}', 'v1');
}
function expectedManifestUrl(): string {
  return MANIFEST_URL_TEMPLATE.replace('{rcTag}', RC_TAG).replace('{bundleVersion}', 'v1');
}

// ---------------------------------------------------------------------------
// 1. Cold start → fetched → cached → second call is a cache hit
// ---------------------------------------------------------------------------
{
  console.log('# Cold start → fetch → cache → hit');
  const root = mkTmpRoot();
  try {
    const cacheRoot = path.join(root, 'embedder');
    const { tarGz, manifest } = buildSyntheticBundle();
    const routes = new Map<string, Buffer | string>([
      [expectedManifestUrl(), JSON.stringify(manifest)],
      [expectedBundleUrl(), tarGz],
    ]);
    const counter = { count: 0 };
    const fetchImpl = makeFetchMock(routes, counter);

    const first = await loadEmbedder({
      cacheRoot,
      rcTag: RC_TAG,
      bundleUrlTemplate: BUNDLE_URL_TEMPLATE,
      manifestUrlTemplate: MANIFEST_URL_TEMPLATE,
      fetchImpl,
      log: () => undefined,
    });
    assert(first.wasFetched === true, 'first call performs a fetch');
    assert(first.manifest.model_id === 'harrier-oss-270m-q4', 'verified manifest is returned');
    assert(counter.count === 2, 'first call fetches manifest + bundle (2 round trips)');
    const layout = resolveCacheLayout(cacheRoot);
    assert(fs.existsSync(layout.manifestPath), 'manifest.json persisted to cache');
    assert(
      fs.existsSync(path.join(layout.versionRoot, 'node_modules/@huggingface/transformers/index.js')),
      'transformers shim extracted',
    );

    // Second call: should NOT fetch.
    const second = await loadEmbedder({
      cacheRoot,
      rcTag: RC_TAG,
      bundleUrlTemplate: BUNDLE_URL_TEMPLATE,
      manifestUrlTemplate: MANIFEST_URL_TEMPLATE,
      fetchImpl,
      log: () => undefined,
    });
    assert(second.wasFetched === false, 'second call hits cache (no fetch)');
    assert(counter.count === 2, 'second call does NOT increment fetch counter');

    // The cacheRequire should resolve `@huggingface/transformers` from the bundle.
    // This is a path-resolution smoke — `cacheRequire` still works for resolving
    // the entry even though loading dual CJS/ESM via require() is the broken bit
    // the fix targets.
    const transformersCjs = second.cacheRequire('@huggingface/transformers') as { AutoTokenizer?: unknown };
    assert(typeof transformersCjs === 'object', 'cacheRequire resolves the bundled module');
    assert(transformersCjs.AutoTokenizer !== undefined, 'CJS entry exposes at least its limited surface');

    // REGRESSION (issue: `autoModel is not a function`, Node 24):
    // `cacheImport` must resolve the ESM entry and surface the named
    // `AutoModel` export. Pre-fix, the loader used `cacheRequire` which
    // on Node 24 returns the CJS namespace with `AutoModel` undefined;
    // the test synthetic bundle mirrors that by omitting `AutoModel`
    // from the CJS entry while exposing it from the ESM entry. So this
    // assertion fails on the pre-fix loader and passes on the post-fix
    // loader regardless of host Node version.
    const transformersEsm = (await second.cacheImport('@huggingface/transformers')) as {
      AutoTokenizer?: unknown;
      AutoModel?: unknown;
      pipeline?: unknown;
    };
    assert(typeof transformersEsm.AutoModel === 'object', 'cacheImport surfaces the ESM-named AutoModel export');
    assert(typeof transformersEsm.AutoTokenizer === 'object', 'cacheImport surfaces the ESM-named AutoTokenizer export');
    assert(typeof transformersEsm.pipeline === 'function', 'cacheImport surfaces the ESM-named pipeline export');
  } finally {
    rmrf(root);
  }
}

// ---------------------------------------------------------------------------
// 2. Manifest shape failure short-circuits BEFORE any tarball download
// ---------------------------------------------------------------------------
{
  console.log('# Manifest shape failure short-circuits');
  const root = mkTmpRoot();
  try {
    const cacheRoot = path.join(root, 'embedder');
    const counter = { count: 0 };
    const routes = new Map<string, Buffer | string>([
      [expectedManifestUrl(), JSON.stringify({ version: 'v1', model_id: '' /* invalid */ })],
    ]);
    const fetchImpl = makeFetchMock(routes, counter);
    let threw = false;
    try {
      await loadEmbedder({
        cacheRoot,
        rcTag: RC_TAG,
        bundleUrlTemplate: BUNDLE_URL_TEMPLATE,
        manifestUrlTemplate: MANIFEST_URL_TEMPLATE,
        fetchImpl,
        log: () => undefined,
      });
    } catch (err) {
      threw = true;
      assert(/shape validation|version/.test(String(err)), 'error mentions shape validation');
    }
    assert(threw === true, 'loadEmbedder throws on malformed manifest');
    assert(counter.count === 1, 'only the manifest URL was hit (no bundle download attempted)');
  } finally {
    rmrf(root);
  }
}

// ---------------------------------------------------------------------------
// 3. Bundle hash mismatch — cache is NOT pinned with a half-extracted tree
// ---------------------------------------------------------------------------
{
  console.log('# Bundle hash mismatch — refuses + does not poison cache');
  const root = mkTmpRoot();
  try {
    const cacheRoot = path.join(root, 'embedder');
    const { tarGz, manifest } = buildSyntheticBundle();
    // Lie about the hash in the manifest so the bundle stream-hash != claim.
    const lyingManifest = { ...manifest, tarball_sha256: 'e'.repeat(64) };
    const routes = new Map<string, Buffer | string>([
      [expectedManifestUrl(), JSON.stringify(lyingManifest)],
      [expectedBundleUrl(), tarGz],
    ]);
    const counter = { count: 0 };
    const fetchImpl = makeFetchMock(routes, counter);
    let threw = false;
    try {
      await loadEmbedder({
        cacheRoot,
        rcTag: RC_TAG,
        bundleUrlTemplate: BUNDLE_URL_TEMPLATE,
        manifestUrlTemplate: MANIFEST_URL_TEMPLATE,
        fetchImpl,
        log: () => undefined,
      });
    } catch (err) {
      threw = true;
      assert(/hash mismatch/.test(String(err)), 'error mentions hash mismatch');
    }
    assert(threw === true, 'loadEmbedder throws on tarball hash mismatch');
    const layout = resolveCacheLayout(cacheRoot);
    // Cache must NOT now contain a manifest.json (we'd treat it as valid on next boot).
    assert(!fs.existsSync(layout.manifestPath), 'no manifest.json persisted on hash failure');
  } finally {
    rmrf(root);
  }
}

// ---------------------------------------------------------------------------
// 4. Cold start with hash mismatch then fixed manifest → cache rebuilds
// ---------------------------------------------------------------------------
{
  console.log('# Recover after hash mismatch');
  const root = mkTmpRoot();
  try {
    const cacheRoot = path.join(root, 'embedder');
    const { tarGz, manifest } = buildSyntheticBundle();

    // First attempt: lying manifest.
    let routes = new Map<string, Buffer | string>([
      [expectedManifestUrl(), JSON.stringify({ ...manifest, tarball_sha256: 'f'.repeat(64) })],
      [expectedBundleUrl(), tarGz],
    ]);
    let counter = { count: 0 };
    let fetchImpl = makeFetchMock(routes, counter);
    try {
      await loadEmbedder({
        cacheRoot,
        rcTag: RC_TAG,
        bundleUrlTemplate: BUNDLE_URL_TEMPLATE,
        manifestUrlTemplate: MANIFEST_URL_TEMPLATE,
        fetchImpl,
        log: () => undefined,
      });
    } catch { /* expected */ }

    // Second attempt: honest manifest.
    routes = new Map<string, Buffer | string>([
      [expectedManifestUrl(), JSON.stringify(manifest)],
      [expectedBundleUrl(), tarGz],
    ]);
    counter = { count: 0 };
    fetchImpl = makeFetchMock(routes, counter);
    const ok = await loadEmbedder({
      cacheRoot,
      rcTag: RC_TAG,
      bundleUrlTemplate: BUNDLE_URL_TEMPLATE,
      manifestUrlTemplate: MANIFEST_URL_TEMPLATE,
      fetchImpl,
      log: () => undefined,
    });
    assert(ok.wasFetched === true, 'second attempt fetches a fresh bundle');
    assert(counter.count === 2, 'manifest + bundle both hit on the recovery path');
    const layout = resolveCacheLayout(cacheRoot);
    assert(fs.existsSync(layout.manifestPath), 'manifest.json now exists on disk');
  } finally {
    rmrf(root);
  }
}

console.log(`\n${passed}/${passed + failed} passed`);
if (failed > 0) process.exit(1);
