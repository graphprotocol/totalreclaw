/**
 * Store-time write gate cross-language parity test (TypeScript / WASM side).
 *
 * Loads `fixtures/write-gate-v1.json` (written by
 * `fixtures/generate-write-gate-v1.py`) and checks the WASM exports
 * `normalizeFactText` and `decideWrite` against every case. Siblings: Rust
 * `rust/totalreclaw-core/src/write_gate.rs` (`tests::parity_fixture_vectors`)
 * and Python `python/tests/test_write_gate_parity.py`.
 *
 * Run (build the flat WASM pkg first):
 *   cd rust/totalreclaw-core && wasm-pack build --target nodejs --out-dir pkg --features wasm
 *   cd tests/parity && npx tsx write-gate-parity.test.ts
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

const wasm = require(
  join(__dirname, '..', '..', 'rust', 'totalreclaw-core', 'pkg', 'totalreclaw_core.js'),
) as {
  normalizeFactText: (text: string) => string;
  decideWrite: (
    text: string,
    embeddingJson: string,
    candidatesJson: string,
    mode: string,
    threshold?: number | null,
  ) => string;
};

interface Match {
  fact_id: string;
  text: string;
  kind: string;
  similarity: number;
  pinned: boolean;
}

interface Decision {
  action: string;
  match: Match | null;
}

interface DecideCase {
  name: string;
  text: string;
  embedding: number[] | null;
  candidates: unknown[];
  threshold: number | null;
  mode: string;
  expected: Decision;
}

interface Fixture {
  default_threshold: number;
  normalize: { input: string; expected: string }[];
  decide: DecideCase[];
}

const fixture: Fixture = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'write-gate-v1.json'), 'utf8'),
);

let passed = 0;
let failed = 0;

function check(ok: boolean, label: string, detail = ''): void {
  if (ok) {
    passed++;
  } else {
    failed++;
    console.error(`FAIL [${label}]${detail}`);
  }
}

check(fixture.default_threshold === 0.85, 'default threshold is 0.85');

for (const c of fixture.normalize) {
  const actual = wasm.normalizeFactText(c.input);
  check(actual === c.expected, `normalize ${JSON.stringify(c.input)}`, ` — got ${JSON.stringify(actual)}`);
}

for (const c of fixture.decide) {
  const got: Decision = JSON.parse(
    wasm.decideWrite(c.text, JSON.stringify(c.embedding), JSON.stringify(c.candidates), c.mode, c.threshold),
  );
  const want = c.expected;
  check(got.action === want.action, `case ${c.name}: action`, ` — got ${got.action}, want ${want.action}`);
  if (want.match === null) {
    check(got.match === null, `case ${c.name}: no match`);
    continue;
  }
  check(got.match !== null, `case ${c.name}: has match`);
  if (got.match === null) continue;
  for (const k of ['fact_id', 'text', 'kind', 'pinned'] as const) {
    check(got.match[k] === want.match[k], `case ${c.name}: ${k}`, ` — got ${got.match[k]}, want ${want.match[k]}`);
  }
  check(Math.abs(got.match.similarity - want.match.similarity) < 1e-6, `case ${c.name}: similarity`);
}

function throws(fn: () => unknown): boolean {
  try {
    fn();
  } catch {
    return true;
  }
  return false;
}

check(throws(() => wasm.decideWrite('t', '[]', '[]', 'sometimes')), 'unknown mode throws');
check(throws(() => wasm.decideWrite('t', '[]', '[]', 'auto', 1.5)), 'threshold > 1 throws');
check(throws(() => wasm.decideWrite('t', 'nope', '[]', 'auto')), 'malformed embedding throws');

console.log('\n========================================');
console.log(`write-gate TS/WASM parity: ${passed}/${passed + failed} assertions passed`);
console.log('========================================');
if (failed > 0) {
  console.error(`FAIL: ${failed} assertion(s) failed`);
  process.exit(1);
}
console.log('PASS: write-gate-v1 vectors match between Rust (canonical) and WASM');
