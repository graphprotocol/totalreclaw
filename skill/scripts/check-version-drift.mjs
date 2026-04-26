#!/usr/bin/env node
/**
 * check-version-drift.mjs — Pre-publish gate that FAILS if any of the
 * three plugin version sites drift from `package.json::version`.
 *
 * Sites checked:
 *   1. skill/plugin/package.json::version           (canonical / source of truth)
 *   2. skill/plugin/SKILL.md frontmatter `version:`
 *   3. skill/plugin/skill.json::version
 *
 * Why this exists (2026-04-25):
 *   The rc.21 brutal QA caught three different versions on disk for what
 *   should have been a single rc.21 bundle. See `sync-version.mjs` for
 *   the full root-cause write-up.
 *
 *   This script is run:
 *     - As `prepublishOnly` in skill/plugin/package.json (npm publish path)
 *     - As an explicit step in npm-publish.yml + publish-clawhub.yml,
 *       AFTER the RC suffix mutation + sync, so a sync regression also
 *       surfaces as a workflow-level failure (not buried in a packlist).
 *
 * Performance:
 *   Three small file reads, no network, no spawn. <100ms in practice.
 *   Well under the <1min "fast gate" budget.
 *
 * Exit codes:
 *   0 — all three sites match
 *   1 — drift detected (printed to stderr in actionable form)
 */

import { readFileSync } from 'node:fs';
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
  process.exit(1);
}

const skillMd = readFileSync(skillMdPath, 'utf8');
const skillJson = JSON.parse(readFileSync(skillJsonPath, 'utf8'));

// SKILL.md: extract the `version:` line from frontmatter only (first
// --- ... --- block at top of file).
const fmStart = skillMd.indexOf('---');
const fmEnd = skillMd.indexOf('\n---', fmStart + 3);
if (fmStart !== 0 || fmEnd === -1) {
  console.error('FATAL: SKILL.md does not start with a YAML frontmatter block');
  process.exit(1);
}
const frontmatter = skillMd.slice(fmStart, fmEnd);
const versionMatch = frontmatter.match(/^version:\s*(\S+)\s*$/m);
if (!versionMatch) {
  console.error('FATAL: SKILL.md frontmatter has no `version:` line');
  process.exit(1);
}
const skillMdVersion = versionMatch[1];
const skillJsonVersion = skillJson.version;

const sites = [
  { name: 'skill/plugin/package.json::version', value: canonical },
  { name: 'skill/plugin/SKILL.md frontmatter version', value: skillMdVersion },
  { name: 'skill/plugin/skill.json::version', value: skillJsonVersion },
];

const drift = sites.filter((s) => s.value !== canonical);

if (drift.length === 0) {
  console.log(`check-version-drift: OK (all three sites = ${canonical})`);
  process.exit(0);
}

console.error('check-version-drift: FAIL — version drift detected');
console.error('');
console.error(`  canonical (package.json::version) = ${canonical}`);
console.error('');
for (const s of sites) {
  const marker = s.value === canonical ? '   ok   ' : '  DRIFT ';
  console.error(`  [${marker}] ${s.name} = ${s.value}`);
}
console.error('');
console.error('Fix: run `node skill/scripts/sync-version.mjs` to propagate package.json::version');
console.error('to SKILL.md and skill.json, then re-run this gate.');
process.exit(1);
