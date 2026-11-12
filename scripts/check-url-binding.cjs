#!/usr/bin/env node
/**
 * check-url-binding.cjs: publish-time relay-URL guard for the built artifacts
 * that have no package-specific guard of their own: NanoClaw,
 * `@totalreclaw/client`, `@totalreclaw/core` (npm WASM), the crates.io crates,
 * the PyO3 `totalreclaw-core` wheels and sdist, and the Python client wheel
 * (p-diogo/totalreclaw-internal#706). The OpenClaw plugin and the MCP server
 * keep their own guards (`skill/scripts/check-url-binding.mjs`,
 * `mcp/scripts/check-url-binding.cjs`); this script follows the same contract
 * and the same literal-matching approach as the MCP one.
 *
 * Contract (the "F flip", public PR #198, 2026-05-07):
 *
 *   BOTH `release-type=stable` AND `release-type=rc` artifacts default to
 *   production (`https://api.totalreclaw.xyz`). Staging is opt-in at runtime
 *   via `TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz`. The
 *   user's env always wins; only the default is fixed.
 *
 * Checks (identical for both release types), per profile:
 *   1. No shipped source file binds a staging URL as a string literal
 *      (`"https://api-staging..."`, `'wss://api-staging...'`). Mentions in
 *      comments are stripped first, and a bare host without a scheme (the
 *      staging-detection checks behind the "staging mode" banners, e.g.
 *      `"api-staging.totalreclaw.xyz" in url`) does not match, so both pass.
 *   2. No shipped binary (`.wasm`, `.so`, `.pyd`, `.dylib`, `.node`) contains
 *      the staging host at all.
 *   3. Every canonical default-URL site still binds production. Catches a
 *      default swapped for staging, a localhost URL, or anything else.
 *   4. Profiles for artifacts that bind no relay URL by design (the client
 *      library, the core crate and its builds) fail on ANY relay URL,
 *      production included. Adding a default there must come with a new
 *      canonical site in the profile below, so the guard keeps covering it.
 *
 * Usage:
 *   node scripts/check-url-binding.cjs --profile=<name> --root=<dir> [--release-type=rc|stable]
 *
 * `--root` is the unpacked artifact: the `package/` directory of an
 * `npm pack` tarball, the unpacked `.crate`, the unzipped wheel, or the
 * unpacked sdist. `--release-type` falls back to $TOTALRECLAW_RELEASE_TYPE,
 * then `stable`; it only labels the output, since the checks are the same.
 *
 * Exit codes: 0 invariants hold, 1 violation, 2 usage error.
 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');

const STAGING_HOST = 'api-staging.totalreclaw.xyz';
const PRODUCTION_HOST = 'api.totalreclaw.xyz';

// Quote characters that delimit string literals, per language.
const QUOTES = {
  js: '\'"`',
  rust: '"',
  python: '\'"',
};

const CODE_EXT = {
  js: new Set(['.js', '.cjs', '.mjs', '.ts']),
  rust: new Set(['.rs']),
  python: new Set(['.py', '.pyi']),
};

const BINARY_EXT = new Set(['.wasm', '.so', '.pyd', '.dylib', '.node']);
const SKIP_DIRS = new Set(['node_modules', '.git', 'coverage', 'target', '__pycache__']);

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Quoted `<scheme>://<host>` literal, http(s) or ws(s), in the given quote set. */
function quotedUrlRe(host, quotes, { exactHost = false } = {}) {
  const q = `[${escapeRe(quotes)}]`;
  const nq = `[^${escapeRe(quotes)}]*`;
  const tail = exactHost ? `\\/?${q}` : `${nq}${q}`;
  return new RegExp(`${q}(?:https?|wss?):\\/\\/${escapeRe(host)}${tail}`);
}

/** Replace every character except newlines with a space (keeps line numbers). */
function blank(s) {
  return s.replace(/[^\n]/g, ' ');
}

/** The identifier that ends just before `i`, skipping whitespace. */
function wordBefore(src, i) {
  let k = i - 1;
  while (k >= 0 && /\s/.test(src[k])) k--;
  let w = '';
  while (k >= 0 && /[A-Za-z0-9_$]/.test(src[k])) w = src[k--] + w;
  return w;
}

const JS_REGEX_AFTER = new Set(['return', 'typeof', 'case', 'in', 'of', 'void', 'delete', 'throw', 'new', 'yield', 'await']);

