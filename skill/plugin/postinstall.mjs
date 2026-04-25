#!/usr/bin/env node
/**
 * postinstall — sweep `.openclaw-install-stage-*` orphan siblings (issue #134).
 *
 * Background
 * ----------
 * `openclaw plugins install @totalreclaw/totalreclaw` extracts the npm
 * tarball into `<extensionsDir>/.openclaw-install-stage-XXXXXX/` and then
 * renames it to `<extensionsDir>/totalreclaw/` on success. If a prior
 * install was interrupted mid-extract (auto-gateway-restart kill, OOM,
 * Ctrl+C), the staging directory survives on disk. On the next gateway
 * start OpenClaw's plugin loader auto-discovers it as a candidate plugin
 * and tries to load its (incomplete) `dist/index.js`, which throws
 * `Cannot find module '@huggingface/transformers'`. The error propagates
 * as `PluginLoadFailureError` and the gateway / CLI exits non-zero.
 *
 * The plugin's existing register-time cleanup (`cleanupInstallStagingDirs`
 * in `fs-helpers.ts`) is defeated by load order: by the time our register
 * code runs, the orphan has already crashed the loader.
 *
 * Why postinstall fixes it
 * ------------------------
 * npm runs this script after extracting the new tarball into the staging
 * dir but BEFORE OpenClaw renames it into place AND BEFORE the next
 * gateway restart re-scans extensions. So we sit OUTSIDE the loader cycle
 * — even if the loader would otherwise crash on orphan siblings, those
 * siblings are gone before it gets a chance.
 *
 * Constraints
 * -----------
 *   - Must NEVER abort the install. Best-effort throughout, exit 0.
 *   - No deps beyond `node:fs` / `node:path` / `node:url`.
 *   - Skip self when running from inside a staging dir.
 *   - Skip non-directory entries with the prefix.
 *   - When running from a non-staging context (local source install,
 *     `node_modules/@totalreclaw/totalreclaw`), the parent directory will
 *     have no matching siblings → graceful no-op.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const STAGING_PREFIX = '.openclaw-install-stage-';

function sweep() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const hereName = path.basename(here);
  const parent = path.dirname(here);
  const isStaging = hereName.startsWith(STAGING_PREFIX);
  const selfResolved = path.resolve(here);

  let entries;
  try {
    entries = fs.readdirSync(parent);
  } catch {
    return 0;
  }

  let removed = 0;
  for (const name of entries) {
    if (!name.startsWith(STAGING_PREFIX)) continue;
    const target = path.join(parent, name);
    if (isStaging && path.resolve(target) === selfResolved) continue;
    try {
      const st = fs.lstatSync(target);
      if (!st.isDirectory()) continue;
      fs.rmSync(target, { recursive: true, force: true });
      removed++;
    } catch {
      // Best-effort — skip unreadable / racy entries.
    }
  }
  return removed;
}

try {
  const removed = sweep();
  if (removed > 0) {
    process.stderr.write(
      `[@totalreclaw/totalreclaw postinstall] removed ${removed} stale install-staging dir(s) from prior interrupted install\n`,
    );
  }
} catch {
  // Belt-and-braces — the helper already swallows, but never let postinstall
  // surface a non-zero exit to npm and abort the install.
}
process.exit(0);
