/**
 * Phase 2.2.6: cross-package parity test for `VALID_MEMORY_TYPES`.
 *
 * Reads the constant declaration from each source file as text and parses
 * it via regex, then compares all three (plugin TS + MCP TS + Python) against
 * a hardcoded canonical list. This avoids cross-package module-resolution
 * gymnastics (CJS-vs-ESM, .js-extension imports under tsx, etc.) at the cost
 * of being mildly hacky.
 *
 * Drift between any of these produces silent cross-client divergence: a
 * rule-typed fact stored by the plugin would deserialize as unknown if MCP
 * or Python's list is missing "rule", or a tool schema enum would silently
 * miss a category.
 *
 * The Python equivalent lives at `python/tests/test_memory_types_parity.py`
 * and additionally verifies the Rust core `canonicalize_claim` accepts every
 * type's short form via the PyO3 bindings.
 *
 * **The eventual fix** is to move `VALID_MEMORY_TYPES` into Rust core and
 * have all language bindings consume from there (see `core-hoist-backlog.md`
 * Tier 2, items 5/6/8). This parity test is the stopgap until that hoist
 * lands — at which point it can be deleted.
 *
 * Run with:
 *   cd tests/parity && npx tsx memory-types-parity.test.ts
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..');

const CANONICAL_TYPES = [
  'fact',
  'preference',
  'decision',
  'episodic',
  'goal',
  'context',
  'summary',
  'rule',
] as const;

let passed = 0;
let failed = 0;

function assert(condition: boolean, name: string, detail?: string): void {
  const n = passed + failed + 1;
  if (condition) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    if (detail) console.log(`  ${detail}`);
    failed++;
  }
}

/**
 * Parse a `VALID_MEMORY_TYPES = [...]` array literal from a TypeScript source
 * file. Returns the list of string values (in order).
 */
function parseTSConstantList(filePath: string, varName: string): string[] {
  const content = readFileSync(filePath, 'utf-8');
  const re = new RegExp(
    `(?:export\\s+)?const\\s+${varName}\\s*[^=]*=\\s*\\[([^\\]]+)\\]`,
    'm',
  );
  const m = content.match(re);
  if (!m) throw new Error(`Could not find ${varName} declaration in ${filePath}`);
  const inner = m[1];
  const items: string[] = [];
  for (const sm of inner.matchAll(/['"]([^'"]+)['"]/g)) {
    items.push(sm[1]);
  }
  return items;
}

/**
 * Parse a `VALID_MEMORY_TYPES: tuple[str, ...] = (...)` tuple literal from a
 * Python source file. Returns the list of string values (in order).
 */
function parsePythonConstantList(filePath: string, varName: string): string[] {
  const content = readFileSync(filePath, 'utf-8');
  const re = new RegExp(
    `${varName}\\s*[^=]*=\\s*\\(([^)]+)\\)`,
    'm',
  );
  const m = content.match(re);
  if (!m) throw new Error(`Could not find ${varName} declaration in ${filePath}`);
  const inner = m[1];
  const items: string[] = [];
  for (const sm of inner.matchAll(/['"]([^'"]+)['"]/g)) {
    items.push(sm[1]);
  }
  return items;
}

const pluginList = parseTSConstantList(
  resolve(REPO_ROOT, 'skill/plugin/extractor.ts'),
  'VALID_MEMORY_TYPES',
);
const mcpList = parseTSConstantList(
  resolve(REPO_ROOT, 'mcp/src/memory-types.ts'),
  'VALID_MEMORY_TYPES',
);
const pythonList = parsePythonConstantList(
  resolve(REPO_ROOT, 'python/src/totalreclaw/agent/extraction.py'),
  'VALID_MEMORY_TYPES',
);

assert(
  pluginList.length === CANONICAL_TYPES.length,
  `plugin: length matches canonical (${CANONICAL_TYPES.length})`,
  `got ${pluginList.length}: ${JSON.stringify(pluginList)}`,
);
for (let i = 0; i < CANONICAL_TYPES.length; i++) {
  assert(
    pluginList[i] === CANONICAL_TYPES[i],
    `plugin: index ${i} is '${CANONICAL_TYPES[i]}'`,
    `got '${pluginList[i]}'`,
  );
}

assert(
  mcpList.length === CANONICAL_TYPES.length,
  `mcp: length matches canonical (${CANONICAL_TYPES.length})`,
  `got ${mcpList.length}: ${JSON.stringify(mcpList)}`,
);
for (let i = 0; i < CANONICAL_TYPES.length; i++) {
  assert(
    mcpList[i] === CANONICAL_TYPES[i],
    `mcp: index ${i} is '${CANONICAL_TYPES[i]}'`,
    `got '${mcpList[i]}'`,
  );
}

assert(
  pythonList.length === CANONICAL_TYPES.length,
  `python: length matches canonical (${CANONICAL_TYPES.length})`,
  `got ${pythonList.length}: ${JSON.stringify(pythonList)}`,
);
for (let i = 0; i < CANONICAL_TYPES.length; i++) {
  assert(
    pythonList[i] === CANONICAL_TYPES[i],
    `python: index ${i} is '${CANONICAL_TYPES[i]}'`,
    `got '${pythonList[i]}'`,
  );
}

assert(
  JSON.stringify(pluginList) === JSON.stringify(mcpList),
  'plugin + mcp: byte-identical',
);
assert(
  JSON.stringify(pluginList) === JSON.stringify(pythonList),
  'plugin + python: byte-identical',
);

console.log(`\n# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('\nMEMORY TYPES PARITY DRIFT DETECTED');
  console.log('See core-hoist-backlog.md tier 2 — these duplications go away when the constants live in Rust core.');
  process.exit(1);
} else {
  console.log('\nALL PARITY CHECKS PASSED');
  process.exit(0);
}

export {};
