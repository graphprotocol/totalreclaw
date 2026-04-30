#!/usr/bin/env node
/**
 * check-url-binding.mjs — assert the artifact's bundled URL defaults
 * match the release-type contract codified in PR #165.
 *
 * Hard invariant (3.3.3-rc.1 onward):
 *
 *   release-type=stable artifacts MUST contain `api.totalreclaw.xyz`
 *   AND MUST NOT contain `api-staging.totalreclaw.xyz`.
 *
 *   release-type=rc artifacts MUST contain `api-staging.totalreclaw.xyz`
 *   (production URL is allowed in comments / fallback strings, but the
 *   default-server-URL site MUST be staging).
 *
 * Source-of-truth in `skill/plugin/{config.ts, index.ts, subgraph-store.ts,
 * skill.json}` references `api-staging.totalreclaw.xyz` everywhere. The
 * publish workflow's "Bind stable artifacts to production URLs" step
 * sed-replaces that string -> `api.totalreclaw.xyz` for stable releases.
 *
 * Modes:
 *   - `--release-type=rc` (default): assert RC invariants
 *   - `--release-type=stable`: assert stable invariants
 *
 * Run targets the BUILT dist/ tree (the artifact users actually receive)
 * plus skill.json. Falls back to the source tree when `dist/` is absent
 * (so `prepublishOnly` works the same way `check-scanner.mjs` does).
 *
 * Exit codes:
 *   0 — invariants hold
 *   1 — invariant violation
 *   2 — usage error
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// skill/scripts/ -> skill/plugin/
const PLUGIN_ROOT = path.resolve(__dirname, '..', 'plugin');

const STAGING = 'api-staging.totalreclaw.xyz';
const PRODUCTION = 'api.totalreclaw.xyz';

// Parse args.
let mode = 'rc';
for (let i = 2; i < process.argv.length; i++) {
  const a = process.argv[i];
  if (a === '--release-type') {
    mode = process.argv[i + 1] || 'rc';
    i++;
  } else if (a.startsWith('--release-type=')) {
    mode = a.slice('--release-type='.length);
  }
}
if (mode !== 'rc' && mode !== 'stable') {
  console.error(`check-url-binding: invalid --release-type "${mode}" (must be rc | stable)`);
  process.exit(2);
}

// Pick the artifact tree. Prefer dist/ (the actual published tarball
// contents). When dist/ is absent (rare local invocation), fall back to
// source so prepublishOnly still has a useful signal.
const distRoot = path.join(PLUGIN_ROOT, 'dist');
const tree = fs.existsSync(distRoot) ? distRoot : PLUGIN_ROOT;
const treeKind = tree === distRoot ? 'dist' : 'source';

// Files to scan.
const SCANNABLE_EXT = new Set([
  '.js', '.cjs', '.mjs',
  '.d.ts',
  '.json',
]);
const SKIP_DIRS = new Set(['node_modules', '.git', 'coverage']);

function walk(dir, out = []) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e of entries) {
    if (SKIP_DIRS.has(e.name)) continue;
    if (e.name.startsWith('.') && e.name !== '.') continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (e.isFile() && SCANNABLE_EXT.has(path.extname(e.name))) out.push(p);
  }
  return out;
}

const files = walk(tree);
// skill.json lives at PLUGIN_ROOT (not under dist), so include it explicitly
// when scanning dist.
if (treeKind === 'dist') {
  const skillJson = path.join(PLUGIN_ROOT, 'skill.json');
  if (fs.existsSync(skillJson)) files.push(skillJson);
}

let stagingHits = 0;
let productionHits = 0;
const stagingFiles = [];
const productionFiles = [];

for (const f of files) {
  let src;
  try {
    src = fs.readFileSync(f, 'utf8');
  } catch {
    continue;
  }
  if (src.includes(STAGING)) {
    stagingHits++;
    stagingFiles.push(path.relative(PLUGIN_ROOT, f));
  }
  if (src.includes(PRODUCTION)) {
    productionHits++;
    productionFiles.push(path.relative(PLUGIN_ROOT, f));
  }
}

const summary = {
  mode,
  tree: treeKind,
  pluginRoot: PLUGIN_ROOT,
  stagingHits,
  productionHits,
  stagingFiles,
  productionFiles,
};

if (mode === 'stable') {
  // Stable invariants:
  //   - MUST contain api.totalreclaw.xyz somewhere (proves the bind ran).
  //   - MUST NOT contain api-staging.totalreclaw.xyz anywhere.
  if (stagingHits > 0) {
    console.error('check-url-binding: FAIL (stable mode)');
    console.error(`  staging URL "${STAGING}" found in ${stagingHits} file(s):`);
    for (const f of stagingFiles) console.error(`    - ${f}`);
    console.error('');
    console.error('  Stable artifacts must NOT contain the staging URL. The');
    console.error('  publish workflow\'s "Bind stable artifacts to production');
    console.error('  URLs" step should have sed-replaced staging -> production');
    console.error('  before reaching this guard.');
    process.exit(1);
  }
  if (productionHits === 0) {
    console.error('check-url-binding: FAIL (stable mode)');
    console.error(`  production URL "${PRODUCTION}" not found anywhere.`);
    console.error('  Stable artifacts must reference api.totalreclaw.xyz at');
    console.error('  least once (the canonical default-server-URL site).');
    process.exit(1);
  }
  console.log(
    `check-url-binding: OK (stable mode) — ${productionHits} production hits, 0 staging hits across ${treeKind}/`,
  );
} else {
  // RC invariants:
  //   - MUST contain api-staging.totalreclaw.xyz somewhere (proves the
  //     source default is intact).
  if (stagingHits === 0) {
    console.error('check-url-binding: FAIL (rc mode)');
    console.error(`  staging URL "${STAGING}" not found anywhere.`);
    console.error('  RC artifacts must reference api-staging.totalreclaw.xyz');
    console.error('  at least once (the canonical RC default-server-URL site).');
    console.error('  If you intended to publish a stable artifact, pass');
    console.error('  --release-type=stable.');
    process.exit(1);
  }
  console.log(
    `check-url-binding: OK (rc mode) — ${stagingHits} staging hits across ${treeKind}/`,
  );
}

if (process.argv.includes('--json')) {
  process.stdout.write(JSON.stringify(summary, null, 2) + '\n');
}
