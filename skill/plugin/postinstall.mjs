// scanner-sim: allow — postinstall scripts run during `npm install`, NOT inside the OpenClaw runtime sandbox. Per check-scanner.mjs guidance ("Moving the subprocess call into a separate post-install helper that OpenClaw sandboxes (NOT covered by this scanner)"), this file is the intended home for child_process usage. The plugin's runtime code (index.ts, etc.) stays scanner-clean; this file only runs once at install-time.
/**
 * postinstall.mjs — TotalReclaw plugin post-install lifecycle script.
 *
 * Runs after `npm install` finishes inside the plugin extension dir
 * (`~/.openclaw/extensions/totalreclaw/`). Three jobs, in order:
 *
 *   1. Clean the partial-install marker (`.tr-partial-install`) that
 *      `preinstall` dropped. Mirrors the inline shim that shipped in
 *      pre-3.3.2 releases.
 *   2. (3.3.2-rc.1 / issue #188) Smoke-check critical deps. After `npm
 *      install` claims success we require() the modules whose absence
 *      bricked rc.22 first-attempt installs (`@scure/bip39`,
 *      `@scure/bip39/wordlists/english.js`, `@totalreclaw/core`,
 *      `@totalreclaw/client`, `qrcode`, `ws`). If any throws, the
 *      post-install fails LOUDLY — better than the rc.21 silent
 *      half-install where `enabled: true` shipped with a missing dep.
 *   3. (3.3.2-rc.1 / issue #190) Sweep `<extensions>/.openclaw-install-stage-*`
 *      siblings. The runtime register() helper handles this on plugin
 *      load too, but doing it here means a re-install starts from a
 *      clean parent dir — no "duplicate plugin id detected; global
 *      plugin will be overridden by global plugin" warning during the
 *      install itself.
 *
 * Constraints:
 *   - Must be idempotent: re-running on a clean tree is a no-op.
 *   - Must not import any production module that itself runs `register()`
 *     or makes outbound calls. We use only Node stdlib + dynamic require()
 *     of the smoke-check deps.
 *   - Must run in CommonJS-compatible Node ESM (the plugin's package.json
 *     declares `"type": "module"`, so this file uses `.mjs` and
 *     `createRequire` to call require() against the plugin's node_modules).
 *
 * Phrase-safety note: this file does NOT touch credentials.json, mnemonics,
 * keys, or any phrase code path. It only validates module loading and
 * cleans staging directories.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

const PARTIAL_INSTALL_MARKER = '.tr-partial-install';

// Order matters: light, fast modules first so a failure surfaces quickly.
// `@scure/bip39/wordlists/english.js` is the EXACT path that bricked rc.21
// (issue #188 — `Cannot find module '@scure/bip39/wordlists/english.js'`).
const CRITICAL_DEPS = [
  '@scure/bip39',
  '@scure/bip39/wordlists/english.js',
  '@totalreclaw/core',
  '@totalreclaw/client',
  'qrcode',
  'ws',
];

function log(msg) {
  process.stdout.write(`[totalreclaw postinstall] ${msg}\n`);
}

function warn(msg) {
  process.stderr.write(`[totalreclaw postinstall] WARN: ${msg}\n`);
}

// ---------------------------------------------------------------------------
// Step 1 — clear .tr-partial-install marker
// ---------------------------------------------------------------------------

function clearPartialInstallMarker() {
  try {
    const markerPath = path.join(here, PARTIAL_INSTALL_MARKER);
    if (fs.existsSync(markerPath)) {
      fs.unlinkSync(markerPath);
      log('cleared .tr-partial-install marker');
    }
  } catch (err) {
    // Best-effort. The runtime register() also clears this defensively.
    warn(`could not clear .tr-partial-install marker: ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// Step 2 — atomic critical-dep validation (issue #188)
// ---------------------------------------------------------------------------

/**
 * Try to require() each critical dep. Returns the list of names that
 * failed; an empty array means everything resolved.
 */
function smokeCheckDeps() {
  const missing = [];
  for (const dep of CRITICAL_DEPS) {
    try {
      require(dep);
    } catch (err) {
      missing.push({ dep, message: err.message });
    }
  }
  return missing;
}

/**
 * Recovery path: if smoke-check fails, blow away the local node_modules
 * tree the parent install populated and re-run `npm install --no-audit
 * --no-fund --no-save --offline=false` once. This is meant to recover
 * from race-condition partial-fetches (issue #188), NOT from a missing
 * dep in package.json.
 *
 * If the second attempt also fails, exit non-zero so `openclaw plugins
 * install` surfaces the failure to the agent rather than writing
 * `enabled: true` over a broken install.
 *
 * Skipped if `TOTALRECLAW_SKIP_POSTINSTALL_RETRY=1` (CI / sandboxes that
 * cannot reach the registry from inside the postinstall hook).
 */
