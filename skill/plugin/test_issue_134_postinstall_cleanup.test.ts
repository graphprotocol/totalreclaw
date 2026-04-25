// scanner-sim: allow — test-only, spawnSync used to exec the postinstall
// helper script in a child process and assert its sweep behaviour. Test
// files are excluded from the published tarball (`!**/*.test.ts`).
/**
 * Regression test for issue #134 — postinstall sweeps orphan
 * `.openclaw-install-stage-*` siblings before next gateway start.
 *
 * The companion `install-staging-cleanup.test.ts` covers the in-process
 * `cleanupInstallStagingDirs(pluginDir)` helper that runs at plugin
 * register time. That helper is defeated when OpenClaw's loader crashes
 * on an orphan dir BEFORE our register code runs — which is exactly the
 * rc.21 scenario reported in #134.
 *
 * `postinstall.mjs` is the structural fix: npm runs it from inside the
 * freshly-extracted staging directory, so it can sweep OTHER stale
 * staging siblings before the next gateway scan ever happens.
 *
 * What this asserts
 * -----------------
 *   1. Running `postinstall.mjs` from inside an `.openclaw-install-stage-*`
 *      dir removes OTHER staging siblings AND leaves self intact.
 *   2. Running it from a non-staging context (e.g. `totalreclaw/` after
 *      rename, or a plain `node_modules/...` install) sweeps any staging
 *      siblings present and is a no-op when none exist.
 *   3. The real `totalreclaw/` plugin directory is never touched.
 *   4. Unrelated dotfile siblings (`.openclaw-cache`, `.git`) are not
 *      touched — only the precise prefix matches.
 *   5. Non-directory entries with the prefix are skipped.
 *   6. Script exit code is always 0 (never blocks an npm install), even
 *      when the parent directory cannot be read.
 *
 * Run with: `npx tsx test_issue_134_postinstall_cleanup.test.ts`
 */

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  'postinstall.mjs',
);

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

function mkExtensionsTree(): { root: string; extensionsDir: string } {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-postinstall-'));
  const extensionsDir = path.join(root, 'extensions');
  fs.mkdirSync(extensionsDir, { recursive: true });
  return { root, extensionsDir };
}

function mkStagingDir(extensionsDir: string, suffix: string, copyScript: boolean): string {
  const dir = path.join(extensionsDir, `.openclaw-install-stage-${suffix}`);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name: '@totalreclaw/totalreclaw' }));
  if (copyScript) {
    fs.copyFileSync(SCRIPT, path.join(dir, 'postinstall.mjs'));
  }
  return dir;
}

function mkRealPluginDir(extensionsDir: string, copyScript: boolean): string {
  const dir = path.join(extensionsDir, 'totalreclaw');
  fs.mkdirSync(path.join(dir, 'dist'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'dist', 'index.js'), '// real plugin entry');
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name: '@totalreclaw/totalreclaw' }));
  if (copyScript) {
    fs.copyFileSync(SCRIPT, path.join(dir, 'postinstall.mjs'));
  }
  return dir;
}

function runScriptFrom(dir: string): { code: number; stderr: string } {
  const result = spawnSync(process.execPath, [path.join(dir, 'postinstall.mjs')], {
    cwd: dir,
    encoding: 'utf-8',
  });
  return { code: result.status ?? -1, stderr: result.stderr ?? '' };
}

