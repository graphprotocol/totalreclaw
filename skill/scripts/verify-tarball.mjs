#!/usr/bin/env node
/**
 * verify-tarball.mjs — Pre-publish gate for the @totalreclaw/totalreclaw plugin.
 *
 * Purpose
 *   Issue #110 (rc.18 manual QA): the published npm tarball shipped raw
 *   .ts source — `index.ts` did `import './crypto.js'` but only `crypto.ts`
 *   existed. Gateway loaded fine via OpenClaw's TS loader; CLI subcommand
 *   path that resolved literal `./crypto.js` couldn't find the file.
 *
 *   This gate runs RIGHT BEFORE `npm publish` and asserts:
 *
 *     (1) `dist/` exists in the project tree (built artifact dir).
 *     (2) Every `import './X.js'` referenced in shipped TS sources has a
 *         matching `dist/X.js` file. Tarball ships both `*.ts` AND `dist/`,
 *         so anything that resolves `.js` literally MUST find a built file.
 *     (3) `package.json:openclaw.extensions[0]` points at a file that
 *         exists in `dist/` (the entry the OpenClaw loader will mount).
 *
 *   Failing any check exits non-zero — the publish workflow fails fast
 *   instead of putting a broken tarball on the registry.
 *
 * Wiring
 *   - `npm run verify-tarball` from skill/plugin/ (runs after `prepack`'s build)
 *   - Re-run from `.github/workflows/npm-publish.yml` `publish-plugin` job
 *     as a defense-in-depth step.
 */

import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';

const HERE = path.dirname(url.fileURLToPath(import.meta.url));
// skill/scripts/ -> skill/plugin/
const PLUGIN_DIR = path.resolve(HERE, '..', 'plugin');
const DIST_DIR = path.join(PLUGIN_DIR, 'dist');
const PKG_PATH = path.join(PLUGIN_DIR, 'package.json');

function fail(msg) {
  console.error('verify-tarball: FAIL —', msg);
  process.exit(1);
}

function ok(msg) {
  console.log('verify-tarball: OK —', msg);
}

// ---- Check 1: dist/ exists -------------------------------------------------
if (!fs.existsSync(DIST_DIR) || !fs.statSync(DIST_DIR).isDirectory()) {
  fail(
    `dist/ directory missing at ${DIST_DIR}. Run \`npm run build\` from skill/plugin/ before publishing. ` +
      `The npm tarball must contain compiled .js files so CLI subcommand paths can resolve literal './X.js' imports ` +
      `(see issue #110 — rc.18 shipped only .ts and broke gateway-CLI subcommand resolution).`,
  );
}

// ---- Check 2: every '.js' import target has a corresponding dist/X.js ------
function readSourceImports(dir, prefix = '') {
  const out = new Map(); // basename without ext -> [referencing file]
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.name === 'node_modules' || entry.name === 'dist') continue;
    if (entry.name.startsWith('.')) continue;
    const full = path.join(dir, entry.name);
    const rel = prefix ? path.posix.join(prefix, entry.name) : entry.name;
    if (entry.isDirectory()) {
      for (const [k, v] of readSourceImports(full, rel)) {
        out.set(k, [...(out.get(k) ?? []), ...v]);
      }
      continue;
    }
    if (!entry.name.endsWith('.ts')) continue;
    if (entry.name.endsWith('.test.ts')) continue;
    if (entry.name === 'pocv2-e2e-test.ts') continue;
    const src = fs.readFileSync(full, 'utf8');
    // Match `from './foo.js'`, `import('./foo.js')`, `from './sub/foo.js'`, etc.
    const re = /(?:from|import\()\s*['"](\.\/[^'"]+\.js)['"]\)?/g;
    let m;
    while ((m = re.exec(src)) !== null) {
      const importPath = m[1]; // e.g. './crypto.js' or './import-adapters/types.js'
      // Resolve relative to the file's own dir (so subdir imports are correct)
      const fileDir = path.dirname(rel);
      const resolved = path.posix.normalize(
        path.posix.join(fileDir, importPath.replace(/^\.\//, '')),
      );
      const distRel = resolved; // already relative to PLUGIN_DIR root
      const arr = out.get(distRel) ?? [];
      arr.push(rel);
      out.set(distRel, arr);
    }
  }
  return out;
}

const imports = readSourceImports(PLUGIN_DIR);
const missing = [];
for (const [importTarget, refs] of imports.entries()) {
  const distFile = path.join(DIST_DIR, importTarget);
  if (!fs.existsSync(distFile)) {
    missing.push({ importTarget, refs });
  }
}
if (missing.length > 0) {
  console.error('verify-tarball: FAIL — missing dist/ artifacts for these .js imports:');
  for (const { importTarget, refs } of missing.slice(0, 20)) {
    console.error(`  - dist/${importTarget}  (referenced by ${refs.slice(0, 3).join(', ')}${refs.length > 3 ? ', ...' : ''})`);
  }
  if (missing.length > 20) {
    console.error(`  ... and ${missing.length - 20} more`);
  }
  console.error('');
  console.error(
    'Run `npm run build` from skill/plugin/ to regenerate dist/. The TS source uses ` /* */ from \'./X.js\'` style ' +
      'imports (Node ESM convention); the .js extension MUST resolve to a real file at runtime, ' +
      'and OpenClaw\'s `jiti` loader is not guaranteed to be available on every plugin entry path.',
  );
  process.exit(1);
}
ok(`${imports.size} '.js' import targets — all resolve to dist/`);

// ---- Check 3: openclaw.extensions[0] points at an existing dist/ file ------
const pkg = JSON.parse(fs.readFileSync(PKG_PATH, 'utf8'));
const exts = pkg?.openclaw?.extensions;
if (!Array.isArray(exts) || exts.length === 0) {
  fail('package.json has no openclaw.extensions entry');
}
const entry = exts[0];
const entryAbs = path.resolve(PLUGIN_DIR, entry);
if (!fs.existsSync(entryAbs)) {
  fail(
    `openclaw.extensions[0] = "${entry}" but ${entryAbs} does not exist. ` +
      `Run \`npm run build\` to regenerate dist/, or update openclaw.extensions to point at a built file.`,
  );
}
ok(`openclaw.extensions[0] (${entry}) resolves to a built file`);

// ---- Check 4: smoke-load the entry to catch syntax errors ------------------
//
// This catches TS-emitted JS that node refuses to parse (rare but possible
// when --noCheck masks a syntax-level problem). We `node --check` the
// file rather than executing it, so we don't trigger the heavy module-load
// side effects.
import { execSync } from 'node:child_process';
try {
  execSync(`node --check ${JSON.stringify(entryAbs)}`, { stdio: 'pipe' });
  ok(`node --check ${path.relative(PLUGIN_DIR, entryAbs)} — syntax valid`);
} catch (err) {
  const stderr = err?.stderr?.toString?.() ?? String(err);
  fail(`node --check failed on ${entry}:\n${stderr}`);
}

console.log('verify-tarball: all checks passed.');
