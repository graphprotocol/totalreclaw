#!/usr/bin/env node
/**
 * sync-version.mjs — Propagate `skill/plugin/package.json::version` into
 * the other version sites bundled with the plugin tarball.
 *
 * Why this exists (2026-04-25):
 *   `skill/plugin/package.json::version` is the source of truth. The
 *   `npm-publish.yml` workflow mutates this file in-place during the
 *   "Apply RC version suffix" step (e.g. `3.3.1` -> `3.3.1-rc.22`).
 *   But the SKILL.md frontmatter `version:` line and `skill.json::version`
 *   were NEVER kept in sync, which produced three drift modes seen in
 *   the rc.21 brutal QA:
 *     - package.json checked into main:    3.3.1-rc.15  (stale RC bump)
 *     - SKILL.md frontmatter:               3.3.1-rc.11  (10 RCs stale)
 *     - skill.json:                          1.6.2       (totally divorced)
 *   And the ClawHub workflow never mutated package.json AT ALL before
 *   `clawhub publish`, so the catalog said "rc.21" but the tarball
 *   shipped rc.15 internals.
 *
 * Behavior:
 *   - Reads canonical version from skill/plugin/package.json::version.
 *   - Writes that exact string into:
 *       1. skill/plugin/SKILL.md frontmatter `version:` line
 *       2. skill/plugin/skill.json::version
 *   - Idempotent: running twice on an aligned tree is a no-op.
 *   - Logs each site it touched.
 *
 * Invocation:
 *   - In CI, runs AFTER the "Apply RC version suffix" step that mutates
 *     package.json (npm-publish.yml + publish-clawhub.yml).
 *   - Locally, you can run it any time you bump package.json::version.
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const pluginDir = resolve(here, '..', 'plugin');

const pkgPath = resolve(pluginDir, 'package.json');
const skillMdPath = resolve(pluginDir, 'SKILL.md');
const skillJsonPath = resolve(pluginDir, 'skill.json');

const pkg = JSON.parse(readFileSync(pkgPath, 'utf8'));
const canonical = pkg.version;
if (!canonical) {
  console.error('FATAL: skill/plugin/package.json has no `version` field');
  process.exit(2);
}

console.log(`sync-version: canonical = ${canonical}`);

// 1. SKILL.md frontmatter
{
  const before = readFileSync(skillMdPath, 'utf8');
  // Frontmatter is the first --- ... --- block. The version line looks like:
  //   version: 3.3.1-rc.11
  // We update only the FIRST occurrence inside the frontmatter to avoid
  // touching a `version:` line that might appear in the body.
  const fmEnd = before.indexOf('\n---', before.indexOf('---') + 3);
  if (fmEnd === -1) {
    console.error('FATAL: SKILL.md has no closing frontmatter ---');
    process.exit(2);
  }
  const fmBlock = before.slice(0, fmEnd);
  const rest = before.slice(fmEnd);
  const versionLineRe = /^version:\s*.*$/m;
  if (!versionLineRe.test(fmBlock)) {
    console.error('FATAL: SKILL.md frontmatter has no `version:` line to update');
    process.exit(2);
  }
  const updatedFm = fmBlock.replace(versionLineRe, `version: ${canonical}`);
  const after = updatedFm + rest;
  if (after !== before) {
    writeFileSync(skillMdPath, after);
    console.log(`sync-version: updated SKILL.md frontmatter -> ${canonical}`);
  } else {
    console.log('sync-version: SKILL.md already aligned');
  }
}

// 2. skill.json — targeted regex on the `"version": "..."` top-level line.
//    Avoids JSON.parse/stringify round-tripping (which would reflow arrays
//    like "os": ["macos","linux","windows"] across multiple lines and
//    produce noisy diffs every publish).
{
  const before = readFileSync(skillJsonPath, 'utf8');
  // Sanity: confirm there IS a top-level version field, and that JSON parses.
  const parsed = JSON.parse(before);
  if (typeof parsed.version !== 'string') {
    console.error('FATAL: skill.json has no top-level string `version` field');
    process.exit(2);
  }
  if (parsed.version === canonical) {
    console.log('sync-version: skill.json already aligned');
  } else {
    // Match the FIRST `"version": "..."` line at the top of the file.
    // Anchored to the indentation that the existing `"version"` line uses
    // (top-level field, 2-space indent in this repo's house style).
    const versionLineRe = /^(\s*"version"\s*:\s*")[^"]*(")/m;
    if (!versionLineRe.test(before)) {
      console.error('FATAL: skill.json has no `"version": "..."` line to update');
      process.exit(2);
    }
    const after = before.replace(versionLineRe, `$1${canonical}$2`);
    // Sanity: re-parse to make sure we didn't break the file.
    JSON.parse(after);
    writeFileSync(skillJsonPath, after);
    console.log(`sync-version: updated skill.json -> ${canonical}`);
  }
}

console.log('sync-version: OK');
