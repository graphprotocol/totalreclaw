#!/usr/bin/env node
/**
 * check-scanner.mjs — Simulate OpenClaw's `env-harvesting` security-scanner
 * rule so we catch false-positives BEFORE publishing to ClawHub.
 *
 * Background (see docs/notes/INVESTIGATION-OPENCLAW-SCANNER-EXEMPTION-*):
 *   OpenClaw's built-in skill scanner refuses to install a plugin whose
 *   source has BOTH:
 *     - `process.env` somewhere in the file, AND
 *     - a case-insensitive word-boundary match for `fetch`, `post`, or
 *       `http.request` anywhere in the same file.
 *   The check is whole-file (per file), so even a comment containing the
 *   word "fetch" will trip the rule if the same file reads env vars. The
 *   intended architectural fix is to centralize all `process.env` reads
 *   into a single file (config.ts) that performs NO network work — but a
 *   stray comment like "// after the billing fetch completes" in that
 *   file is enough to re-trip the rule.
 *
 * This script walks every `.ts/.tsx/.js/.mjs/.cjs/.cts/.mts/.jsx` file
 * under skill/plugin/ (skipping node_modules, dist, hidden dirs) and
 * exits non-zero with a readable error if any file matches BOTH patterns.
 *
 * Per-file suppression is available via a `// scanner-sim: allow` comment
 * at the top of a file (top 3 lines). Prefer fixing the false-positive
 * over suppressing — suppression defeats the purpose of the check.
 *
 * Usage:
 *   node skill/scripts/check-scanner.mjs            # exit 0 clean, 1 on any flag
 *   node skill/scripts/check-scanner.mjs --json     # emit structured findings
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// skill/scripts/ -> skill/plugin/
const ROOT = path.resolve(__dirname, '..', 'plugin');

const SCANNABLE_EXT = new Set(['.ts', '.tsx', '.js', '.mjs', '.cjs', '.cts', '.mts', '.jsx']);
const SKIP_DIRS = new Set(['node_modules', 'dist', 'pkg', '.git', 'coverage']);

// Mirrors OpenClaw `skill-scanner-*.js` SOURCE_RULES[env-harvesting]:
//   pattern:         /process\.env/
//   requiresContext: /\bfetch\b|\bpost\b|http\.request/i
const ENV_PATTERN = /process\.env/;
const CONTEXT_PATTERN = /\bfetch\b|\bpost\b|http\.request/i;

const ALLOW_COMMENT = /^\s*(?:\/\/|\*|\/\*).*scanner-sim:\s*allow/i;

function isSuppressed(source) {
  const head = source.split('\n', 5).join('\n');
  return ALLOW_COMMENT.test(head);
}

function walk(dir) {
  const out = [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e of entries) {
    if (e.name.startsWith('.')) continue;
    if (SKIP_DIRS.has(e.name)) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...walk(p));
    else if (e.isFile() && SCANNABLE_EXT.has(path.extname(e.name))) out.push(p);
  }
  return out;
}

function firstLineMatching(lines, re) {
  for (let i = 0; i < lines.length; i++) if (re.test(lines[i])) return { line: i + 1, text: lines[i].trim() };
  return null;
}

function allLinesMatching(lines, re) {
  const out = [];
  for (let i = 0; i < lines.length; i++) if (re.test(lines[i])) out.push({ line: i + 1, text: lines[i].trim() });
  return out;
}

const findings = [];

if (!fs.existsSync(ROOT)) {
  console.error(`scanner-sim: plugin directory not found at ${ROOT}`);
  process.exit(2);
}

const files = walk(ROOT);
for (const absPath of files) {
  const relPath = path.relative(ROOT, absPath);
  const src = fs.readFileSync(absPath, 'utf8');
  if (!ENV_PATTERN.test(src)) continue;
  if (!CONTEXT_PATTERN.test(src)) continue;
  if (isSuppressed(src)) continue;
  const lines = src.split('\n');
  const envHit = firstLineMatching(lines, ENV_PATTERN);
  const triggerHits = allLinesMatching(lines, CONTEXT_PATTERN);
  findings.push({
    file: relPath,
    envLine: envHit?.line ?? 1,
    triggers: triggerHits.slice(0, 10),
  });
}

const jsonMode = process.argv.includes('--json');
if (jsonMode) {
  process.stdout.write(JSON.stringify({ root: ROOT, findings }, null, 2) + '\n');
  process.exit(findings.length ? 1 : 0);
}

if (findings.length === 0) {
  console.log(`scanner-sim: OK — ${files.length} files scanned, 0 env-harvesting flags under ${path.relative(process.cwd(), ROOT) || ROOT}`);
  process.exit(0);
}

console.error(`scanner-sim: FAIL — ${findings.length} file(s) would trip OpenClaw's env-harvesting rule`);
console.error('');
console.error('Each of these files reads process.env AND contains a case-insensitive');
console.error('match for \\bfetch\\b, \\bpost\\b, or http.request. OpenClaw will refuse');
console.error('to install such plugins. Fix by either:');
console.error('  1. Moving the env read into config.ts (centralize), OR');
console.error('  2. Rewording the trigger word (e.g., "fetch" -> "lookup") if it is');
console.error('     only in a comment, OR');
console.error('  3. Splitting the file so env and network live in separate files.');
console.error('');
for (const f of findings) {
  console.error(`  ${f.file}:${f.envLine}  first process.env read`);
  for (const t of f.triggers) {
    console.error(`    :${t.line}  trigger -> ${t.text.slice(0, 120)}`);
  }
}
console.error('');
console.error('Suppress a specific file (discouraged) with a top-of-file comment:');
console.error('  // scanner-sim: allow');
process.exit(1);
