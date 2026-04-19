#!/usr/bin/env node
/**
 * check-scanner.mjs — Simulate OpenClaw's security-scanner rules so we catch
 * false-positives BEFORE publishing to ClawHub.
 *
 * Background (see docs/notes/INVESTIGATION-OPENCLAW-SCANNER-EXEMPTION-*):
 *   OpenClaw's built-in skill scanner refuses to install a plugin whose
 *   source matches per-file rule patterns. We simulate TWO rules:
 *
 *   1. `env-harvesting` — a file contains BOTH:
 *        - `process.env` somewhere in the file, AND
 *        - a case-insensitive word-boundary match for `fetch`, `post`, or
 *          `http.request` anywhere in the same file.
 *      Fixed in 3.0.4/3.0.5 by centralizing all `process.env` reads into
 *      `config.ts`.
 *
 *   2. `potential-exfiltration` — a file contains BOTH:
 *        - an `fs.read*` call (`fs.readFileSync`, `fs.readFile`,
 *          `fs.promises.readFile`, or a bare `readFile(` from `fs/promises`),
 *          AND
 *        - a case-insensitive word-boundary match for `fetch`, `post`,
 *          `http.request`, `axios`, or `XMLHttpRequest` anywhere in the
 *          same file.
 *      Fixed in 3.0.7 by extracting `readBillingCache` / `writeBillingCache`
 *      into `billing-cache.ts` (no network-capable trigger markers allowed
 *      in that file).
 *
 *   Both checks are whole-file (per file), so even a comment containing one
 *   of the trigger words will trip the rule if the same file reads env or
 *   a disk file. The intended architectural fix is file-level isolation.
 *
 * This script walks every `.ts/.tsx/.js/.mjs/.cjs/.cts/.mts/.jsx` file
 * under skill/plugin/ (skipping node_modules, dist, hidden dirs) and
 * exits non-zero with a readable error if any file matches either rule.
 *
 * Per-file suppression is available via a `// scanner-sim: allow` comment
 * at the top of a file (top 5 lines). Prefer fixing the false-positive
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

// Mirrors OpenClaw SOURCE_RULES[potential-exfiltration]:
//   pattern:         fs.read* (sync, callback, or fs/promises forms)
//   requiresContext: /\bfetch\b|\bpost\b|http\.request|\baxios\b|\bXMLHttpRequest\b/i
// Trigger set intentionally wider than env-harvesting because axios and
// XMLHttpRequest also qualify as network-send for exfiltration risk.
const FS_READ_PATTERN = /fs\.readFileSync|fs\.readFile\b|fs\.promises\.readFile|\breadFile\s*\(/;
const EXFIL_CONTEXT_PATTERN = /\bfetch\b|\bpost\b|http\.request|\baxios\b|\bXMLHttpRequest\b/i;

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

const envFindings = [];
const exfilFindings = [];

if (!fs.existsSync(ROOT)) {
  console.error(`scanner-sim: plugin directory not found at ${ROOT}`);
  process.exit(2);
}

const files = walk(ROOT);
for (const absPath of files) {
  const relPath = path.relative(ROOT, absPath);
  const src = fs.readFileSync(absPath, 'utf8');
  if (isSuppressed(src)) continue;
  const lines = src.split('\n');

  // Rule 1 — env-harvesting
  if (ENV_PATTERN.test(src) && CONTEXT_PATTERN.test(src)) {
    const envHit = firstLineMatching(lines, ENV_PATTERN);
    const triggerHits = allLinesMatching(lines, CONTEXT_PATTERN);
    envFindings.push({
      file: relPath,
      envLine: envHit?.line ?? 1,
      triggers: triggerHits.slice(0, 10),
    });
  }

  // Rule 2 — potential-exfiltration
  if (FS_READ_PATTERN.test(src) && EXFIL_CONTEXT_PATTERN.test(src)) {
    const readHit = firstLineMatching(lines, FS_READ_PATTERN);
    const triggerHits = allLinesMatching(lines, EXFIL_CONTEXT_PATTERN);
    exfilFindings.push({
      file: relPath,
      readLine: readHit?.line ?? 1,
      triggers: triggerHits.slice(0, 10),
    });
  }
}

const totalFindings = envFindings.length + exfilFindings.length;
const jsonMode = process.argv.includes('--json');
if (jsonMode) {
  process.stdout.write(
    JSON.stringify(
      {
        root: ROOT,
        envHarvesting: envFindings,
        potentialExfiltration: exfilFindings,
      },
      null,
      2,
    ) + '\n',
  );
  process.exit(totalFindings ? 1 : 0);
}

if (totalFindings === 0) {
  console.log(
    `scanner-sim: OK — ${files.length} files scanned, 0 flags (env-harvesting + potential-exfiltration) under ${
      path.relative(process.cwd(), ROOT) || ROOT
    }`,
  );
  process.exit(0);
}

console.error(
  `scanner-sim: FAIL — ${envFindings.length} env-harvesting + ${exfilFindings.length} potential-exfiltration flag(s)`,
);
console.error('');

if (envFindings.length > 0) {
  console.error('[env-harvesting]');
  console.error('Each of these files reads process.env AND contains a case-insensitive');
  console.error('match for \\bfetch\\b, \\bpost\\b, or http.request. OpenClaw will refuse');
  console.error('to install such plugins. Fix by either:');
  console.error('  1. Moving the env read into config.ts (centralize), OR');
  console.error('  2. Rewording the trigger word (e.g., "fetch" -> "lookup") if it is');
  console.error('     only in a comment, OR');
  console.error('  3. Splitting the file so env and network live in separate files.');
  console.error('');
  for (const f of envFindings) {
    console.error(`  ${f.file}:${f.envLine}  first process.env read`);
    for (const t of f.triggers) {
      console.error(`    :${t.line}  trigger -> ${t.text.slice(0, 120)}`);
    }
  }
  console.error('');
}

if (exfilFindings.length > 0) {
  console.error('[potential-exfiltration]');
  console.error('Each of these files calls fs.read* AND contains a case-insensitive');
  console.error('match for \\bfetch\\b, \\bpost\\b, http.request, \\baxios\\b, or');
  console.error('\\bXMLHttpRequest\\b. OpenClaw will refuse to install such plugins.');
  console.error('Fix by either:');
  console.error('  1. Extracting the fs.read* into a dedicated module that has no');
  console.error('     outbound-request trigger markers (preferred), OR');
  console.error('  2. Rewording comment-only trigger words to synonyms (e.g., "fetch"');
  console.error('     -> "lookup"), OR');
  console.error('  3. Splitting the file so disk I/O and the request live separately.');
  console.error('');
  for (const f of exfilFindings) {
    console.error(`  ${f.file}:${f.readLine}  first fs.read* call`);
    for (const t of f.triggers) {
      console.error(`    :${t.line}  trigger -> ${t.text.slice(0, 120)}`);
    }
  }
  console.error('');
}

console.error('Suppress a specific file (discouraged) with a top-of-file comment:');
console.error('  // scanner-sim: allow');
process.exit(1);
