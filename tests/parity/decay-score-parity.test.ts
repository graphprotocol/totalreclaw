/**
 * graphprotocol/totalreclaw#789 — on-chain decayScore parity (TypeScript leg).
 *
 * Fixture `fixtures/decay-score-v1.json`: 0 = tombstone, 1.0 = live, no
 * importance. Asserts the WASM core (`liveDecayScore`), the MCP copy
 * (`mcp/src/subgraph/decay.ts`) and the OpenClaw plugin copy
 * (`skill/plugin/subgraph/decay-score.ts`) all return the fixture value, that
 * the live value is at or above the subgraph threshold, and that the
 * threshold in the fixture still equals the one in `subgraph/src/mapping.ts`.
 *
 * Siblings: rust/totalreclaw-core/tests/decay_score_parity.rs (Rust),
 * tests/parity/test_decay_score_parity.py (Python).
 *
 * Run (needs rust/totalreclaw-core/pkg from
 * `wasm-pack build --target nodejs --out-dir pkg --features wasm`):
 *   cd tests/parity && npx tsx decay-score-parity.test.ts
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const ROOT = join(__dirname, '..', '..');

const fx = JSON.parse(readFileSync(join(__dirname, 'fixtures', 'decay-score-v1.json'), 'utf8')) as {
  live: number;
  tombstone: number;
  subgraph_threshold: number;
};

const wasm = require(join(ROOT, 'rust', 'totalreclaw-core', 'pkg', 'totalreclaw_core.js')) as {
  liveDecayScore?: () => number;
};
const mcp = require(join(ROOT, 'mcp', 'src', 'subgraph', 'decay.ts')) as {
  liveDecayScore: () => number;
  LIVE_DECAY_SCORE: number;
  ACTIVE_DECAY_THRESHOLD: number;
};
const plugin = (await import(join(ROOT, 'skill', 'plugin', 'subgraph', 'decay-score.ts'))) as {
  liveDecayScore: () => number;
  LIVE_DECAY_SCORE: number;
  ACTIVE_DECAY_THRESHOLD: number;
};

let failed = 0;
function check(name: string, ok: boolean): void {
  console.log(`${ok ? 'ok' : 'not ok'} - ${name}`);
  if (!ok) failed++;
}

check('WASM core exports liveDecayScore', typeof wasm.liveDecayScore === 'function');
check('WASM core value == fixture', wasm.liveDecayScore?.() === fx.live);
check('MCP copy == fixture', mcp.liveDecayScore() === fx.live && mcp.LIVE_DECAY_SCORE === fx.live);
check('plugin copy == fixture', plugin.liveDecayScore() === fx.live && plugin.LIVE_DECAY_SCORE === fx.live);
check('MCP copy == WASM core', mcp.liveDecayScore() === wasm.liveDecayScore?.());
check('plugin copy == WASM core', plugin.liveDecayScore() === wasm.liveDecayScore?.());
check('tombstone value is 0', fx.tombstone === 0);
check('live value stays above the subgraph threshold', fx.live >= fx.subgraph_threshold);

const mapping = readFileSync(join(ROOT, 'subgraph', 'src', 'mapping.ts'), 'utf8');
const m = mapping.match(/INACTIVE_THRESHOLD\s*=\s*BigDecimal\.fromString\("([0-9.]+)"\)/);
check('subgraph INACTIVE_THRESHOLD == fixture threshold', m !== null && Number(m[1]) === fx.subgraph_threshold);
check('subgraph still compares with ge', mapping.includes('decoded.decayScore.ge(INACTIVE_THRESHOLD)'));
check(
  'MCP + plugin thresholds == fixture',
  mcp.ACTIVE_DECAY_THRESHOLD === fx.subgraph_threshold && plugin.ACTIVE_DECAY_THRESHOLD === fx.subgraph_threshold,
);

if (failed > 0) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log('decay-score parity: all checks passed');