/**
 * Blank out comments in JS/TS or Rust source, leaving strings intact. A
 * small tokenizer, not a regex: a line comment that contains `/*` (e.g.
 * "mounted at /workspace/extra/*") must not open a block comment, and a URL
 * inside a string must not read as a `//` comment.
 */
function stripCLike(src, lang) {
  const isJs = lang === 'js';
  const n = src.length;
  let out = '';
  let lastSig = ''; // last non-space code character, for JS regex detection
  let i = 0;
  while (i < n) {
    const c = src[i];
    const d = src[i + 1];
    if (c === '/' && d === '/') {
      const j = src.indexOf('\n', i);
      const end = j === -1 ? n : j;
      out += blank(src.slice(i, end));
      i = end;
      continue;
    }
    if (c === '/' && d === '*') {
      let depth = 1;
      let j = i + 2;
      while (j < n && depth > 0) {
        if (!isJs && src[j] === '/' && src[j + 1] === '*') {
          depth++; // Rust block comments nest
          j += 2;
        } else if (src[j] === '*' && src[j + 1] === '/') {
          depth--;
          j += 2;
        } else {
          j++;
        }
      }
      out += blank(src.slice(i, j));
      i = j;
      continue;
    }
    if (!isJs && (c === 'r' || (c === 'b' && d === 'r')) && !/[A-Za-z0-9_]/.test(src[i - 1] || '')) {
      // Rust raw string: r"..", r#".."#, br"..".
      const start = c === 'b' ? i + 2 : i + 1;
      let k = start;
      while (src[k] === '#') k++;
      if (src[k] === '"') {
        const close = '"' + src.slice(start, k);
        const j = src.indexOf(close, k + 1);
        const end = j === -1 ? n : j + close.length;
        out += src.slice(i, end);
        i = end;
        lastSig = '"';
        continue;
      }
    }
    if (!isJs && c === "'") {
      // Rust char literal ('"', '\n', '\u{1F600}'); otherwise a lifetime.
      const m = /^'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]+\}|.)|[^\\'\n])'/.exec(src.slice(i, i + 16));
      const lit = m ? m[0] : c;
      out += lit;
      i += lit.length;
      lastSig = "'";
      continue;
    }
    if (c === '"' || (isJs && (c === "'" || c === '`'))) {
      let j = i + 1;
      while (j < n && src[j] !== c) {
        if (src[j] === '\\') j++;
        else if (isJs && c !== '`' && src[j] === '\n') break; // unterminated
        j++;
      }
      const end = Math.min(j + 1, n);
      out += src.slice(i, end);
      i = end;
      lastSig = c;
      continue;
    }
    if (isJs && c === '/') {
      const w = wordBefore(src, i);
      const regexContext = w ? JS_REGEX_AFTER.has(w) : lastSig === '' || '(,=:[!&|?{};+-*%<>~^'.includes(lastSig);
      if (regexContext) {
        let j = i + 1;
        let inClass = false;
        while (j < n && src[j] !== '\n') {
          if (src[j] === '\\') {
            j += 2;
            continue;
          }
          if (src[j] === '[') inClass = true;
          else if (src[j] === ']') inClass = false;
          else if (src[j] === '/' && !inClass) break;
          j++;
        }
        if (src[j] === '/') {
          j++;
          while (/[a-z]/i.test(src[j] || '')) j++;
          out += src.slice(i, j);
          i = j;
          lastSig = '/';
          continue;
        }
      }
    }
    out += c;
    if (!/\s/.test(c)) lastSig = c;
    i++;
  }
  return out;
}

