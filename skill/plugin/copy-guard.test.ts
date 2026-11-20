/**
 * Copy guard for agent-facing tier claims (WQ-B fixup, PR #788 review).
 *
 * Spec: docs/specs/ops/write-quota-source-of-truth.md §2 (R-C). index.ts and
 * skill.json are agent-facing surfaces: they must not claim Pro is
 * "unlimited" (it has a monthly cap), that memories are "stored on testnet"
 * (single-chain Gnosis since ops-1), or hardcode a quota number — the
 * figures come from /v1/billing/status at runtime, and when the field is
 * absent the copy omits the number.
 *
 * Reads the sources as TEXT (no imports), so it also works before any
 * build and needs no dependencies.
 *
 * Run with: npx tsx skill/plugin/copy-guard.test.ts   (repo root)
 *        or: npx tsx copy-guard.test.ts               (skill/plugin)
 *
 * TAP-style output, no jest dependency. Auto-discovered by run-tests.mjs.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const indexTs = readFileSync(new URL('./index.ts', import.meta.url), 'utf8');
const skillJson = readFileSync(new URL('./skill.json', import.meta.url), 'utf8');

// [label, pattern] — case-insensitive. "1,500" is the retired stale Pro cap.
const BANNED: ReadonlyArray<readonly [string, RegExp]> = [
  ['unlimited memories', /unlimited memories/i],
  ['unlimited storage', /unlimited storage/i],
  ['stored on testnet', /stored on testnet/i],
  ['3,000 memories', /\b3,000 memories\b/i],
  ['1,500', /\b1,500\b/i],
];

const FILES: ReadonlyArray<readonly [string, string]> = [
  ['index.ts', indexTs],
  ['skill.json', skillJson],
];

let passed = 0;
let failed = 0;

function check(name: string, fn: () => void): void {
  const n = passed + failed + 1;
  try {
    fn();
    console.log(`ok ${n} - ${name}`);
    passed++;
  } catch (err) {
    console.log(`not ok ${n} - ${name}`);
    console.log(`  ${err instanceof Error ? err.message : String(err)}`);
    failed++;
  }
}

for (const [file, text] of FILES) {
  for (const [label, re] of BANNED) {
    check(`${file} carries no "${label}" claim`, () => {
      const m = re.exec(text);
      if (m !== null) {
        const snip = text.slice(Math.max(0, m.index - 60), m.index + 60).replace(/\s+/g, ' ');
        assert.fail(`${file} matches "${label}": …${snip}…`);
      }
    });
  }
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('\nSOME TESTS FAILED');
  process.exit(1);
}
console.log('\nALL TESTS PASSED');
