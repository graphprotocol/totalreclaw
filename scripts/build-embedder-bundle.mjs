#!/usr/bin/env node
/**
 * build-embedder-bundle.mjs — produce `embedder-v1.tar.gz` + matching
 * manifest for the rc.22+ lazy-fetch flow.
 *
 * Why this script exists:
 *   The OpenClaw plugin used to ship `@huggingface/transformers` +
 *   `onnxruntime-node` in its dependencies, plus eagerly download the
 *   ONNX model on first use. rc.21 surfaced an OOM-kill failure on a
 *   3.7 GB Hetzner VPS during `openclaw plugins install` because npm's
 *   peak install RAM with those native packages exceeded the host's
 *   available memory. rc.22 splits the install path: the plugin tarball
 *   ships LIGHT (no native deps), and on first call to `embed()` the
 *   plugin downloads `embedder-v1.tar.gz` from a pinned GitHub Release
 *   into `~/.totalreclaw/embedder/v1/` and lazy-loads the model.
 *
 * What this script produces:
 *   - `dist/embedder/embedder-v1.tar.gz`         — the bundle tarball.
 *   - `dist/embedder/embedder-v1.manifest.json`  — verification metadata.
 *
 * Bundle contents:
 *   - `node_modules/@huggingface/transformers`  (the JS code that loads + runs ONNX)
 *   - `node_modules/onnxruntime-node`           (the native runtime)
 *   - the entire transitive `node_modules/` graph for those two packages,
 *     installed via a one-shot `npm install --no-save --no-audit
 *     --no-fund` into a temp directory so the plugin's own node_modules
 *     stays clean.
 *
 * Manifest fields:
 *   {
 *     "version": "v1",
 *     "model_id": "harrier-oss-270m-q4",
 *     "dimension": 640,
 *     "tarball_sha256": "<hex of the .tar.gz bytes>",
 *     "tarball_size_bytes": <int>,
 *     "files": [ { "path": "<rel>", "sha256": "<hex>", "size": <int> }, ... ]
 *   }
 *
 * The `tarball_sha256` lets the plugin pin bundle integrity to the
 * manifest. The per-file `files` array lets the plugin re-verify the
 * extracted tree on every boot — the cache is cheap to discard, so any
 * mismatch triggers a re-fetch.
 *
 * The script does NOT bake the model weights into the bundle directly.
 * On first run the bundled `transformers` library will fetch the ONNX
 * model from Hugging Face and populate its own cache under
 * `~/.totalreclaw/embedder/v1/transformers-cache/` (the bundle sets
 * `TRANSFORMERS_CACHE` accordingly via the loader entrypoint). Including
 * the ~344 MB model weights in the bundle is supported via the
 * `--include-model` flag — flip it on for offline / air-gapped releases.
 *
 * Usage:
 *   node scripts/build-embedder-bundle.mjs                      # default
 *   node scripts/build-embedder-bundle.mjs --out-dir dist/embedder
 *   node scripts/build-embedder-bundle.mjs --include-model      # bake ONNX
 *   node scripts/build-embedder-bundle.mjs --transformers-version 4.0.1
 *
 * Pipeline integration (rc.22+):
 *   The `npm-publish.yml` plugin job calls this script after the plugin
 *   tarball is published, then `gh release upload v<rcTag>
 *   embedder-v1.tar.gz embedder-v1.manifest.json` so the artifacts land
 *   on the same GitHub Release the plugin's first-run code points at.
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import zlib from 'node:zlib';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..');

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

const args = process.argv.slice(2);
function getArg(name, def) {
  const idx = args.indexOf(`--${name}`);
  if (idx < 0) return def;
  return args[idx + 1] ?? def;
}
function hasFlag(name) {
  return args.includes(`--${name}`);
}

const OUT_DIR = path.resolve(REPO_ROOT, getArg('out-dir', 'dist/embedder'));
const BUNDLE_VERSION = 'v1';
const MODEL_ID = 'harrier-oss-270m-q4';
const MODEL_DIMENSION = 640;
const TRANSFORMERS_VERSION = getArg('transformers-version', '^4.0.1');
const ONNXRUNTIME_VERSION = getArg('onnxruntime-version', '^1.24.0');
const INCLUDE_MODEL = hasFlag('include-model');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function log(...m) { console.error('[build-embedder-bundle]', ...m); }

function rmrf(p) {
  try { fs.rmSync(p, { recursive: true, force: true }); } catch { /* ignore */ }
}

