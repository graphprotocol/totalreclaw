// scanner-sim: allow — dev-only test runner, not in the npm tarball ("files" allowlist in package.json excludes it) and never reaches the OpenClaw runtime sandbox. Spawning test files via child_process is this script's entire purpose; the import is test-infrastructure only and not shipped. Invoked only as `node run-tests.mjs` (see package.json "test"), so no shebang is needed.
// ⚠️⚠️⚠️ DO NOT SHIP THIS FILE — read this before editing ⚠️⚠️⚠️
// This dev-only test runner MUST stay out of the published npm tarball. It
// imports `child_process` and spawns subprocesses — exactly what OpenClaw's
// ClawHub install scanner refuses. The `// scanner-sim: allow` comment on the
// FIRST line above only appeases OUR local `scripts/check-scanner.mjs`
// simulation; the REAL ClawHub install scanner ignores that comment entirely
// (the 3.3.1-rc.1 NO-GO scenario). If `run-tests.mjs` (or any `*.mjs` matching
// the scanner rules) ever lands in package.json `"files"`, every ClawHub
// install goes NO-GO. The runner asserts this invariant at startup (see
// `assertNotShipped`) — keep the `scanner-sim: allow` line as line 1, since
// the local scanner only scans the first 5 lines for the suppression marker.
// Test runner for skill/plugin: discovers every *.test.ts under this package
// (recursively, excluding node_modules/ and dist/) and runs each via `npx tsx`
// sequentially, failing fast-free (runs all, reports every failure) with a
// per-file pass/fail summary and a non-zero exit if any file fails.
//
// Replaces the previously hand-enumerated `test` script, which had drifted:
// 33 *.test.ts files existed but were never listed, so CI never ran them.
//
// Run a single file the same way it always worked: `npx tsx <file>.test.ts`.

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const ROOT = dirname(fileURLToPath(import.meta.url));

// Per-file wall-clock budget. A hung await, a real-timer sleep, or a
// pathological retry loop in a single test file would otherwise stall the
// whole suite (and the CI job) with no signal — `memory/pin-unpin.test.ts`
// previously clocked 481s here because it reached a live-subgraph poll loop.
// Override locally with RUN_TESTS_TIMEOUT_MS=<ms>. Default 120s leaves ample
// headroom under the CI job budget for any single legit file.
const PER_FILE_TIMEOUT_MS = Number(process.env.RUN_TESTS_TIMEOUT_MS || 120_000);

// Guard the shipping invariant described in the file-header warning: this
// runner must NEVER appear in package.json "files". The real ClawHub scanner
// ignores `// scanner-sim: allow`; a stray `*.mjs` entry would NO-GO installs.
function assertNotShipped() {
  const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));
  const filesList = Array.isArray(pkg.files) ? pkg.files : [];
  const leaked = filesList.filter((p) => typeof p === 'string' && /run-tests\.mjs/.test(p));
  if (leaked.length) {
    console.error(
      `FATAL: package.json "files" includes ${leaked.map((f) => `"${f}"`).join(', ')} — ` +
        'this dev-only test runner must NOT ship in the tarball (ClawHub scanner NO-GO). Remove it from "files".',
    );
    process.exit(1);
  }
}
assertNotShipped();

// Explicit, visible exclusions. Anything here is NOT run by `npm test`.
// Each entry MUST carry a reason. These are files that require a build
// artifact, live network, or secret env vars — i.e. cannot pass in plain CI.
const EXCLUDE = new Map([
  [
    'dist-esm-smoke.test.ts',
    'Requires a built ./dist (ESM smoke test of compiled output). Run via `npm run smoke:dist` after `npm run build`.',
  ],
]);

const SKIP_DIRS = new Set(['node_modules', 'dist', '.git']);

function discover(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (SKIP_DIRS.has(name)) continue;
      out.push(...discover(full));
    } else if (name.endsWith('.test.ts')) {
      out.push(full);
    }
  }
  return out;
}

const all = discover(ROOT)
  .map((f) => relative(ROOT, f))
  .sort();

const excluded = [];
const toRun = [];
for (const f of all) {
  if (EXCLUDE.has(f)) excluded.push(f);
  else toRun.push(f);
}

console.log(`Discovered ${all.length} test file(s): ${toRun.length} to run, ${excluded.length} excluded.`);
if (excluded.length) {
  console.log('Excluded:');
  for (const f of excluded) console.log(`  - ${f}: ${EXCLUDE.get(f)}`);
}
console.log('');

const failures = [];
const started = Date.now();

for (let i = 0; i < toRun.length; i++) {
  const file = toRun[i];
  const label = `[${i + 1}/${toRun.length}] ${file}`;
  const t0 = Date.now();
  const res = spawnSync('npx', ['tsx', file], {
    cwd: ROOT,
    stdio: 'inherit',
    encoding: 'utf8',
    timeout: PER_FILE_TIMEOUT_MS,
  });
  const secs = ((Date.now() - t0) / 1000).toFixed(1);
  if (res.timedOut) {
    console.log(
      `FAIL ${label} (TIMED OUT after ${secs}s, limit ${PER_FILE_TIMEOUT_MS / 1000}s)`,
    );
    failures.push({ file, status: 'TIMEOUT' });
  } else if (res.status === 0) {
    console.log(`PASS ${label} (${secs}s)`);
  } else {
    console.log(
      `FAIL ${label} (exit ${res.status}${res.signal ? ` signal ${res.signal}` : ''}, ${secs}s)`,
    );
    failures.push({ file, status: res.status });
  }
  console.log('');
}

const total = ((Date.now() - started) / 1000).toFixed(1);
console.log('='.repeat(60));
console.log(`Ran ${toRun.length} file(s) in ${total}s. ${toRun.length - failures.length} passed, ${failures.length} failed.`);
if (failures.length) {
  console.log('Failures:');
  for (const { file, status } of failures) console.log(`  - ${file} (exit ${status})`);
  process.exit(1);
}
console.log('All tests passed.');
