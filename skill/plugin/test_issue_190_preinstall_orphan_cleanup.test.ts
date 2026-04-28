// scanner-sim: allow — test-only file (excluded from published tarball), child_process used to spawn the preinstall.mjs script under controlled HOME / cwd. Not shipped.
/**
 * Regression test for issue #190 (umbrella #182 finding F6) — `preinstall.mjs`
 * cleans orphan `.openclaw-install-stage-*` siblings before the new install
 * completes, preventing the gateway's duplicate-plugin-id warning that
 * blocks plugin registration.
 *
 * Why this matters
 * ----------------
 * The rc.21 fix (#126/#134) added `cleanupInstallStagingDirs` called at
 * register-time from index.ts. That helper is too late for the re-install
 * scenario: with an orphan staging dir present, OpenClaw's config validator
 * fires `duplicate plugin id detected; global plugin will be overridden by
 * global plugin` BEFORE plugin register, so register never runs and the
 * helper never gets a chance to clean up. The plugin completely fails to
 * load.
 *
 * The fix runs the same cleanup logic at npm preinstall time (before the
 * gateway can scan + warn), via a dedicated `preinstall.mjs` script.
 *
 * Test strategy: spawn the actual `preinstall.mjs` as a child process with
 * HOME pointed at a tmp dir, cwd set to a fake "new staging dir," and
 * assert that:
 *   1. orphan stage dirs in extensions/ are removed
 *   2. the staging dir matching cwd's basename survives (we must never
 *      delete ourselves)
 *   3. the real `totalreclaw/` install dir survives
 *   4. the `.tr-partial-install` marker is dropped in cwd
 *   5. when no extensions dir exists at all, the script still exits 0
 *
 * Run with: `npx tsx test_issue_190_preinstall_orphan_cleanup.test.ts`
 */

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PREINSTALL_SCRIPT = path.resolve(__dirname, 'preinstall.mjs');

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

function mkSandbox(): { home: string; extensionsDir: string } {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-preinstall-'));
  const extensionsDir = path.join(home, '.openclaw', 'extensions');
  fs.mkdirSync(extensionsDir, { recursive: true });
  return { home, extensionsDir };
}

function mkRealPlugin(extensionsDir: string): string {
  const pluginRoot = path.join(extensionsDir, 'totalreclaw');
  fs.mkdirSync(path.join(pluginRoot, 'dist'), { recursive: true });
  fs.writeFileSync(path.join(pluginRoot, 'package.json'), JSON.stringify({ name: '@totalreclaw/totalreclaw' }));
  fs.writeFileSync(path.join(pluginRoot, 'dist', 'index.js'), '// real plugin');
  return pluginRoot;
}

function mkOrphanStaging(extensionsDir: string, suffix: string): string {
  const dir = path.join(extensionsDir, `.openclaw-install-stage-${suffix}`);
  fs.mkdirSync(path.join(dir, 'dist'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name: '@totalreclaw/totalreclaw' }));
  fs.writeFileSync(path.join(dir, 'dist', 'index.js'), '// orphan');
  return dir;
}

function runPreinstall(cwd: string, home: string, extra: Record<string, string> = {}): { status: number; stdout: string; stderr: string } {
  const env = { ...process.env, HOME: home, ...extra };
  delete (env as Record<string, string | undefined>).OPENCLAW_STATE_DIR;
  Object.assign(env, extra);
  const r = spawnSync('node', [PREINSTALL_SCRIPT], { cwd, env, encoding: 'utf8' });
  return { status: r.status ?? -1, stdout: r.stdout, stderr: r.stderr };
}

// ---------------------------------------------------------------------------
// Test 1 — newer-OpenClaw flow (cwd in /tmp).
// Orphan stage dirs in ~/.openclaw/extensions/ get cleaned. Real plugin survives.
// ---------------------------------------------------------------------------
{
  const { home, extensionsDir } = mkSandbox();
  const realPlugin = mkRealPlugin(extensionsDir);
  const orphan1 = mkOrphanStaging(extensionsDir, 'aaa111');
  const orphan2 = mkOrphanStaging(extensionsDir, 'bbb222');

  // Newer OpenClaw stages in /tmp/openclaw-npm-pack-XXX/ — emulate that.
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'openclaw-npm-pack-'));

  const r = runPreinstall(cwd, home);
  assert(r.status === 0, 'preinstall exits 0 (newer OpenClaw flow)');
  assert(!fs.existsSync(orphan1), 'orphan #1 in extensions/ deleted');
  assert(!fs.existsSync(orphan2), 'orphan #2 in extensions/ deleted');
  assert(fs.existsSync(realPlugin), 'real totalreclaw/ install survives');
  assert(fs.existsSync(path.join(cwd, '.tr-partial-install')), 'partial-install marker dropped in cwd');

  rmrf(home);
  rmrf(cwd);
}