function sha256OfBuffer(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function sha256OfFile(p) {
  return new Promise((resolve, reject) => {
    const hasher = crypto.createHash('sha256');
    const rs = fs.createReadStream(p);
    rs.on('data', (chunk) => hasher.update(chunk));
    rs.on('end', () => resolve(hasher.digest('hex')));
    rs.on('error', reject);
  });
}

// Walk a tree, emit relative paths (POSIX) for each regular file.
function walkFiles(root, base = root) {
  const out = [];
  let entries;
  try { entries = fs.readdirSync(root, { withFileTypes: true }); } catch { return out; }
  for (const e of entries) {
    const full = path.join(root, e.name);
    if (e.isDirectory()) {
      out.push(...walkFiles(full, base));
    } else if (e.isFile()) {
      out.push(path.relative(base, full).split(path.sep).join('/'));
    }
  }
  return out;
}

// Minimal USTAR tar writer (matches embedder-fetch.ts reader).
function tarHeader(name, size, mode = 0o644, type = '0', mtime = Math.floor(Date.now() / 1000)) {
  const header = Buffer.alloc(512, 0);
  if (name.length > 100) {
    // Long-name extension (GNU 'L' typeflag) — write a separate header
    // before this entry. We handle that in `tarPack` below.
    throw new Error(`tarHeader cannot inline names > 100 chars; use tarPack`);
  }
  header.write(name, 0, 100, 'utf8');
  header.write(mode.toString(8).padStart(6, '0') + ' \0', 100, 8, 'utf8');
  header.write('0'.repeat(7) + '\0', 108, 8, 'utf8'); // uid
  header.write('0'.repeat(7) + '\0', 116, 8, 'utf8'); // gid
  header.write(size.toString(8).padStart(11, '0') + ' ', 124, 12, 'utf8');
  header.write(mtime.toString(8).padStart(11, '0') + ' ', 136, 12, 'utf8');
  header.write('        ', 148, 8, 'utf8'); // checksum placeholder
  header[156] = type.charCodeAt(0);
  header.write('ustar  \0', 257, 8, 'utf8'); // GNU ustar magic+version
  // Compute checksum.
  let sum = 0;
  for (let i = 0; i < 512; i++) sum += header[i];
  header.write(sum.toString(8).padStart(6, '0') + '\0 ', 148, 8, 'utf8');
  return header;
}

function tarLongName(name) {
  const body = Buffer.from(name + '\0', 'utf8');
  const header = Buffer.alloc(512, 0);
  header.write('././@LongLink', 0, 100, 'utf8');
  header.write('0000644 \0', 100, 8, 'utf8');
  header.write('0'.repeat(7) + '\0', 108, 8, 'utf8');
  header.write('0'.repeat(7) + '\0', 116, 8, 'utf8');
  header.write(body.length.toString(8).padStart(11, '0') + ' ', 124, 12, 'utf8');
  header.write('0'.repeat(11) + ' ', 136, 12, 'utf8');
  header.write('        ', 148, 8, 'utf8');
  header[156] = 'L'.charCodeAt(0);
  header.write('ustar  \0', 257, 8, 'utf8');
  let sum = 0;
  for (let i = 0; i < 512; i++) sum += header[i];
  header.write(sum.toString(8).padStart(6, '0') + '\0 ', 148, 8, 'utf8');
  const padded = Math.ceil(body.length / 512) * 512;
  const out = Buffer.concat([header, body, Buffer.alloc(padded - body.length, 0)]);
  return out;
}

// Pack an array of `{ name, content }` tuples into a USTAR tar Buffer.
function tarPack(entries) {
  const chunks = [];
  for (const { name, content } of entries) {
    if (name.length > 100) {
      chunks.push(tarLongName(name));
    }
    chunks.push(tarHeader(name.length > 100 ? name.slice(0, 100) : name, content.length, 0o644, '0'));
    chunks.push(content);
    const pad = Math.ceil(content.length / 512) * 512 - content.length;
    if (pad > 0) chunks.push(Buffer.alloc(pad, 0));
  }
  // Two zero blocks signal end-of-archive.
  chunks.push(Buffer.alloc(1024, 0));
  return Buffer.concat(chunks);
}

// ---------------------------------------------------------------------------
// Build
// ---------------------------------------------------------------------------

async function main() {
  log(`Building embedder bundle ${BUNDLE_VERSION} (model=${MODEL_ID}, dim=${MODEL_DIMENSION})`);
  log(`Output dir: ${OUT_DIR}`);
  log(`Transformers: ${TRANSFORMERS_VERSION}, onnxruntime-node: ${ONNXRUNTIME_VERSION}`);
  log(`Include model weights: ${INCLUDE_MODEL ? 'yes' : 'no'}`);

  fs.mkdirSync(OUT_DIR, { recursive: true });

  // Step 1: Stage a temp directory and `npm install` the heavy deps there.
  const stageRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-embedder-build-'));
  log(`Staging in ${stageRoot}`);

  try {
    fs.writeFileSync(
      path.join(stageRoot, 'package.json'),
      JSON.stringify({
        name: '@totalreclaw/embedder-bundle-stage',
        version: '0.0.0',
        private: true,
        dependencies: {
          '@huggingface/transformers': TRANSFORMERS_VERSION,
          'onnxruntime-node': ONNXRUNTIME_VERSION,
        },
      }, null, 2),
    );
    log('Running npm install...');
    const npmRes = spawnSync('npm', ['install', '--no-audit', '--no-fund', '--no-save', '--legacy-peer-deps'], {
      cwd: stageRoot,
      stdio: ['ignore', 'inherit', 'inherit'],
    });
    if (npmRes.status !== 0) {
      throw new Error(`npm install failed with exit ${npmRes.status}`);
    }

    // Step 2 (optional): pre-warm the ONNX model into the bundle.
    if (INCLUDE_MODEL) {
      log('Pre-warming ONNX model weights into bundle (--include-model)...');
      const preWarmCachePath = path.join(stageRoot, 'embedder-cache');
      fs.mkdirSync(preWarmCachePath, { recursive: true });
      const preWarmScript = `
        process.env.TRANSFORMERS_CACHE = ${JSON.stringify(preWarmCachePath)};
        const t = require('@huggingface/transformers');
        (async () => {
          const tok = await t.AutoTokenizer.from_pretrained('onnx-community/harrier-oss-v1-270m-ONNX');
          const m = await t.AutoModel.from_pretrained('onnx-community/harrier-oss-v1-270m-ONNX', { dtype: 'q4' });
          const inputs = await tok('warmup', { return_tensors: 'pt', padding: true });
          await m(inputs);
          console.log('pre-warm ok');
        })().catch((e) => { console.error(e); process.exit(1); });
      `;
      const warmRes = spawnSync('node', ['-e', preWarmScript], {
        cwd: stageRoot,
        stdio: ['ignore', 'inherit', 'inherit'],
        env: { ...process.env, NODE_PATH: path.join(stageRoot, 'node_modules') },
      });
      if (warmRes.status !== 0) {
        throw new Error(`model pre-warm failed with exit ${warmRes.status}`);
      }
    }

    // Step 3: collect every file in node_modules + (optional) embedder-cache.
    const filesToBundle = [];
    const nodeModulesAbs = path.join(stageRoot, 'node_modules');
    for (const rel of walkFiles(nodeModulesAbs)) {
      filesToBundle.push({ rel: `node_modules/${rel}`, abs: path.join(nodeModulesAbs, rel) });
    }
    if (INCLUDE_MODEL) {
      const cacheAbs = path.join(stageRoot, 'embedder-cache');
      for (const rel of walkFiles(cacheAbs)) {
        filesToBundle.push({ rel: `transformers-cache/${rel}`, abs: path.join(cacheAbs, rel) });
      }
    }
    log(`Bundling ${filesToBundle.length} files`);

    // Step 4: build the manifest's per-file table.
    const manifestFiles = [];
    const tarEntries = [];
    for (const { rel, abs } of filesToBundle) {
      const content = fs.readFileSync(abs);
      manifestFiles.push({
        path: rel,
        sha256: sha256OfBuffer(content),
        size: content.length,
      });
      tarEntries.push({ name: rel, content });
    }

    // Step 5: pack tar + gzip, write out.
    log('Packing tar...');
    const tarBuffer = tarPack(tarEntries);
    log(`Tar size: ${tarBuffer.length} bytes; gzipping...`);
    const tarGzBuffer = await new Promise((resolve, reject) => {
      zlib.gzip(tarBuffer, { level: 9 }, (err, out) => err ? reject(err) : resolve(out));
    });
    const tarGzPath = path.join(OUT_DIR, `embedder-${BUNDLE_VERSION}.tar.gz`);
    fs.writeFileSync(tarGzPath, tarGzBuffer);
    log(`Wrote ${tarGzPath} (${tarGzBuffer.length} bytes)`);

    const tarballSha = await sha256OfFile(tarGzPath);

    const manifest = {
      version: BUNDLE_VERSION,
      model_id: MODEL_ID,
      dimension: MODEL_DIMENSION,
      tarball_sha256: tarballSha,
      tarball_size_bytes: tarGzBuffer.length,
      files: manifestFiles,
      // Diagnostic — not consumed by the plugin's verifyCache, but useful
      // for ops debugging and air-gapped mirror auditing.
      _meta: {
        built_at: new Date().toISOString(),
        transformers_version: TRANSFORMERS_VERSION,
        onnxruntime_version: ONNXRUNTIME_VERSION,
        include_model_weights: INCLUDE_MODEL,
      },
    };
    const manifestPath = path.join(OUT_DIR, `embedder-${BUNDLE_VERSION}.manifest.json`);
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
    log(`Wrote ${manifestPath}`);
    log(`tarball_sha256: ${tarballSha}`);
  } finally {
    rmrf(stageRoot);
  }

  log('Done.');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