/** Blank out `#` comments in Python source, leaving strings (and docstrings) intact. */
function stripPython(src) {
  const n = src.length;
  let out = '';
  let i = 0;
  while (i < n) {
    const c = src[i];
    if (c === '#') {
      const j = src.indexOf('\n', i);
      const end = j === -1 ? n : j;
      out += blank(src.slice(i, end));
      i = end;
      continue;
    }
    if (c === '"' || c === "'") {
      const q = src.startsWith(c.repeat(3), i) ? c.repeat(3) : c;
      let j = i + q.length;
      while (j < n && !src.startsWith(q, j)) {
        if (src[j] === '\\') j++; // an escaped quote never closes, raw strings included
        else if (q.length === 1 && src[j] === '\n') break; // unterminated
        j++;
      }
      const end = Math.min(j + q.length, n);
      out += src.slice(i, end);
      i = end;
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

/** Strip comments so mentions in them never count; strings stay intact. */
function stripComments(src, lang) {
  return lang === 'python' ? stripPython(src) : stripCLike(src, lang);
}

/**
 * Profiles: one per artifact. `include` lists the directories (relative to
 * the artifact root) that hold shipped code; at least one must exist.
 * `sites` are the canonical default-URL sites; each must match `pattern`
 * (default: a quoted production URL) after comments are stripped.
 * `noRelayUrls` (a reason string) marks an artifact that binds no relay URL.
 */
const PROFILES = {
  nanoclaw: {
    label: '@totalreclaw/skill-nanoclaw (npm tarball)',
    lang: 'js',
    include: ['dist', 'mcp'],
    sites: [
      // billing status fetch: TOTALRECLAW_SERVER_URL || production
      { file: 'dist/billing.js' },
      // URL handed to the MCP child process: TOTALRECLAW_SERVER_URL || production
      { file: 'mcp/nanoclaw-agent-runner.ts' },
    ],
  },
  client: {
    label: '@totalreclaw/client (npm tarball)',
    lang: 'js',
    include: ['dist'],
    sites: [],
    noRelayUrls:
      'the client library has no default relay URL: callers must pass `serverUrl`',
  },
  'core-npm': {
    label: '@totalreclaw/core (npm WASM package)',
    lang: 'js',
    include: ['nodejs', 'web'],
    sites: [],
    noRelayUrls: 'totalreclaw-core is pure computation and binds no relay URL',
  },
  'core-crate': {
    label: 'totalreclaw-core (crate or PyPI sdist)',
    lang: 'rust',
    include: ['src'],
    sites: [],
    noRelayUrls: 'totalreclaw-core is pure computation and binds no relay URL',
  },
  'core-wheel': {
    label: 'totalreclaw-core (PyPI wheel)',
    lang: 'python',
    include: ['totalreclaw_core'],
    sites: [],
    noRelayUrls: 'totalreclaw-core is pure computation and binds no relay URL',
  },
  'memory-crate': {
    label: 'totalreclaw-memory (crate)',
    lang: 'rust',
    include: ['src'],
    sites: [
      {
        file: 'src/backend.rs',
        pattern: /const DEFAULT_RELAY_URL: &str = "https:\/\/api\.totalreclaw\.xyz";/,
      },
    ],
  },
  'python-client': {
    label: 'totalreclaw (Python client wheel)',
    lang: 'python',
    include: ['totalreclaw'],
    sites: [
      // THE canonical default for reads and writes (see relay.py).
      {
        file: 'totalreclaw/relay.py',
        pattern: /^_HARDCODED_DEFAULT_URL\s*=\s*"https:\/\/api\.totalreclaw\.xyz"\s*$/m,
      },
      // Pairing WebSocket default.
      {
        file: 'totalreclaw/pair/remote_client.py',
        pattern: /^DEFAULT_RELAY_URL\s*=\s*"wss:\/\/api\.totalreclaw\.xyz"\s*$/m,
      },
    ],
  },
};

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
    else if (e.isFile()) out.push(p);
  }
  return out;
}

/**
 * Check an unpacked artifact against a profile.
 * Returns { ok, profile, root, errors, scannedCode, scannedBinary }.
 */
function checkUrlBinding({ profile: name, root }) {
  const profile = PROFILES[name];
  if (!profile) {
    throw new Error(`unknown profile "${name}" (known: ${Object.keys(PROFILES).join(', ')})`);
  }
  const errors = [];
  const rel = (f) => path.relative(root, f);
  const quotes = QUOTES[profile.lang];
  const stagingRe = quotedUrlRe(STAGING_HOST, quotes);
  const productionRe = quotedUrlRe(PRODUCTION_HOST, quotes);
  const productionExactRe = quotedUrlRe(PRODUCTION_HOST, quotes, { exactHost: true });

  const dirs = profile.include.map((d) => path.join(root, d)).filter((d) => fs.existsSync(d));
  if (dirs.length === 0) {
    errors.push(
      `none of ${profile.include.map((d) => `${d}/`).join(', ')} exists under ${root}: ` +
        'not the expected artifact layout',
    );
    return { ok: false, profile: name, root, errors, scannedCode: 0, scannedBinary: 0 };
  }

  const files = dirs.flatMap((d) => walk(d));
  const stagingHits = [];
  const relayHits = [];
  let scannedCode = 0;
  let scannedBinary = 0;

  for (const f of files) {
    const ext = path.extname(f);
    if (BINARY_EXT.has(ext)) {
      scannedBinary++;
      const buf = fs.readFileSync(f);
      if (buf.includes(STAGING_HOST)) stagingHits.push(`${rel(f)}: binary contains ${STAGING_HOST}`);
      else if (profile.noRelayUrls && buf.includes(PRODUCTION_HOST)) {
        relayHits.push(`${rel(f)}: binary contains ${PRODUCTION_HOST}`);
      }
      continue;
    }
    if (!CODE_EXT[profile.lang].has(ext) || f.endsWith('.d.ts')) continue;
    scannedCode++;
    const lines = stripComments(fs.readFileSync(f, 'utf8'), profile.lang).split('\n');
    lines.forEach((line, i) => {
      if (stagingRe.test(line)) stagingHits.push(`${rel(f)}:${i + 1}: ${line.trim()}`);
      else if (profile.noRelayUrls && productionRe.test(line)) {
        relayHits.push(`${rel(f)}:${i + 1}: ${line.trim()}`);
      }
    });
  }

  if (stagingHits.length > 0) {
    errors.push(
      `staging URL bound in ${stagingHits.length} place(s):\n` +
        stagingHits.map((s) => `    - ${s}`).join('\n'),
    );
  }
  if (relayHits.length > 0) {
    errors.push(
      `relay URL found, but ${profile.noRelayUrls}. If this artifact now has a ` +
        'default, add it to the profile as a canonical site in scripts/check-url-binding.cjs:\n' +
        relayHits.map((s) => `    - ${s}`).join('\n'),
    );
  }

  const missing = [];
  for (const site of profile.sites) {
    const f = path.join(root, site.file);
    if (!fs.existsSync(f)) {
      missing.push(`${site.file} (file missing)`);
      continue;
    }
    const code = stripComments(fs.readFileSync(f, 'utf8'), profile.lang);
    const pattern = site.pattern || productionExactRe;
    if (!pattern.test(code)) missing.push(`${site.file} (no production default; expected ${pattern})`);
  }
  if (missing.length > 0) {
    errors.push(
      'canonical default-URL site(s) lost the production default:\n' +
        missing.map((s) => `    - ${s}`).join('\n'),
    );
  }

  return { ok: errors.length === 0, profile: name, root, errors, scannedCode, scannedBinary };
}

function parseArgs(argv, env) {
  let releaseType = env.TOTALRECLAW_RELEASE_TYPE || 'stable';
  let profile;
  let root;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const take = (flag) => (a === flag ? argv[++i] : a.slice(flag.length + 1));
    if (a === '--release-type' || a.startsWith('--release-type=')) releaseType = take('--release-type');
    else if (a === '--profile' || a.startsWith('--profile=')) profile = take('--profile');
    else if (a === '--root' || a.startsWith('--root=')) root = take('--root');
    else throw new Error(`unknown argument "${a}"`);
  }
  if (releaseType !== 'rc' && releaseType !== 'stable') {
    throw new Error(`invalid --release-type "${releaseType}" (must be rc | stable)`);
  }
  if (!profile) throw new Error('--profile is required');
  if (!PROFILES[profile]) {
    throw new Error(`unknown --profile "${profile}" (known: ${Object.keys(PROFILES).join(', ')})`);
  }
  if (!root) throw new Error('--root is required');
  return { releaseType, profile, root: path.resolve(root) };
}

if (require.main === module) {
  let args;
  try {
    args = parseArgs(process.argv.slice(2), process.env);
  } catch (err) {
    console.error(`check-url-binding: ${err.message}`);
    process.exit(2);
  }
  const r = checkUrlBinding(args);
  const label = PROFILES[args.profile].label;
  if (!r.ok) {
    console.error(`check-url-binding: FAIL (${args.releaseType}, ${label}, ${args.root})`);
    for (const e of r.errors) console.error(`  ${e}`);
    console.error('');
    console.error('  Both stable and RC artifacts must default to https://api.totalreclaw.xyz.');
    console.error('  Staging is opt-in via the TOTALRECLAW_SERVER_URL env override.');
    process.exit(1);
  }
  const p = PROFILES[args.profile];
  const what = p.noRelayUrls
    ? 'no relay URL bound (as expected)'
    : `${p.sites.length} default site(s) bind production`;
  console.log(
    `check-url-binding: OK (${args.releaseType}, ${label}): ${what}, 0 staging literals ` +
      `in ${r.scannedCode} code file(s) and ${r.scannedBinary} binary file(s)`,
  );
}

module.exports = { checkUrlBinding, parseArgs, stripComments, PROFILES };
