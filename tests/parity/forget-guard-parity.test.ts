/**
 * Forget-guard cross-language parity test (TypeScript / WASM side).
 *
 * internal#605 / #640. Loads `fixtures/forget-guard-v1.json` (hand-written)
 * and checks the WASM exports `matchForgotten` and `isForgetRequest` against
 * every case. Siblings: Rust `rust/totalreclaw-core/src/forget_guard.rs`
 * (`tests::parity_fixture_vectors`) and Python `python/tests/test_forget_guard.py`
 * (core binding + pure-Python fallback).
 *
 * Run (build the flat WASM pkg first):
 *   cd rust/totalreclaw-core && wasm-pack build --target nodejs --out-dir pkg --features wasm
 *   cd tests/parity && npx tsx forget-guard-parity.test.ts
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
  matchForgotten(text: string, embeddingJson: string, entriesJson: string): string;
  isForgetRequest(text: string): boolean;
};

interface MatchCase {
  name: string;
  text: string;
  embedding: number[] | null;
  entries: Array<{ id: string; text: string; embedding?: number[] }>;
  expected: { id: string; reason: string } | null;
}

const fixture = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'forget-guard-v1.json'), 'utf-8'),
) as {
  match_cases: MatchCase[];
  request_cases: Array<{ text: string; expected: boolean }>;
};

let passed = 0;
let failed = 0;

function check(ok: boolean, name: string, detail = ''): void {
  if (ok) {
    passed++;
  } else {
    failed++;
    console.error(`FAIL ${name} ${detail}`);
  }
}

for (const c of fixture.match_cases) {
  const out = JSON.parse(
    wasm.matchForgotten(c.text, JSON.stringify(c.embedding), JSON.stringify(c.entries)),
  ) as { id: string; reason: string } | null;
  if (c.expected === null) {
    check(out === null, `match:${c.name}`, `got ${JSON.stringify(out)}`);
  } else {
    check(
      out !== null && out.id === c.expected.id && out.reason === c.expected.reason,
      `match:${c.name}`,
      `got ${JSON.stringify(out)}`,
    );
  }
}

for (const c of fixture.request_cases) {
  check(wasm.isForgetRequest(c.text) === c.expected, `request:${JSON.stringify(c.text)}`);
}

let threw = false;
try {
  wasm.matchForgotten('x', 'not json', '[]');
} catch {
  threw = true;
}
check(threw, 'malformed embedding JSON throws');

console.log(`forget-guard parity: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
