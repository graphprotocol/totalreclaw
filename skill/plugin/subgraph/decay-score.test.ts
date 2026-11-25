/**
 * #789 — on-chain decayScore: 0 = tombstone, 1.0 = live, no importance.
 *
 * TAP-style, no framework. Run with: npx tsx subgraph/decay-score.test.ts
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ACTIVE_DECAY_THRESHOLD, LIVE_DECAY_SCORE, liveDecayScore } from './decay-score.js';

const here = dirname(fileURLToPath(import.meta.url));
const pluginRoot = join(here, '..');
const src = (f: string) => readFileSync(join(pluginRoot, f), 'utf8');

let passed = 0;
let failed = 0;
function assert(ok: boolean, name: string): void {
  console.log(`${ok ? 'ok' : 'not ok'} ${passed + failed + 1} - ${name}`);
  if (ok) passed++;
  else failed++;
}

const fixture = JSON.parse(
  readFileSync(join(pluginRoot, '..', '..', 'tests', 'parity', 'fixtures', 'decay-score-v1.json'), 'utf8'),
);
const mapping = readFileSync(join(pluginRoot, '..', '..', 'subgraph', 'src', 'mapping.ts'), 'utf8');

assert(liveDecayScore() === 1.0 && LIVE_DECAY_SCORE === 1.0, 'live decayScore is 1.0');
assert(liveDecayScore() === fixture.live, 'matches parity fixture');
assert(ACTIVE_DECAY_THRESHOLD === fixture.subgraph_threshold, 'threshold matches fixture');
assert(
  mapping.includes('INACTIVE_THRESHOLD = BigDecimal.fromString("0.3")') &&
    mapping.includes('decoded.decayScore.ge(INACTIVE_THRESHOLD)'),
  'subgraph still marks decayScore >= 0.3 active',
);
assert(liveDecayScore() >= ACTIVE_DECAY_THRESHOLD, 'live value is above the subgraph threshold');

const index = src('index.ts');
assert(!/decayScore:\s*effectiveImportance/.test(index), 'index.ts: on-chain write no longer uses importance');
assert(!/decayScore:\s*10\b/.test(index), 'index.ts: digest write no longer a raw 10');
assert(!/match\.decayScore/.test(index), 'index.ts: dedup supersede does not read decayScore');
assert(
  /effectiveImportance = Math\.max\(fact\.importance, dupResult\.match\.importance\)/.test(index),
  'index.ts: supersede inherits the old fact blob importance',
);
assert((index.match(/decayScore: liveDecayScore\(\)/g) ?? []).length >= 3, 'index.ts: live writes use liveDecayScore()');
assert(/decayScore:\s*liveDecayScore\(\)/.test(src('cli/tr-cli.ts')), 'tr-cli remember uses liveDecayScore()');
assert(/decayScore:\s*liveDecayScore\(\)/.test(src('memory/pin.ts')), 'pin/unpin uses liveDecayScore()');
assert(/decayScore:\s*liveDecayScore\(\)/.test(src('memory/retype-setscope.ts')), 'retype/set_scope uses liveDecayScore()');
for (const f of ['index.ts', 'memory/pin.ts', 'memory/retype-setscope.ts']) {
  const lines = src(f).split('\n');
  const tomb = lines.map((l, i) => (/decayScore:\s*0,/.test(l) ? i : -1)).filter((i) => i >= 0);
  assert(tomb.length > 0, `${f}: tombstones still write decayScore 0`);
}

console.log(`# ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
