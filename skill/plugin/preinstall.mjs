#!/usr/bin/env node
// Runs at npm preinstall during `openclaw plugins install @totalreclaw/totalreclaw`.
//
// Two responsibilities:
//   1. Drop the `.tr-partial-install` marker. detectPartialInstall (rc.22)
//      uses it to identify installs that didn't reach postinstall.
//   2. Remove orphan `.openclaw-install-stage-*` siblings from
//      `~/.openclaw/extensions/` (and `$OPENCLAW_STATE_DIR/extensions/` when set).
//      An interrupted prior install leaves a stage dir behind; on next gateway
//      start, OpenClaw's config validator finds two manifests with the same
//      plugin id, fires `duplicate plugin id detected; global plugin will be
//      overridden by global plugin`, and refuses to register either copy.
//      The register-time `cleanupInstallStagingDirs` helper from rc.21 (#126)
//      cannot recover this: registration is blocked before our code runs.
//
// Best-effort throughout. Never throws or exits non-zero.
//
// Background: issue #190 (umbrella #182 finding F6).

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const STAGE_PREFIX = '.openclaw-install-stage-';

try {
  fs.writeFileSync('.tr-partial-install', '');
} catch { /* swallow */ }

function cleanExtensionsDir(extensionsDir, selfName) {
  const removed = [];
  let entries;
  try {
    entries = fs.readdirSync(extensionsDir);
  } catch { return removed; }
  for (const name of entries) {
    if (!name.startsWith(STAGE_PREFIX)) continue;
    if (name === selfName) continue;
    const target = path.join(extensionsDir, name);
    try {
      if (!fs.lstatSync(target).isDirectory()) continue;
      fs.rmSync(target, { recursive: true, force: true });
      removed.push(target);
    } catch { /* skip racy / unreadable */ }
  }
  return removed;
}

const cwd = process.cwd();
const cwdName = path.basename(cwd);

const candidates = new Set();
if (cwdName.startsWith(STAGE_PREFIX)) {
  candidates.add(path.dirname(cwd));
}
candidates.add(path.join(os.homedir(), '.openclaw', 'extensions'));
if (process.env.OPENCLAW_STATE_DIR) {
  candidates.add(path.join(process.env.OPENCLAW_STATE_DIR, 'extensions'));
}

for (const dir of candidates) {
  for (const removedPath of cleanExtensionsDir(dir, cwdName)) {
    process.stdout.write(`[totalreclaw preinstall] removed orphan install-stage dir: ${removedPath}\n`);
  }
}
