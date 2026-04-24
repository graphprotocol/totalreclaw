/**
 * lazy-load-embedding.test.ts — Regression guard for the deferred
 * onnxruntime-node / @huggingface/transformers install path introduced in
 * 3.3.1-rc.15.
 *
 * Background (install-time SIGTERM on bandwidth-constrained hosts):
 *   `openclaw plugins install @totalreclaw/totalreclaw` invoked `npm install`,
 *   which fetched `@huggingface/transformers` (direct dep) and transitively
 *   `onnxruntime-node`, whose postinstall downloads a ~216MB native binary
 *   from GitHub Releases. Slow / constrained hosts (CI containers with
 *   limited bandwidth, throttled VPNs) exceeded the plugin-install timeout
 *   and got SIGTERM'd mid-download, leaving the plugin partially installed.
 *
 *   Fix: demote `@huggingface/transformers` + `onnxruntime-node` from
 *   `dependencies` to optional peer dependencies, and convert
 *   `embedding.ts`'s static `import { AutoTokenizer, ... } from
 *   '@huggingface/transformers'` to a dynamic `await import(...)` inside the
 *   first-call code path.
 *
 *   Users who want semantic memory install the extras explicitly:
 *     npm install @huggingface/transformers
 *   Users who do not (chat-only flows) get a clean, lean install.
 *
 * This test asserts the invariants that make the lazy-load safe:
 *   1. package.json does NOT declare the heavy packages as regular deps.
 *   2. package.json DOES declare them as peer deps marked `optional: true`.
 *   3. embedding.ts uses a dynamic `import()` — NOT a static top-level
 *      runtime import — for `@huggingface/transformers`.
 *   4. A type-only `import type` from `@huggingface/transformers` is fine
 *      (erased at compile time, no runtime dep).
 *
 * Source-level text inspection is used here (rather than a child-process
 * require-cache sniff) because `embedding.ts` is an ESM module with a
 * dynamic `import()` whose behavior depends on whether the optional peer is
 * actually installed on the test host. A deterministic regex check of the
 * source is precise and unambiguous, and fails loudly if a future refactor
 * reintroduces a top-level static import.
 *
 * Run with: npx tsx lazy-load-embedding.test.ts
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let passed = 0;
let failed = 0;

function assert(condition: boolean, name: string): void {
  const n = passed + failed + 1;
  if (condition) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

// ---------------------------------------------------------------------------
// 1. package.json: heavy packages must be optional peer deps, not direct deps
// ---------------------------------------------------------------------------

const pkgPath = path.join(__dirname, 'package.json');
const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8')) as {
  dependencies?: Record<string, string>;
  peerDependencies?: Record<string, string>;
  peerDependenciesMeta?: Record<string, { optional?: boolean }>;
};

const HEAVY_PACKAGES = ['@huggingface/transformers', 'onnxruntime-node'] as const;

for (const name of HEAVY_PACKAGES) {
  assert(
    !(pkg.dependencies ?? {})[name],
    `package.json: "${name}" is NOT in "dependencies" (would force 216MB ONNX download at plugin install)`,
  );
  assert(
    !!(pkg.peerDependencies ?? {})[name],
    `package.json: "${name}" is declared in "peerDependencies"`,
  );
  assert(
    (pkg.peerDependenciesMeta ?? {})[name]?.optional === true,
    `package.json: "peerDependenciesMeta.${name}.optional" === true (npm v7+ will not auto-install)`,
  );
}

// ---------------------------------------------------------------------------
// 2. embedding.ts: no static top-level runtime import of the heavy package
// ---------------------------------------------------------------------------

const embeddingPath = path.join(__dirname, 'embedding.ts');
const embeddingSrc = fs.readFileSync(embeddingPath, 'utf8');

{
  // 2a. No top-level, non-type, static import from @huggingface/transformers.
  //     Matches lines like: import { X } from '@huggingface/transformers';
  //     but NOT: import type { X } from '@huggingface/transformers';
  //     The check is source-line-scoped so the `import type` line below
  //     doesn't trigger a false positive.
  const staticRuntimeImport = embeddingSrc
    .split('\n')
    .some(
      (line) =>
        /^\s*import\s/.test(line) &&
        !/^\s*import\s+type\s/.test(line) &&
        /from\s+['"]@huggingface\/transformers['"]/.test(line),
    );
  assert(
    !staticRuntimeImport,
    'embedding.ts does NOT have a static top-level runtime import of @huggingface/transformers',
  );
}

{
  // 2b. Must contain a dynamic `await import('@huggingface/transformers')`.
  const hasDynamicImport =
    /await\s+import\s*\(\s*['"]@huggingface\/transformers['"]\s*\)/.test(embeddingSrc);
  assert(
    hasDynamicImport,
    'embedding.ts uses a dynamic `await import("@huggingface/transformers")` (lazy-loaded on first use)',
  );
}

{
  // 2c. Regression guard: no top-level `require('@huggingface/transformers')`
  //     either — would equally defeat lazy loading.
  const hasRequire =
    /require\s*\(\s*['"]@huggingface\/transformers['"]\s*\)/.test(embeddingSrc);
  assert(
    !hasRequire,
    'embedding.ts does NOT use `require("@huggingface/transformers")` at module scope',
  );
}

{
  // 2d. Module docstring mentions the optional-peer install command so users
  //     have a pointer when they hit the "module not installed" error.
  const mentionsInstallCommand = /npm install @huggingface\/transformers/.test(embeddingSrc);
  assert(
    mentionsInstallCommand,
    'embedding.ts documents the `npm install @huggingface/transformers` opt-in command',
  );
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`# fail: ${failed}`);
console.log(`# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('SOME TESTS FAILED');
  process.exit(1);
}
console.log('ALL TESTS PASSED');
