#!/usr/bin/env node
/**
 * check-url-binding.mjs — assert the artifact's default URL bindings match
 * the post-F-flip contract (3.3.12-rc.1).
 *
 * Hard invariant (3.3.12-rc.1 onward):
 *
 *   BOTH `release-type=stable` AND `release-type=rc` artifacts MUST default
 *   to `api.totalreclaw.xyz`. Any *default-binding site* (i.e. a literal
 *   like `'https://api-staging.totalreclaw.xyz'` or
 *   `'wss://api-staging.totalreclaw.xyz'` that appears as a fallback in
 *   shipped JS) is forbidden. Mentions of `api-staging.totalreclaw.xyz` in
 *   comments, help-text strings, and the banner copy that warns users
 *   about staging mode are allowed (they don't change the default).
 *
 * Rationale:
 *   Pre-flip, RC builds defaulted to staging and stable builds got a
 *   publish-time sed-rewrite to production. That stranded any user who
 *   picked `@rc` with their memories on a staging relay. Post-flip, the
 *   source already binds to production for both release types. Staging
 *   access is opt-in via TOTALRECLAW_SERVER_URL.
 *
 * What this guard checks:
 *   - skill.json `default` field MUST be `https://api.totalreclaw.xyz`.
 *   - No shipped JS file may contain a default URL literal of the form
 *     `'https://api-staging.totalreclaw.xyz'` or
 *     `'wss://api-staging.totalreclaw.xyz'` (matching string literals only).
 *   - The artifact MUST contain `api.totalreclaw.xyz` somewhere
 *     (proves the canonical default-URL site is intact).
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
const PLUGIN_ROOT = path.resolve(__dirname, '..', 'plugin');

const STAGING = 'api-staging.totalreclaw.xyz';
const PRODUCTION = 'api.totalreclaw.xyz';

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

const distRoot = path.join(PLUGIN_ROOT, 'dist');
const tree = fs.existsSync(distRoot) ? distRoot : PLUGIN_ROOT;
const treeKind = tree === distRoot ? 'dist' : 'source';

const SCANNABLE_EXT = new Set(['.js', '.cjs', '.mjs', '.d.ts']);
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

// ---- 1. skill.json default-field check ----
const skillJsonPath = path.join(PLUGIN_ROOT, 'skill.json');
let skillJsonOk = true;
const skillJsonErrors = [];
if (fs.existsSync(skillJsonPath)) {
  try {
    const sj = JSON.parse(fs.readFileSync(skillJsonPath, 'utf8'));
    const defaultUrl = sj?.openclaw?.config?.serverUrl?.default;
    if (typeof defaultUrl !== 'string') {
      skillJsonOk = false;
      skillJsonErrors.push('skill.json openclaw.config.serverUrl.default missing or not a string');
    } else if (defaultUrl !== `https://${PRODUCTION}`) {
      skillJsonOk = false;
      skillJsonErrors.push(
        `skill.json openclaw.config.serverUrl.default = "${defaultUrl}", expected "https://${PRODUCTION}"`,
      );
    }
  } catch (err) {
    skillJsonOk = false;
    skillJsonErrors.push(`skill.json parse error: ${err.message}`);
  }
}

// ---- 2. JS-default-binding check ----
// Match string literals that bind a staging URL: `'https://api-staging…'`
// or `"wss://api-staging…"` (single or double quotes, http or wss). Comments
// don't include quotes around the URL, so they don't match.
const STAGING_LITERAL_RE = /['"](https?|wss?):\/\/api-staging\.totalreclaw\.xyz[^'"]*['"]/;

let productionHits = 0;
const offendingFiles = [];
for (const f of files) {
  let src;
  try { src = fs.readFileSync(f, 'utf8'); } catch { continue; }
  if (src.includes(PRODUCTION)) productionHits++;
  if (STAGING_LITERAL_RE.test(src)) {
    offendingFiles.push(path.relative(PLUGIN_ROOT, f));
  }
}

const failHeader = `check-url-binding: FAIL (${mode} mode)`;
const okHeader = `check-url-binding: OK (${mode} mode)`;

let failed = false;

if (!skillJsonOk) {
  console.error(failHeader);
  for (const e of skillJsonErrors) console.error(`  ${e}`);
  failed = true;
}

if (offendingFiles.length > 0) {
  console.error(failHeader);
  console.error(`  Default-URL binding to staging found in ${offendingFiles.length} file(s):`);
  for (const f of offendingFiles) console.error(`    - ${f}`);
  console.error('');
  console.error('  Both stable and RC artifacts must default to production.');
  console.error('  Staging is opt-in via TOTALRECLAW_SERVER_URL env override.');
  failed = true;
}

if (productionHits === 0) {
  console.error(failHeader);
  console.error(`  production URL "${PRODUCTION}" not found anywhere.`);
  console.error('  Artifacts must reference api.totalreclaw.xyz at least once.');
  failed = true;
}

if (failed) process.exit(1);
console.log(
  `${okHeader} — ${productionHits} production hits, 0 staging default-binding sites across ${treeKind}/`,
);

if (process.argv.includes('--json')) {
  const summary = {
    mode,
    tree: treeKind,
    pluginRoot: PLUGIN_ROOT,
    productionHits,
    offendingFiles,
    skillJsonOk,
  };
  process.stdout.write(JSON.stringify(summary, null, 2) + '\n');
}