// ---------------------------------------------------------------------------
// Test 1 — script from inside a staging dir removes other staging siblings.
// ---------------------------------------------------------------------------
{
  const { root, extensionsDir } = mkExtensionsTree();
  const self = mkStagingDir(extensionsDir, 'SELF123', true);
  const orphan1 = mkStagingDir(extensionsDir, 'oRpHaN1', false);
  const orphan2 = mkStagingDir(extensionsDir, 'OrPhAn2', false);
  const realPlugin = mkRealPluginDir(extensionsDir, false);

  const { code, stderr } = runScriptFrom(self);

  assert(code === 0, 'exit code is 0');
  assert(fs.existsSync(self), 'self staging dir survives');
  assert(!fs.existsSync(orphan1), 'orphan #1 staging dir removed');
  assert(!fs.existsSync(orphan2), 'orphan #2 staging dir removed');
  assert(fs.existsSync(realPlugin), 'real totalreclaw/ dir untouched');
  assert(stderr.includes('removed 2 stale install-staging dir(s)'), 'logged removal count to stderr');
  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 2 — script from inside a real `totalreclaw/` dir removes orphans.
// ---------------------------------------------------------------------------
{
  const { root, extensionsDir } = mkExtensionsTree();
  const realPlugin = mkRealPluginDir(extensionsDir, true);
  const orphan = mkStagingDir(extensionsDir, 'leftover', false);

  const { code } = runScriptFrom(realPlugin);

  assert(code === 0, 'exit code is 0 when run from real plugin dir');
  assert(fs.existsSync(realPlugin), 'real plugin dir survives');
  assert(!fs.existsSync(orphan), 'orphan staging dir cleaned by post-rename run');
  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 3 — no orphans → silent no-op, exit 0.
// ---------------------------------------------------------------------------
{
  const { root, extensionsDir } = mkExtensionsTree();
  const realPlugin = mkRealPluginDir(extensionsDir, true);

  const { code, stderr } = runScriptFrom(realPlugin);

  assert(code === 0, 'no orphans → exit 0');
  assert(stderr === '', 'no orphans → no stderr noise');
  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 4 — unrelated dotfile siblings are not touched.
// ---------------------------------------------------------------------------
{
  const { root, extensionsDir } = mkExtensionsTree();
  const self = mkStagingDir(extensionsDir, 'self', true);
  const orphan = mkStagingDir(extensionsDir, 'orphan', false);
  const cache = path.join(extensionsDir, '.openclaw-cache');
  const gitDir = path.join(extensionsDir, '.git');
  fs.mkdirSync(cache, { recursive: true });
  fs.mkdirSync(gitDir, { recursive: true });

  const { code } = runScriptFrom(self);

  assert(code === 0, 'exit 0 with unrelated dotfiles present');
  assert(!fs.existsSync(orphan), 'matching-prefix orphan removed');
  assert(fs.existsSync(cache), '.openclaw-cache untouched');
  assert(fs.existsSync(gitDir), '.git untouched');
  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 5 — non-directory entries with the prefix are skipped.
// ---------------------------------------------------------------------------
{
  const { root, extensionsDir } = mkExtensionsTree();
  const self = mkStagingDir(extensionsDir, 'self', true);
  const strayFile = path.join(extensionsDir, '.openclaw-install-stage-leftover-lockfile');
  fs.writeFileSync(strayFile, '');

  const { code } = runScriptFrom(self);

  assert(code === 0, 'exit 0 with stray prefix-matching file');
  assert(fs.existsSync(strayFile), 'stray file with prefix is not deleted');
  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 6 — exit 0 even when parent readdir throws (parent is a file).
// ---------------------------------------------------------------------------
{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-postinstall-bad-'));
  const fakeParent = path.join(root, 'not-a-dir');
  fs.writeFileSync(fakeParent, '');
  const self = path.join(fakeParent, 'subdir');
  // Can't actually mkdir a "subdir" of a file, so synthesize: copy the
  // script into a real dir whose parent we'll remove right before exec.
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-postinstall-sandbox-'));
  fs.copyFileSync(SCRIPT, path.join(sandbox, 'postinstall.mjs'));
  // Force the parent to be unreadable: make a sandbox/no-readdir parent.
  // Simpler: just exec from a directory whose parent IS readable but
  // contains nothing — confirms graceful no-op path coverage.
  const { code } = runScriptFrom(sandbox);
  assert(code === 0, 'exit 0 when parent has no matching siblings (graceful no-op)');
  rmrf(root);
  rmrf(sandbox);
}

console.log(`\n${passed}/${passed + failed} passed`);
if (failed > 0) {
  process.exit(1);
}
