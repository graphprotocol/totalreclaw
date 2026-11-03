/**
 * Topical dedup-context cross-language parity test (TypeScript / WASM side).
 *
 * PRD-04 DEP-3. Loads `fixtures/dedup-context-v1.json` (written by
 * `fixtures/generate-dedup-context-v1.py`) and asserts that the WASM export
 * `buildDedupContext` returns every case's `expected` string byte-for-byte.
 * Siblings: Rust `rust/totalreclaw-core/src/dedup_context.rs`
 * (`tests::parity_fixture_vectors`) and Python
 * `python/tests/test_dedup_context_parity.py`.
 *
 * Run (build the flat WASM pkg first):
 *   cd rust/totalreclaw-core && wasm-pack build --target nodejs --out-dir pkg --features wasm
 *   cd tests/parity && npx tsx dedup-context-parity.test.ts
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
  buildDedupContext: (
    topicalJson: string,
    pinnedJson: string,
    recentJson: string,
    cap?: number | null,
  ) => string;
};

interface Item {
  id: string;
  text: string;
  [extra: string]: unknown;
}

interface Case {
  name: string;
  topical: Item[];
  pinned: Item[];
  recent: Item[];
  cap: number;
  expected: string;
}

interface Fixture {
  version: number;
  header: string;
  default_cap: number;
  cases: Case[];
}

const HEADER =
  'Existing memories (use these for dedup — classify as UPDATE/DELETE/NOOP if they conflict or overlap):';

const fixture: Fixture = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'dedup-context-v1.json'), 'utf8'),
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

check(fixture.header === HEADER, 'fixture header is the pre-DEP-3 header');
check(fixture.cases.length === 16, 'fixture carries 16 cases', ` — got ${fixture.cases.length}`);

for (const c of fixture.cases) {
  const actual = wasm.buildDedupContext(
    JSON.stringify(c.topical),
    JSON.stringify(c.pinned),
    JSON.stringify(c.recent),
    c.cap,
  );
  check(
    actual === c.expected,
    `case ${c.name}`,
    `\n  actual:   ${JSON.stringify(actual)}\n  expected: ${JSON.stringify(c.expected)}`,
  );
  if (c.expected !== '') {
    check(actual.startsWith(HEADER + '\n'), `case ${c.name}: starts with the header`);
  }
}

check(wasm.buildDedupContext('null', 'null', 'null', 30) === '', 'null sections are treated as empty');

function throws(fn: () => unknown): boolean {
  try {
    fn();
  } catch {
    return true;
  }
  return false;
}

check(throws(() => wasm.buildDedupContext('not json', '[]', '[]', 30)), 'malformed topical JSON throws');

// cap: omitted / null -> default 30. Anything that is not a non-negative
// integer throws instead of being coerced (a usize parameter turned -1 into
// "no cap" and NaN into an empty block).
const forty = JSON.stringify(Array.from({ length: 40 }, (_, n) => ({ id: `t${n}`, text: `fact ${n}` })));
for (const cap of [undefined, null]) {
  const lines = wasm.buildDedupContext(forty, '[]', '[]', cap).split('\n').length - 1;
  check(lines === 30, `cap ${cap} defaults to 30 lines`, ` — got ${lines}`);
}
for (const cap of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
  check(throws(() => wasm.buildDedupContext(forty, '[]', '[]', cap)), `cap ${cap} throws`);
}

console.log('\n========================================');
console.log(`dedup-context TS/WASM parity: ${passed}/${passed + failed} assertions passed`);
console.log('========================================');
if (failed > 0) {
  console.error(`FAIL: ${failed} assertion(s) failed`);
  process.exit(1);
}
console.log('PASS: dedup-context-v1 vectors match between Rust (canonical) and WASM');
