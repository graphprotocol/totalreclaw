#!/usr/bin/env node
/**
 * check-scanner.mjs — Simulate OpenClaw's security-scanner rules so we catch
 * false-positives BEFORE publishing to ClawHub.
 *
 * Background (see docs/notes/INVESTIGATION-OPENCLAW-SCANNER-EXEMPTION-*
 * and internal#21 QA report for 3.3.0-rc.1 NO-GO):
 *   OpenClaw's built-in skill scanner refuses to install a plugin whose
 *   source matches per-file rule patterns. We simulate the rules known to
 *   have blocked prior releases:
 *
 *   1. `env-harvesting` — a file contains BOTH:
 *        - `process.env` somewhere in the file, AND
 *        - a case-insensitive word-boundary match for `fetch`, `post`, or
 *          `http.request` anywhere in the same file.
 *      Fixed in 3.0.4/3.0.5 by centralizing all `process.env` reads into
 *      `config.ts`.
 *
 *   2. `potential-exfiltration` — a file contains BOTH:
 *        - the substring `readFileSync` or `readFile` (NO `fs.` prefix —
 *          the real scanner matches any occurrence, including comments,
 *          function names, and string literals), AND
 *        - a case-insensitive word-boundary match for `fetch`, `post`,
 *          or `http.request` anywhere in the same file.
 *      Fixed in 3.0.7 by extracting billing-cache reads into
 *      `billing-cache.ts`; fixed definitively in 3.0.8 by moving ALL
 *      `fs.*` calls out of `index.ts` into `fs-helpers.ts`. The real
 *      scanner is first-found (reports one line per file at most), so
 *      incremental extractions played whack-a-mole until 3.0.8.
 *
 *   3. `dynamic-code-execution` — a file contains ANY of:
 *        - a match for `\beval\s*\(`, OR
 *        - a match for `new\s+Function\s*\(`.
 *      This is NOT gated by a context trigger — the scanner flags the
 *      match outright. Severity: high; blocks install. Shipped 2026-04-20
 *      after 3.3.0-rc.1 was blocked by a single comment line in
 *      `pair-http.ts` that contained the literal substring `eval (`.
 *
 *   Rules 1 and 2 are whole-file AND'd (both conditions must hit). Rule 3
 *   is a bare pattern — any single hit anywhere in the file trips it.
 *   Even a comment containing the trigger words will fire.
 *
 * This script walks every `.ts/.tsx/.js/.mjs/.cjs/.cts/.mts/.jsx` file
 * under skill/plugin/ (skipping node_modules, dist, hidden dirs) and
 * exits non-zero with a readable error if any file matches any rule.
 *
 * Per-file suppression is available via a `// scanner-sim: allow` comment
 * at the top of a file (top 5 lines). Prefer fixing the false-positive
 * over suppressing — suppression defeats the purpose of the check.
 *
 * Usage:
 *   node skill/scripts/check-scanner.mjs            # exit 0 clean, 1 on any flag
 *   node skill/scripts/check-scanner.mjs --json     # emit structured findings
 *   node skill/scripts/check-scanner.mjs --root PATH  # scan a custom tree
 *                                                      (e.g. an unpacked tarball)
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// skill/scripts/ -> skill/plugin/
const DEFAULT_ROOT = path.resolve(__dirname, '..', 'plugin');

const SCANNABLE_EXT = new Set(['.ts', '.tsx', '.js', '.mjs', '.cjs', '.cts', '.mts', '.jsx']);
const SKIP_DIRS = new Set(['node_modules', 'dist', 'pkg', '.git', 'coverage']);

// Mirrors OpenClaw `skill-scanner-*.js` SOURCE_RULES[env-harvesting]:
//   pattern:         /process\.env/
//   requiresContext: /\bfetch\b|\bpost\b|http\.request/i
const ENV_PATTERN = /process\.env/;
const CONTEXT_PATTERN = /\bfetch\b|\bpost\b|http\.request/i;

// Mirrors OpenClaw SOURCE_RULES[potential-exfiltration] EXACTLY — the real
// scanner uses the bare substrings `readFileSync` and `readFile` with NO
// `fs.` prefix. That means any string containing `readFile` anywhere in
// the file matches (including comments, function names, and test helpers).
// The trigger set is the SAME as env-harvesting (fetch/post/http.request) —
// NOT the wider axios/XMLHttpRequest set we used to use. Confirmed
// 2026-04-19 by inspecting `/app/dist/skill-scanner-*.js` inside the
// OpenClaw 2026.3.7 Docker image.
const FS_READ_PATTERN = /readFileSync|readFile/;
const EXFIL_CONTEXT_PATTERN = /\bfetch\b|\bpost\b|http\.request/i;

// Mirrors OpenClaw SOURCE_RULES[dynamic-code-execution]:
//   pattern:         /\beval\s*\(|new\s+Function\s*\(/
//   requiresContext: <none> — first hit blocks install.
// Shipped 2026-04-20. The rc.1 NO-GO for 3.3.0-rc.1 was a single comment
// line in `pair-http.ts` that contained the substring `eval (` (word
// "eval", space, open-paren) because the comment wrapped mid-word. Even
// though the file never actually CALLS eval, the regex fired.
const DYNAMIC_CODE_PATTERN = /\beval\s*\(|new\s+Function\s*\(/;

// Mirrors OpenClaw SOURCE_RULES[shell-execution]:
//   pattern:         /child_process/
//   requiresContext: <none> — the import alone trips the scanner.
// Shipped 2026-04-22 after 3.3.1-rc.1 NO-GO: `gateway-url.ts` imported
// `child_process.execFileSync` for Tailscale auto-detect, which the
// OpenClaw scanner flagged as "Shell command execution detected" and
// BLOCKED install with:
//   WARNING: Plugin "totalreclaw" contains dangerous code patterns:
//   Shell command execution detected (child_process)
// Even the import line alone fires the rule — the scanner doesn't
// require an actual `spawn`/`exec*` call site. Fix by either:
//   1. Removing the child_process usage entirely (preferred — most
//      subprocess needs have a pure-node alternative), OR
//   2. Moving the subprocess call into a separate post-install helper
//      that OpenClaw sandboxes (NOT covered by this scanner), OR
//   3. Applying for a scanner exemption (see docs/notes/
//      INVESTIGATION-OPENCLAW-SCANNER-EXEMPTION-*).
const SHELL_EXEC_PATTERN = /child_process/;

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

// ---------------------------------------------------------------------------
// CLI parsing
// ---------------------------------------------------------------------------

let ROOT = DEFAULT_ROOT;
const jsonMode = process.argv.includes('--json');
for (let i = 2; i < process.argv.length; i++) {
  const a = process.argv[i];
  if (a === '--root') {
    const next = process.argv[i + 1];
    if (!next) {
      console.error('scanner-sim: --root requires a path argument');
      process.exit(2);
    }
    ROOT = path.resolve(next);
    i++;
  } else if (a.startsWith('--root=')) {
    ROOT = path.resolve(a.slice('--root='.length));
  }
}

const envFindings = [];
const exfilFindings = [];
const dynCodeFindings = [];
const shellExecFindings = [];

if (!fs.existsSync(ROOT)) {
  console.error(`scanner-sim: root directory not found at ${ROOT}`);
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

  // Rule 3 — dynamic-code-execution (no context-trigger gate)
  if (DYNAMIC_CODE_PATTERN.test(src)) {
    const hits = allLinesMatching(lines, DYNAMIC_CODE_PATTERN);
    dynCodeFindings.push({
      file: relPath,
      hits: hits.slice(0, 10),
    });
  }

  // Rule 4 — shell-execution (no context-trigger gate)
  if (SHELL_EXEC_PATTERN.test(src)) {
    const hits = allLinesMatching(lines, SHELL_EXEC_PATTERN);
    shellExecFindings.push({
      file: relPath,
      hits: hits.slice(0, 10),
    });
  }
}

const totalFindings =
  envFindings.length + exfilFindings.length + dynCodeFindings.length + shellExecFindings.length;
if (jsonMode) {
  process.stdout.write(
    JSON.stringify(
      {
        root: ROOT,
        envHarvesting: envFindings,
        potentialExfiltration: exfilFindings,
        dynamicCodeExecution: dynCodeFindings,
        shellExecution: shellExecFindings,
      },
      null,
      2,
    ) + '\n',
  );
  process.exit(totalFindings ? 1 : 0);
}

if (totalFindings === 0) {
  console.log(
    `scanner-sim: OK — ${files.length} files scanned, 0 flags (env-harvesting + potential-exfiltration + dynamic-code-execution + shell-execution) under ${
      path.relative(process.cwd(), ROOT) || ROOT
    }`,
  );
  process.exit(0);
}

console.error(
  `scanner-sim: FAIL — ${envFindings.length} env-harvesting + ${exfilFindings.length} potential-exfiltration + ${dynCodeFindings.length} dynamic-code-execution + ${shellExecFindings.length} shell-execution flag(s)`,
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
  console.error('match for \\bfetch\\b, \\bpost\\b, or http.request. OpenClaw will refuse');
  console.error('to install such plugins. Fix by either:');
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

if (dynCodeFindings.length > 0) {
  console.error('[dynamic-code-execution]');
  console.error('Each of these files contains at least one match for \\beval\\s*\\( or');
  console.error('new\\s+Function\\s*\\(. OpenClaw will refuse to install such plugins even');
  console.error('if the match is a COMMENT. Fix by either:');
  console.error('  1. Rewording the comment so it does not contain the literal substring');
  console.error('     "eval(" or "new Function(" (add a word-break, e.g. "no runtime');
  console.error('     code evaluation"), OR');
  console.error('  2. Removing the call entirely if it is actual runtime code.');
  console.error('');
  for (const f of dynCodeFindings) {
    console.error(`  ${f.file}`);
    for (const t of f.hits) {
      console.error(`    :${t.line}  hit -> ${t.text.slice(0, 120)}`);
    }
  }
  console.error('');
}

if (shellExecFindings.length > 0) {
  console.error('[shell-execution]');
  console.error('Each of these files contains at least one match for `child_process` —');
  console.error('even importing the module (`import ... from "child_process"` or');
  console.error('`require("child_process")`) is enough to trip the rule. OpenClaw refuses');
  console.error('to install plugins that can execute shell commands.');
  console.error('');
  console.error('This rule shipped after 3.3.1-rc.1 NO-GO: `gateway-url.ts` used');
  console.error('`child_process.execFileSync(\"tailscale\", ...)` for MagicDNS auto-detect');
  console.error('and blocked every `openclaw plugins install`. Fix by either:');
  console.error('  1. Removing the child_process usage entirely (prefer pure-node');
  console.error('     alternatives — os.networkInterfaces(), node:dns, node:fs), OR');
  console.error('  2. Moving subprocess logic into a separate post-install helper');
  console.error('     script that OpenClaw sandboxes (NOT inside the main plugin tree).');
  console.error('');
  for (const f of shellExecFindings) {
    console.error(`  ${f.file}`);
    for (const t of f.hits) {
      console.error(`    :${t.line}  hit -> ${t.text.slice(0, 120)}`);
    }
  }
  console.error('');
}

console.error('Suppress a specific file (discouraged) with a top-of-file comment:');
console.error('  // scanner-sim: allow');
process.exit(1);
