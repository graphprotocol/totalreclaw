// scanner-sim: allow — *.test.ts files are excluded from the npm tarball (see "files" array in package.json) and never reach the OpenClaw runtime sandbox. This test spawns the postinstall.mjs CLI via execFileSync to validate its end-to-end behavior; the child_process import is only present in test infrastructure, not shipped code.
/**
 * Regression test for issue #188 — postinstall.mjs atomic dep validation.
 * Regression test for issue #190 — postinstall sweeps `.openclaw-install-stage-*` siblings.
 *
 * Asserts (via subprocess execution since postinstall.mjs is a CLI script):
 *   1. Running postinstall.mjs in a fresh (deps-resolvable) tree exits 0.
 *   2. The `.tr-partial-install` marker is cleared if present.
 *   3. The smoke check passes when all critical deps resolve.
 *   4. (#190) Stale `.openclaw-install-stage-*` siblings in the parent
 *      `extensions/` dir are removed when the postinstall runs from the
 *      plugin root inside an `extensions/` parent.
 *   5. The script's idempotent — re-running on a clean tree is a no-op.
 *   6. The required `TOTALRECLAW_SKIP_POSTINSTALL_RETRY=1` escape hatch
 *      works (the test sandbox cannot reach the npm registry, so the
 *      retry path would always fail; we set the env var to skip it).
 *
 * Run with: `npx tsx postinstall-validation.test.ts`
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));

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

function rmrf(p: string): void {
  try { fs.rmSync(p, { recursive: true, force: true }); } catch { /* swallow */ }
}

/**
 * Stage a fake plugin-extensions tree with a symlinked node_modules so the
 * postinstall script can resolve real critical deps. We re-use the test
 * runner's already-installed node_modules to avoid a registry round-trip.
 */
function stageExtensionsTree(opts: { withStaging?: boolean; withMarker?: boolean }): {
  extensionsDir: string;
  pluginRoot: string;
} {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-postinstall-test-'));
  const extensionsDir = path.join(root, 'extensions');
  const pluginRoot = path.join(extensionsDir, 'totalreclaw');
  fs.mkdirSync(pluginRoot, { recursive: true });

  // Copy postinstall.mjs into the staged plugin root (the script reads
  // its own location to resolve siblings, so it must run in-tree).
  fs.copyFileSync(path.join(here, 'postinstall.mjs'), path.join(pluginRoot, 'postinstall.mjs'));

  // Symlink node_modules from the real plugin tree so require() works.
  fs.symlinkSync(path.join(here, 'node_modules'), path.join(pluginRoot, 'node_modules'), 'dir');

  if (opts.withMarker) {
    fs.writeFileSync(path.join(pluginRoot, '.tr-partial-install'), '');
  }
  if (opts.withStaging) {
    const stagingA = path.join(extensionsDir, '.openclaw-install-stage-aaa111');
    const stagingB = path.join(extensionsDir, '.openclaw-install-stage-bbb222');
    fs.mkdirSync(stagingA);
    fs.mkdirSync(stagingB);
  }
  return { extensionsDir, pluginRoot };
}

