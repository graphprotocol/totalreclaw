/**
 * Regression test for umbrella #182 finding F3 (issue #186) — plugin-load
 * manifests at `<pluginRoot>/.loaded.json` and `.error.json`.
 *
 * The agent inside the gateway uses these manifests to verify TR loaded
 * (and which tools bound) when `openclaw plugins list` is unavailable —
 * sibling F1 #184 (CLI hangs inside gateway).
 *
 * What this asserts
 * -----------------
 *   1. `writePluginLoadedManifest` writes valid JSON with `loadedAt`,
 *      `version`, and `tools` fields at `<pluginRoot>/.loaded.json`.
 *   2. `writePluginErrorManifest` writes valid JSON with `loadedAt`,
 *      `error`, and `stack` fields at `<pluginRoot>/.error.json`.
 *   3. `clearPluginManifests` removes both files when present, returns the
 *      removed-count, and is a no-op (returns 0) when neither is present.
 *   4. Both writers are best-effort: missing pluginRootDir → false return,
 *      no throw.
 *   5. The on-disk JSON for the loaded manifest is human-readable
 *      (pretty-printed with 2-space indent) so an agent can `cat | head`
 *      without piping through `jq`.
 *
 * Run with: `npx tsx test_issue_186_load_manifests.test.ts`
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  writePluginLoadedManifest,
  writePluginErrorManifest,
  clearPluginManifests,
  PLUGIN_LOADED_MANIFEST,
  PLUGIN_ERROR_MANIFEST,
} from './fs-helpers.js';

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
  return fs.mkdtempSync(path.join(os.tmpdir(), 'tr-issue-186-'));
}

function rmrf(dir: string): void {
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    // best-effort
  }
}

// ---------------------------------------------------------------------------
// Test 1 — writePluginLoadedManifest happy path.
// ---------------------------------------------------------------------------
{
  const root = mkTmpRoot();
  const ok = writePluginLoadedManifest(root, {
    loadedAt: 1700000000000,
    version: '3.3.2-rc.1',
    tools: ['totalreclaw_pair', 'totalreclaw_remember', 'totalreclaw_recall'],
  });
  assert(ok === true, 'writePluginLoadedManifest returns true on success');

  const file = path.join(root, PLUGIN_LOADED_MANIFEST);
  assert(fs.existsSync(file), `.loaded.json written at ${file}`);

  const raw = fs.readFileSync(file, 'utf-8');
  const parsed = JSON.parse(raw) as { loadedAt: number; version: string; tools: string[] };
  assert(parsed.loadedAt === 1700000000000, 'loadedAt round-trips');
  assert(parsed.version === '3.3.2-rc.1', 'version round-trips');
  assert(parsed.tools.length === 3, 'tools array preserved');
  assert(parsed.tools[0] === 'totalreclaw_pair', 'first tool name preserved');

  // Human-readable: pretty-printed JSON has at least one newline.
  assert(raw.includes('\n'), 'JSON is pretty-printed (has newlines)');

  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 2 — writePluginErrorManifest happy path.
// ---------------------------------------------------------------------------
{
  const root = mkTmpRoot();
  const ok = writePluginErrorManifest(root, {
    loadedAt: 1700000001000,
    error: 'register failed: bad config',
    stack: 'Error: register failed\n    at register (index.ts:42)',
  });
  assert(ok === true, 'writePluginErrorManifest returns true on success');

  const file = path.join(root, PLUGIN_ERROR_MANIFEST);
  assert(fs.existsSync(file), `.error.json written at ${file}`);

  const parsed = JSON.parse(fs.readFileSync(file, 'utf-8')) as {
    loadedAt: number;
    error: string;
    stack: string;
  };
  assert(parsed.loadedAt === 1700000001000, 'loadedAt round-trips');
  assert(parsed.error === 'register failed: bad config', 'error message round-trips');
  assert(parsed.stack.startsWith('Error:'), 'stack round-trips');

  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 3 — clearPluginManifests removes both files when present.
// ---------------------------------------------------------------------------
{
  const root = mkTmpRoot();
  fs.writeFileSync(path.join(root, PLUGIN_LOADED_MANIFEST), '{}');
  fs.writeFileSync(path.join(root, PLUGIN_ERROR_MANIFEST), '{}');

  const removed = clearPluginManifests(root);
  assert(removed === 2, 'clearPluginManifests reports 2 removed');
  assert(!fs.existsSync(path.join(root, PLUGIN_LOADED_MANIFEST)), '.loaded.json deleted');
  assert(!fs.existsSync(path.join(root, PLUGIN_ERROR_MANIFEST)), '.error.json deleted');

  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 4 — clearPluginManifests no-op on a clean dir.
// ---------------------------------------------------------------------------
{
  const root = mkTmpRoot();
  const removed = clearPluginManifests(root);
  assert(removed === 0, 'clearPluginManifests on clean dir reports 0 removed');
  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 5 — writers return false when pluginRootDir does not exist (no throw).
// ---------------------------------------------------------------------------
{
  const missing = path.join(os.tmpdir(), 'tr-issue-186-does-not-exist-' + Date.now());
  const okLoaded = writePluginLoadedManifest(missing, {
    loadedAt: 0,
    version: null,
    tools: [],
  });
  assert(okLoaded === false, 'writePluginLoadedManifest on missing dir returns false (no throw)');

  const okError = writePluginErrorManifest(missing, {
    loadedAt: 0,
    error: 'x',
    stack: '',
  });
  assert(okError === false, 'writePluginErrorManifest on missing dir returns false (no throw)');
}

// ---------------------------------------------------------------------------
// Test 6 — version=null is preserved (package.json read can fail).
// ---------------------------------------------------------------------------
{
  const root = mkTmpRoot();
  writePluginLoadedManifest(root, {
    loadedAt: 1700000002000,
    version: null,
    tools: ['totalreclaw_remember'],
  });
  const parsed = JSON.parse(
    fs.readFileSync(path.join(root, PLUGIN_LOADED_MANIFEST), 'utf-8'),
  ) as { version: string | null };
  assert(parsed.version === null, 'version=null is preserved as JSON null');
  rmrf(root);
}

// ---------------------------------------------------------------------------
// Test 7 — empty tools array round-trips.
// ---------------------------------------------------------------------------
{
  const root = mkTmpRoot();
  writePluginLoadedManifest(root, {
    loadedAt: 1700000003000,
    version: '3.3.2-rc.1',
    tools: [],
  });
  const parsed = JSON.parse(
    fs.readFileSync(path.join(root, PLUGIN_LOADED_MANIFEST), 'utf-8'),
  ) as { tools: string[] };
  assert(Array.isArray(parsed.tools) && parsed.tools.length === 0, 'empty tools array preserved');
  rmrf(root);
}

// ---------------------------------------------------------------------------
console.log(`\n${passed}/${passed + failed} passed`);
if (failed > 0) {
  process.exit(1);
}
