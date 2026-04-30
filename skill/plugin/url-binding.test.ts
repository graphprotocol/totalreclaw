// scanner-sim: allow — *.test.ts files are excluded from the npm tarball (see "files" array in package.json) and never reach the OpenClaw runtime sandbox. This test invokes check-url-binding.mjs as a subprocess; the child_process import is test-infrastructure only and not shipped.
/**
 * Regression test for the env-binding contract codified in PR #165 +
 * implemented in 3.3.3-rc.1.
 *
 * Hard invariants:
 *   - Source default for `TOTALRECLAW_SERVER_URL` is `api-staging.totalreclaw.xyz`.
 *   - `release-type=rc` artifacts ship the staging URL.
 *   - `release-type=stable` artifacts ship the production URL (workflow-side
 *     sed-replace `api-staging.totalreclaw.xyz` -> `api.totalreclaw.xyz`).
 *
 * The `check-url-binding.mjs` guard is the single fail-fast gate that
 * blocks misconfigured artifacts from reaching the registry. This test
 * exercises both modes against a synthetic artifact tree and confirms
 * the guard passes / fails as designed.
 *
 * Run with: `npx tsx url-binding.test.ts`
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.resolve(here, '..', 'scripts', 'check-url-binding.mjs');

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

interface RunResult {
  status: number;
  stdout: string;
  stderr: string;
}

/**
 * Run `check-url-binding.mjs --release-type=<mode>` against a synthetic
 * skill/plugin tree at `pluginRoot`. The script resolves PLUGIN_ROOT from
 * its own location (skill/scripts -> skill/plugin), so we have to stage a
 * matching skill/scripts symlink + a skill/plugin/dist tree.
 */
function runGuard(pluginRoot: string, mode: 'rc' | 'stable'): RunResult {
  // Stage a parallel skill/scripts dir that points at the real script,
  // so the script's PLUGIN_ROOT resolution lands on `pluginRoot`.
  const stagedScripts = path.join(pluginRoot, '..', 'scripts');
  fs.mkdirSync(stagedScripts, { recursive: true });
  const scriptCopy = path.join(stagedScripts, 'check-url-binding.mjs');
  if (!fs.existsSync(scriptCopy)) fs.copyFileSync(SCRIPT, scriptCopy);
  try {
    const out = execFileSync('node', [scriptCopy, `--release-type=${mode}`], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    return { status: 0, stdout: out, stderr: '' };
  } catch (err) {
    const e = err as { status?: number; stdout?: Buffer | string; stderr?: Buffer | string };
    return {
      stdout: typeof e.stdout === 'string' ? e.stdout : (e.stdout?.toString() ?? ''),
      stderr: typeof e.stderr === 'string' ? e.stderr : (e.stderr?.toString() ?? ''),
      status: e.status ?? -1,
    };
  }
}

function stagePluginTree(opts: { distContent: string; skillJson?: string }): {
  pluginRoot: string;
  cleanup: () => void;
} {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-url-binding-'));
  const pluginRoot = path.join(tmp, 'skill', 'plugin');
  fs.mkdirSync(path.join(pluginRoot, 'dist'), { recursive: true });
  fs.writeFileSync(path.join(pluginRoot, 'dist', 'config.js'), opts.distContent);
  fs.writeFileSync(
    path.join(pluginRoot, 'skill.json'),
    opts.skillJson ?? '{"version": "test", "default": "https://api-staging.totalreclaw.xyz"}',
  );
  return { pluginRoot, cleanup: () => rmrf(tmp) };
}

// ---------------------------------------------------------------------------
// Test 1 — RC artifact with staging URL passes RC guard
// ---------------------------------------------------------------------------
{
  const { pluginRoot, cleanup } = stagePluginTree({
    distContent: `export const SERVER_URL = 'https://api-staging.totalreclaw.xyz';\n`,
  });
  const r = runGuard(pluginRoot, 'rc');
  assert(r.status === 0, 'rc guard passes when artifact contains api-staging URL');
  assert(r.stdout.includes('OK (rc mode)'), 'rc OK message in stdout');
  cleanup();
}

// ---------------------------------------------------------------------------
// Test 2 — RC artifact missing staging URL FAILS RC guard
// ---------------------------------------------------------------------------
{
  const { pluginRoot, cleanup } = stagePluginTree({
    distContent: `export const SERVER_URL = 'https://api.totalreclaw.xyz';\n`,
    skillJson: '{"version": "test", "default": "https://api.totalreclaw.xyz"}',
  });
  const r = runGuard(pluginRoot, 'rc');
  assert(r.status === 1, 'rc guard fails when artifact is missing api-staging URL');
  assert(r.stderr.includes('FAIL (rc mode)'), 'rc FAIL message in stderr');
  cleanup();
}

// ---------------------------------------------------------------------------
// Test 3 — stable artifact with production URL passes stable guard
// ---------------------------------------------------------------------------
{
  const { pluginRoot, cleanup } = stagePluginTree({
    distContent: `export const SERVER_URL = 'https://api.totalreclaw.xyz';\n`,
    skillJson: '{"version": "test", "default": "https://api.totalreclaw.xyz"}',
  });
  const r = runGuard(pluginRoot, 'stable');
  assert(r.status === 0, 'stable guard passes when artifact contains api production URL only');
  assert(r.stdout.includes('OK (stable mode)'), 'stable OK message in stdout');
  cleanup();
}

// ---------------------------------------------------------------------------
// Test 4 — stable artifact still containing staging URL FAILS stable guard
// ---------------------------------------------------------------------------
{
  const { pluginRoot, cleanup } = stagePluginTree({
    distContent: `export const SERVER_URL = 'https://api-staging.totalreclaw.xyz';\n`,
    skillJson: '{"version": "test", "default": "https://api.totalreclaw.xyz"}',
  });
  const r = runGuard(pluginRoot, 'stable');
  assert(r.status === 1, 'stable guard fails when artifact still contains api-staging URL');
  assert(r.stderr.includes('FAIL (stable mode)'), 'stable FAIL message in stderr');
  assert(
    r.stderr.includes('staging URL'),
    'stable FAIL message names the offending URL',
  );
  cleanup();
}

// ---------------------------------------------------------------------------
// Test 5 — stable artifact with NEITHER URL FAILS stable guard
// ---------------------------------------------------------------------------
{
  const { pluginRoot, cleanup } = stagePluginTree({
    distContent: `export const NOTHING = 1;\n`,
    skillJson: '{"version": "test"}',
  });
  const r = runGuard(pluginRoot, 'stable');
  assert(r.status === 1, 'stable guard fails when artifact has no api.totalreclaw.xyz');
  assert(r.stderr.includes('FAIL (stable mode)'), 'stable FAIL message in stderr');
  cleanup();
}

// ---------------------------------------------------------------------------
console.log(`\n${passed}/${passed + failed} passed`);
if (failed > 0) process.exit(1);
