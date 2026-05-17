/**
 * Regression guard for the ops-3 batched-UserOp restoration.
 *
 * Background: in 3.3.1-rc.* a Base Sepolia gas-estimation bug forced us to
 * submit subgraph payloads ONE FACT PER UserOp inside `storeExtractedFacts`.
 * With the move to single-chain Gnosis (ops-3, May 2026), the loop was
 * removed and the batched executeBatch path restored — a single
 * `submitFactBatchOnChain(pendingPayloads, batchConfig)` call handles all
 * pending facts.
 *
 * The after-fix shape is structural: there must be exactly one batched call,
 * inside `storeExtractedFacts`, that passes the full `pendingPayloads` array
 * (not a slice / single-element wrapper), with no enclosing `for`/`while`
 * loop driving repeated calls. If this regresses, every multi-fact extraction
 * silently degrades back to N-UserOps-per-extraction (slower, higher gas, AA25
 * exposure).
 *
 * Why structural rather than behavioral:
 * `storeExtractedFacts` is ~400 LOC with static ESM imports of LLM, encryption,
 * billing, relay and subgraph dependencies. The repo's test runner is plain
 * `npx tsx` — no jest / vitest module mocking. A behavioral test would need
 * either a refactor to dependency-injected callers or a mock-aware harness.
 * Both are out of scope for ops-3. Until that infrastructure lands, this
 * static guard pins the after-restoration shape.
 *
 * Run with: npx tsx storeextractedfacts-batched-submission.test.ts
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const INDEX_PATH = resolve(__dirname, 'index.ts');
const SRC = readFileSync(INDEX_PATH, 'utf8');

let passed = 0;
let failed = 0;

function assert(cond: boolean, name: string, detail?: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}${detail ? ` — ${detail}` : ''}`);
    failed++;
  }
}

// ---------------------------------------------------------------------------
// Extract the body of `storeExtractedFacts` by brace-matching from its signature.
// ---------------------------------------------------------------------------

function extractFunctionBody(src: string, fnName: string): string {
  const sigRe = new RegExp(`(?:async\\s+)?function\\s+${fnName}\\s*\\(`);
  const sigMatch = sigRe.exec(src);
  if (!sigMatch) throw new Error(`function ${fnName} not found in index.ts`);

  // Find the first `{` after the signature
  let i = sigMatch.index + sigMatch[0].length;
  while (i < src.length && src[i] !== '{') i++;
  if (i >= src.length) throw new Error(`open brace of ${fnName} not found`);
  const start = i;

  // Brace-match. Naive: assumes no `{` inside strings/regex in this range.
  // The function body in index.ts uses only standard control flow + template
  // strings — template-string `${}` interpolation contributes balanced braces,
  // so a simple depth counter is sufficient here.
  let depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start + 1, i);
    }
  }
  throw new Error(`closing brace of ${fnName} not found`);
}

const body = extractFunctionBody(SRC, 'storeExtractedFacts');

// ---------------------------------------------------------------------------
// 1. `submitFactBatchOnChain` is called exactly once inside storeExtractedFacts.
// ---------------------------------------------------------------------------

const callRe = /\bsubmitFactBatchOnChain\s*\(/g;
const callCount = (body.match(callRe) ?? []).length;
assert(
  callCount === 1,
  'storeExtractedFacts calls submitFactBatchOnChain exactly once',
  `actual call count: ${callCount}`,
);

// ---------------------------------------------------------------------------
// 2. The single call passes the FULL `pendingPayloads` identifier, not a slice
//    or single-element wrapper like `[pendingPayloads[i]]`.
// ---------------------------------------------------------------------------

const callArgsRe = /submitFactBatchOnChain\s*\(\s*([^,)]+)/;
const argsMatch = callArgsRe.exec(body);
const firstArg = argsMatch ? argsMatch[1].trim() : '';
assert(
  firstArg === 'pendingPayloads',
  'first arg is the full pendingPayloads array (not a slice/wrapper)',
  `actual first arg: "${firstArg}"`,
);

// Negative-form: no per-fact slice literal like `[pendingPayloads[…]]` or
// `pendingPayloads.slice(…)` is passed into submitFactBatchOnChain. This
// would catch a re-introduction of single-fact batches even if the call
// count stayed at 1 (e.g. inside a loop with one call site).
const sliceWrapperRe = /submitFactBatchOnChain\s*\(\s*\[\s*pendingPayloads\s*\[/;
assert(
  !sliceWrapperRe.test(body),
  'no [pendingPayloads[i]] single-element wrapper passed to submitFactBatchOnChain',
);
const sliceMethodRe = /submitFactBatchOnChain\s*\(\s*pendingPayloads\s*\.\s*slice\s*\(/;
assert(
  !sliceMethodRe.test(body),
  'no pendingPayloads.slice(...) passed to submitFactBatchOnChain',
);

// ---------------------------------------------------------------------------
// 3. There is no `for` or `while` loop driving the submit call. Specifically,
//    the call must NOT live inside a loop that iterates over pendingPayloads.
//    We detect this by finding the call's character offset in `body` and
//    walking upward — if the nearest enclosing block (depth-balanced) is
//    preceded by `for (` or `while (`, that's the regression we're guarding.
// ---------------------------------------------------------------------------

const callIdx = body.search(callRe);
assert(callIdx >= 0, 'located submitFactBatchOnChain call site for loop check');

function findEnclosingBlockOpen(src: string, idx: number): number {
  let depth = 0;
  for (let i = idx; i >= 0; i--) {
    if (src[i] === '}') depth++;
    else if (src[i] === '{') {
      if (depth === 0) return i;
      depth--;
    }
  }
  return -1;
}

// Walk up two levels: the call sits inside a try { ... } inside an
// if (pendingPayloads.length > 0 && isSubgraphMode()) { ... } block.
// A regression to the loop form would put a `for` or `while` between those.
let cursor = callIdx;
let foundLoop = false;
for (let level = 0; level < 4 && cursor >= 0; level++) {
  const open = findEnclosingBlockOpen(body, cursor - 1);
  if (open < 0) break;
  // Look at the ~80 chars immediately before the open brace for a loop keyword.
  const prelude = body.slice(Math.max(0, open - 80), open);
  if (/\b(for|while)\s*\(/.test(prelude)) {
    foundLoop = true;
    break;
  }
  cursor = open;
}
assert(
  !foundLoop,
  'submitFactBatchOnChain call is not nested inside a for/while loop',
);

// ---------------------------------------------------------------------------
// 4. The success path increments `stored` by the FULL `pendingPayloads.length`,
//    not by `1` or by a per-iteration slice length. This guards against a
//    half-revert where the loop is removed but accounting still increments by
//    one — which would mis-report storage counts to callers.
// ---------------------------------------------------------------------------

const storedIncRe = /stored\s*\+=\s*pendingPayloads\s*\.\s*length/;
assert(
  storedIncRe.test(body),
  'success branch increments stored by pendingPayloads.length (full-batch accounting)',
);

// ---------------------------------------------------------------------------
// 5. The deleted Base-Sepolia gas-estimation comment must NOT be reintroduced
//    in storeExtractedFacts. Its presence would signal someone copy-pasted the
//    old loop back in.
// ---------------------------------------------------------------------------

assert(
  !/Base Sepolia/i.test(body),
  'no "Base Sepolia" reference in storeExtractedFacts (single-chain Gnosis only)',
);
assert(
  !/single-call UserOps/i.test(body),
  'no "single-call UserOps" comment in storeExtractedFacts',
);

// ---------------------------------------------------------------------------
// 6. Sanity: the import of submitFactBatchOnChain from subgraph-store still
//    exists (catches the case where someone removes the import but leaves the
//    call — TS would catch it, but if the imports are reshuffled this asserts
//    the after-fix shape).
// ---------------------------------------------------------------------------

assert(
  /from\s+['"]\.\/subgraph-store\.js['"];?[\s\S]*submitFactBatchOnChain/.test(SRC) ||
    /submitFactBatchOnChain[\s\S]*from\s+['"]\.\/subgraph-store\.js['"]/.test(SRC),
  'submitFactBatchOnChain is imported from ./subgraph-store.js',
);

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('SOME TESTS FAILED');
  process.exit(1);
}
console.log('ALL TESTS PASSED');