// ---------------------------------------------------------------------------
// Test 2 — older-OpenClaw flow (cwd inside extensions/).
// Self-staging dir survives (script must skip the dir it lives in).
// ---------------------------------------------------------------------------
{
  const { home, extensionsDir } = mkSandbox();
  const realPlugin = mkRealPlugin(extensionsDir);
  const orphan = mkOrphanStaging(extensionsDir, 'oldA');
  const selfStaging = mkOrphanStaging(extensionsDir, 'selfB');

  const r = runPreinstall(selfStaging, home);
  assert(r.status === 0, 'preinstall exits 0 (older OpenClaw flow)');
  assert(!fs.existsSync(orphan), 'orphan sibling deleted');
  assert(fs.existsSync(selfStaging), 'self-staging dir SURVIVES (must not delete cwd)');
  assert(fs.existsSync(realPlugin), 'real totalreclaw/ install survives');
  assert(fs.existsSync(path.join(selfStaging, '.tr-partial-install')), 'partial-install marker dropped in cwd');

  rmrf(home);
}

// ---------------------------------------------------------------------------
// Test 3 — OPENCLAW_STATE_DIR env override picks an alternate extensions dir.
// ---------------------------------------------------------------------------
{
  const { home } = mkSandbox();
  const altState = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-altstate-'));
  const altExtensions = path.join(altState, 'extensions');
  fs.mkdirSync(altExtensions, { recursive: true });
  const altOrphan = mkOrphanStaging(altExtensions, 'altC');
  const altReal = mkRealPlugin(altExtensions);

  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'openclaw-npm-pack-'));
  const r = runPreinstall(cwd, home, { OPENCLAW_STATE_DIR: altState });
  assert(r.status === 0, 'preinstall exits 0 with OPENCLAW_STATE_DIR set');
  assert(!fs.existsSync(altOrphan), 'orphan in $OPENCLAW_STATE_DIR/extensions/ deleted');
  assert(fs.existsSync(altReal), 'real plugin in $OPENCLAW_STATE_DIR/extensions/ survives');

  rmrf(home);
  rmrf(altState);
  rmrf(cwd);
}

// ---------------------------------------------------------------------------
// Test 4 — no extensions dir exists at all (e.g. CI build of the tarball).
// Script must still exit 0 and drop the marker.
// ---------------------------------------------------------------------------
{
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-no-ext-'));
  // Deliberately do NOT create ~/.openclaw/extensions/.
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'openclaw-npm-pack-'));

  const r = runPreinstall(cwd, home);
  assert(r.status === 0, 'preinstall exits 0 when no extensions dir exists');
  assert(fs.existsSync(path.join(cwd, '.tr-partial-install')), 'marker dropped even without extensions dir');

  rmrf(home);
  rmrf(cwd);
}

// ---------------------------------------------------------------------------
// Test 5 — non-stage-prefixed entries (other plugins, dotfiles) are NEVER
// touched. Only `.openclaw-install-stage-*` siblings get cleaned.
// ---------------------------------------------------------------------------
{
  const { home, extensionsDir } = mkSandbox();
  mkRealPlugin(extensionsDir);

  // Drop a sibling unrelated plugin and a dotfile-prefixed cache dir.
  const otherPlugin = path.join(extensionsDir, 'someone-else-plugin');
  fs.mkdirSync(otherPlugin, { recursive: true });
  fs.writeFileSync(path.join(otherPlugin, 'package.json'), JSON.stringify({ name: 'someone-else-plugin' }));
  const dotfileDir = path.join(extensionsDir, '.openclaw-cache');
  fs.mkdirSync(dotfileDir, { recursive: true });

  const orphan = mkOrphanStaging(extensionsDir, 'orphZ');
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'openclaw-npm-pack-'));

  const r = runPreinstall(cwd, home);
  assert(r.status === 0, 'preinstall exits 0');
  assert(!fs.existsSync(orphan), 'orphan stage dir deleted');
  assert(fs.existsSync(otherPlugin), 'unrelated plugin dir untouched');
  assert(fs.existsSync(dotfileDir), 'unrelated dotfile sibling (.openclaw-cache) untouched');

  rmrf(home);
  rmrf(cwd);
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
console.log(`\n${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('\nSOME TESTS FAILED');
  process.exit(1);
}
console.log('\nALL TESTS PASSED');