function runPostinstall(pluginRoot: string): { stdout: string; stderr: string; status: number } {
  try {
    const out = execFileSync('node', ['./postinstall.mjs'], {
      cwd: pluginRoot,
      env: {
        ...process.env,
        TOTALRECLAW_SKIP_POSTINSTALL_RETRY: '1',
      },
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    return { stdout: out, stderr: '', status: 0 };
  } catch (err) {
    const e = err as { status?: number; stdout?: Buffer | string; stderr?: Buffer | string };
    return {
      stdout: typeof e.stdout === 'string' ? e.stdout : (e.stdout?.toString() ?? ''),
      stderr: typeof e.stderr === 'string' ? e.stderr : (e.stderr?.toString() ?? ''),
      status: e.status ?? -1,
    };
  }
}

// ---------------------------------------------------------------------------
// Test 1 — happy path: marker cleared, deps validate, exit 0
// ---------------------------------------------------------------------------
{
  const { extensionsDir, pluginRoot } = stageExtensionsTree({ withMarker: true });
  const result = runPostinstall(pluginRoot);
  assert(result.status === 0, 'postinstall exits 0 on a healthy tree');
  assert(!fs.existsSync(path.join(pluginRoot, '.tr-partial-install')), '.tr-partial-install marker cleared');
  assert(result.stdout.includes('smoke check OK'), 'smoke check OK message in stdout');
  assert(result.stdout.includes('postinstall complete'), 'postinstall completion message');
  rmrf(path.dirname(extensionsDir));
}

// ---------------------------------------------------------------------------
// Test 2 — issue #190: stale `.openclaw-install-stage-*` siblings get swept
// ---------------------------------------------------------------------------
{
  const { extensionsDir, pluginRoot } = stageExtensionsTree({ withStaging: true });
  // Pre-check: 2 staging dirs exist before running the script.
  assert(fs.existsSync(path.join(extensionsDir, '.openclaw-install-stage-aaa111')), 'staging dir A pre-exists');
  assert(fs.existsSync(path.join(extensionsDir, '.openclaw-install-stage-bbb222')), 'staging dir B pre-exists');

  const result = runPostinstall(pluginRoot);
  assert(result.status === 0, 'postinstall exits 0 with staging cleanup');

  // After-check: both staging dirs gone.
  assert(!fs.existsSync(path.join(extensionsDir, '.openclaw-install-stage-aaa111')), 'staging dir A removed');
  assert(!fs.existsSync(path.join(extensionsDir, '.openclaw-install-stage-bbb222')), 'staging dir B removed');
  // The real plugin root must survive.
  assert(fs.existsSync(pluginRoot), 'real plugin root survived sweep');
  rmrf(path.dirname(extensionsDir));
}

// ---------------------------------------------------------------------------
// Test 3 — sweep does NOT touch unrelated dotfile siblings
// ---------------------------------------------------------------------------
{
  const { extensionsDir, pluginRoot } = stageExtensionsTree({ withStaging: false });
  // Add a non-staging dotfile sibling — the sweep must not delete it.
  const unrelated = path.join(extensionsDir, '.openclaw-cache');
  fs.mkdirSync(unrelated);
  // Add ONE staging dir to ensure the sweep block runs.
  fs.mkdirSync(path.join(extensionsDir, '.openclaw-install-stage-targeted'));

  const result = runPostinstall(pluginRoot);
  assert(result.status === 0, 'postinstall exits 0 (mixed siblings)');
  assert(fs.existsSync(unrelated), '.openclaw-cache sibling NOT touched');
  assert(!fs.existsSync(path.join(extensionsDir, '.openclaw-install-stage-targeted')), 'staging dir removed');
  rmrf(path.dirname(extensionsDir));
}

// ---------------------------------------------------------------------------
// Test 4 — idempotent: a second run on a clean tree exits 0 and is a no-op
// ---------------------------------------------------------------------------
{
  const { extensionsDir, pluginRoot } = stageExtensionsTree({});
  const r1 = runPostinstall(pluginRoot);
  assert(r1.status === 0, 'first run exits 0');
  const r2 = runPostinstall(pluginRoot);
  assert(r2.status === 0, 'second run exits 0 (idempotent)');
  rmrf(path.dirname(extensionsDir));
}

// ---------------------------------------------------------------------------
// Test 5 — when run outside an `extensions/` parent (dev checkout),
//          the staging sweep is skipped safely.
// ---------------------------------------------------------------------------
{
  // Stage a tree where the parent dir is NOT named `extensions` AND has
  // no staging siblings (so the heuristic returns null).
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-postinstall-dev-'));
  const pluginRoot = path.join(root, 'totalreclaw');
  fs.mkdirSync(pluginRoot, { recursive: true });
  fs.copyFileSync(path.join(here, 'postinstall.mjs'), path.join(pluginRoot, 'postinstall.mjs'));
  fs.symlinkSync(path.join(here, 'node_modules'), path.join(pluginRoot, 'node_modules'), 'dir');

  const result = runPostinstall(pluginRoot);
  assert(result.status === 0, 'dev checkout postinstall exits 0');
  assert(result.stdout.includes('skipping staging sweep'), 'sweep skipped for dev checkout');
  rmrf(root);
}

// ---------------------------------------------------------------------------
console.log(`\n${passed}/${passed + failed} passed`);
if (failed > 0) {
  process.exit(1);
}