function retryNpmInstall() {
  if (process.env.TOTALRECLAW_SKIP_POSTINSTALL_RETRY === '1') {
    warn('TOTALRECLAW_SKIP_POSTINSTALL_RETRY=1 — skipping retry');
    return false;
  }
  try {
    log('first-attempt smoke check failed — clearing node_modules and retrying npm install once...');
    const nm = path.join(here, 'node_modules');
    if (fs.existsSync(nm)) {
      fs.rmSync(nm, { recursive: true, force: true });
    }
    // Note: we deliberately re-invoke npm install here. The `--ignore-scripts`
    // flag is critical — without it we'd re-trigger this same postinstall
    // and recurse forever.
    execSync('npm install --no-audit --no-fund --ignore-scripts', {
      cwd: here,
      stdio: 'inherit',
    });
    log('retry npm install completed; re-validating deps');
    return true;
  } catch (err) {
    warn(`retry npm install failed: ${err.message}`);
    return false;
  }
}

function validateDepsOrFail() {
  const firstMiss = smokeCheckDeps();
  if (firstMiss.length === 0) {
    log(`smoke check OK (${CRITICAL_DEPS.length} critical deps resolved)`);
    return;
  }

  warn(`smoke check failed on first attempt:`);
  for (const m of firstMiss) {
    warn(`  - ${m.dep}: ${m.message}`);
  }

  const retried = retryNpmInstall();
  if (!retried) {
    process.exitCode = 1;
    throw new Error(
      `TotalReclaw postinstall: critical deps missing after npm install — ` +
        `[${firstMiss.map((m) => m.dep).join(', ')}]. ` +
        `Re-run \`openclaw plugins install @totalreclaw/totalreclaw\` to retry, ` +
        `or set TOTALRECLAW_SKIP_POSTINSTALL_RETRY=1 to bypass and surface the ` +
        `original error.`,
    );
  }

  const secondMiss = smokeCheckDeps();
  if (secondMiss.length === 0) {
    log(`smoke check OK after retry (${CRITICAL_DEPS.length} deps resolved)`);
    return;
  }

  process.exitCode = 1;
  throw new Error(
    `TotalReclaw postinstall: deps still missing after retry — ` +
      `[${secondMiss.map((m) => m.dep).join(', ')}]. ` +
      `This is likely a permanent breakage (registry outage, package rename, ` +
      `or corrupted node_modules). The plugin will not load. Original errors:\n` +
      secondMiss.map((m) => `  - ${m.dep}: ${m.message}`).join('\n'),
  );
}

// ---------------------------------------------------------------------------
// Step 3 — sweep `.openclaw-install-stage-*` siblings (issue #190)
// ---------------------------------------------------------------------------

/**
 * Resolve the OpenClaw extensions dir from the plugin's own location.
 * The plugin lives at `<extensions>/totalreclaw/` so the parent is the
 * extensions root. Returns null if the layout is not what we expect
 * (npm tarball linked outside an `<extensions>/` parent — e.g. dev
 * checkout) so we never delete random siblings.
 */
function resolveExtensionsDir() {
  // `here` is the plugin root (this file is at the package root, NOT in dist/).
  // The parent should be the OpenClaw extensions directory.
  const parent = path.resolve(here, '..');
  // Heuristic check: only sweep if we look like we're inside an OpenClaw
  // install dir. We accept (a) the well-known `extensions` dirname, OR
  // (b) the presence of any sibling `.openclaw-install-stage-*` (which is
  // proof we're inside an extensions dir).
  if (path.basename(parent) === 'extensions') return parent;
  try {
    const entries = fs.readdirSync(parent);
    if (entries.some((n) => n.startsWith('.openclaw-install-stage-'))) {
      return parent;
    }
  } catch {
    // Parent unreadable — bail safely.
  }
  return null;
}

function sweepStagingSiblings() {
  const extensionsDir = resolveExtensionsDir();
  if (!extensionsDir) {
    log('no extensions parent detected (dev checkout?) — skipping staging sweep');
    return;
  }
  let removed = 0;
  let entries;
  try {
    entries = fs.readdirSync(extensionsDir);
  } catch (err) {
    warn(`could not list ${extensionsDir}: ${err.message}`);
    return;
  }
  for (const name of entries) {
    if (!name.startsWith('.openclaw-install-stage-')) continue;
    const target = path.join(extensionsDir, name);
    try {
      const st = fs.lstatSync(target);
      if (!st.isDirectory()) continue;
      fs.rmSync(target, { recursive: true, force: true });
      removed++;
      log(`removed stale staging dir: ${name}`);
    } catch (err) {
      warn(`could not remove ${name}: ${err.message}`);
    }
  }
  if (removed === 0) {
    log('no stale staging dirs to sweep');
  } else {
    log(`swept ${removed} stale staging dir(s)`);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

clearPartialInstallMarker();
sweepStagingSiblings();
validateDepsOrFail();

log('postinstall complete');
