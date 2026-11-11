#!/usr/bin/env node
/**
 * check-url-binding.cjs: assert the MCP server's default relay URL bindings
 * match the environment-binding contract before a publish.
 *
 * Contract (the "F flip", public PR #198, 2026-05-07; same as the OpenClaw
 * plugin's skill/scripts/check-url-binding.mjs and the Python client's
 * publish-time wheel guard):
 *
 *   BOTH `release-type=stable` AND `release-type=rc` artifacts default to
 *   production (`api.totalreclaw.xyz`). Staging is opt-in at runtime via
 *   `TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz`. RCs used to
 *   default to staging, which stranded anyone who installed `@rc` with their
 *   memories on a relay that has no SLA and may be wiped.
 *
 * Checks (identical for both release types):
 *   1. No shipped file binds a staging URL as a string literal
 *      (`'https://api-staging...'`, `"wss://api-staging..."`, or a template
 *      literal). Mentions in comments are not quoted, so they pass.
 *   2. Every canonical default-URL site still carries the quoted production
 *      literal. Catches a rewrite that swapped the default for staging, a
 *      localhost URL, or anything else.
 *
 * Usage:
 *   node scripts/check-url-binding.cjs [--release-type=rc|stable] [--root=<dir>]
 *
 * `--release-type` falls back to $TOTALRECLAW_RELEASE_TYPE, then `stable`.
 * The tree scanned is `<root>/dist` when it exists, otherwise `<root>/src`.
 *
 * Exit codes: 0 invariants hold, 1 violation, 2 usage error.
 *
 * Addresses p-diogo/totalreclaw-internal#673 (the MCP publish job had no URL
 * guard, unlike the plugin and Python jobs).
 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');

const PRODUCTION_HOST = 'api.totalreclaw.xyz';

// Quoted staging URL in any quote style, http(s) or ws(s).
const STAGING_LITERAL_RE = /['"`](?:https?|wss?):\/\/api-staging\.totalreclaw\.xyz[^'"`]*['"`]/;
// Quoted production URL, http(s) or ws(s), host exactly api.totalreclaw.xyz.
const PRODUCTION_LITERAL_RE = /['"`](?:https?|wss?):\/\/api\.totalreclaw\.xyz\/?['"`]/;

// Files that hold a default relay URL. Paths are relative to the tree root
// and given without extension; `.ts` (src) or `.js` (dist) is appended.
const CANONICAL_SITES = [
  'index', // SERVER_URL fallback
  'cli/setup', // DEFAULT_SERVER_URL
  'subgraph/store', // relayUrl fallback
  'tools/pair', // DEFAULT_RELAY_URL_WSS + pair serverUrl fallback
  'pair-remote-client', // DEFAULT_RELAY_URL
];

const SCANNABLE_EXT = new Set(['.js', '.cjs', '.mjs', '.ts']);
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
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (e.isFile() && SCANNABLE_EXT.has(path.extname(e.name)) && !e.name.endsWith('.map')) {
      out.push(p);
    }
  }
  return out;
}

/** Strip `//` line comments and `/* *\/` block comments (approximate, good enough for URL literals). */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:'"`\\])\/\/[^\n]*/g, '$1');
}

/**
 * Check a package root. Returns { ok, releaseType, tree, treeKind, errors, stagingSites, missingSites }.
 */
function checkUrlBinding({ root, releaseType = 'stable' }) {
  if (releaseType !== 'rc' && releaseType !== 'stable') {
    throw new Error(`invalid release type "${releaseType}" (must be rc | stable)`);
  }
  const distRoot = path.join(root, 'dist');
  const treeKind = fs.existsSync(distRoot) ? 'dist' : 'src';
  const tree = path.join(root, treeKind);
  const ext = treeKind === 'dist' ? '.js' : '.ts';
  const errors = [];

  if (!fs.existsSync(tree)) {
    errors.push(`no ${treeKind}/ directory under ${root}`);
    return { ok: false, releaseType, tree, treeKind, errors, stagingSites: [], missingSites: [] };
  }

  const stagingSites = [];
  for (const f of walk(tree)) {
    if (f.endsWith('.d.ts')) continue; // type declarations never bind a runtime default
    const code = stripComments(fs.readFileSync(f, 'utf8'));
    const lines = code.split('\n');
    lines.forEach((line, i) => {
      if (STAGING_LITERAL_RE.test(line)) {
        stagingSites.push(`${path.relative(root, f)}:${i + 1}: ${line.trim()}`);
      }
    });
  }
  if (stagingSites.length > 0) {
    errors.push(
      `staging URL bound as a default in ${stagingSites.length} place(s):\n` +
        stagingSites.map((s) => `    - ${s}`).join('\n'),
    );
  }

  const missingSites = [];
  for (const site of CANONICAL_SITES) {
    const f = path.join(tree, site + ext);
    if (!fs.existsSync(f)) {
      missingSites.push(`${path.relative(root, f)} (file missing)`);
      continue;
    }
    const code = stripComments(fs.readFileSync(f, 'utf8'));
    if (!PRODUCTION_LITERAL_RE.test(code)) {
      missingSites.push(`${path.relative(root, f)} (no quoted ${PRODUCTION_HOST} default)`);
    }
  }
  if (missingSites.length > 0) {
    errors.push(
      `canonical default-URL site(s) lost the production literal:\n` +
        missingSites.map((s) => `    - ${s}`).join('\n'),
    );
  }

  return { ok: errors.length === 0, releaseType, tree, treeKind, errors, stagingSites, missingSites };
}

function parseArgs(argv, env) {
  let releaseType = env.TOTALRECLAW_RELEASE_TYPE || 'stable';
  let root = path.resolve(__dirname, '..');
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--release-type') releaseType = argv[++i];
    else if (a.startsWith('--release-type=')) releaseType = a.slice('--release-type='.length);
    else if (a === '--root') root = path.resolve(argv[++i]);
    else if (a.startsWith('--root=')) root = path.resolve(a.slice('--root='.length));
    else throw new Error(`unknown argument "${a}"`);
  }
  return { releaseType, root };
}

if (require.main === module) {
  let args;
  try {
    args = parseArgs(process.argv.slice(2), process.env);
    if (args.releaseType !== 'rc' && args.releaseType !== 'stable') {
      throw new Error(`invalid --release-type "${args.releaseType}" (must be rc | stable)`);
    }
  } catch (err) {
    console.error(`check-url-binding: ${err.message}`);
    process.exit(2);
  }
  const r = checkUrlBinding(args);
  if (!r.ok) {
    console.error(`check-url-binding: FAIL (${r.releaseType}, ${r.treeKind}/)`);
    for (const e of r.errors) console.error(`  ${e}`);
    console.error('');
    console.error('  Both stable and RC artifacts must default to https://api.totalreclaw.xyz.');
    console.error('  Staging is opt-in via the TOTALRECLAW_SERVER_URL env override.');
    process.exit(1);
  }
  console.log(
    `check-url-binding: OK (${r.releaseType}, ${r.treeKind}/): ${CANONICAL_SITES.length} default sites bind production, 0 staging defaults`,
  );
}

module.exports = { checkUrlBinding, parseArgs, CANONICAL_SITES };
