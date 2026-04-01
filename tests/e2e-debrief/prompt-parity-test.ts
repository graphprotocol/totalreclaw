// Run: npx tsx tests/e2e-debrief/prompt-parity-test.ts
//
// Test 4: Verifies that DEBRIEF_SYSTEM_PROMPT is identical across all 5 implementations:
//   1. TypeScript MCP server     (mcp/src/tools/debrief.ts)
//   2. TypeScript OpenClaw plugin (skill/plugin/extractor.ts)
//   3. TypeScript NanoClaw        (skill-nanoclaw/src/extraction/prompts.ts)
//   4. Python Hermes plugin       (python/src/totalreclaw/hermes/debrief.py)
//   5. Rust ZeroClaw              (rust/totalreclaw-memory/src/debrief.rs)
//
// Normalizes whitespace (trim, collapse multiple spaces/newlines) before comparison.
// Offline test -- no network required.

import { execSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// ---------------------------------------------------------------------------
// Test runner
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

async function runTest(name: string, fn: () => Promise<void>): Promise<boolean> {
  try {
    await fn();
    console.log(`  [PASS] ${name}`);
    passed++;
    return true;
  } catch (err: any) {
    console.log(`  [FAIL] ${name} -- ${err.message}`);
    failed++;
    return false;
  }
}

// ---------------------------------------------------------------------------
// Normalize helper
// ---------------------------------------------------------------------------

/** Trim and collapse multiple spaces/newlines to single space for comparison. */
function normalize(s: string): string {
  return s.trim().replace(/\s+/g, ' ');
}

// ---------------------------------------------------------------------------
// Prompt extraction helpers
// ---------------------------------------------------------------------------

const ROOT = resolve(__dirname, '..', '..');

/** 1-3: TypeScript imports (dynamic import -- tsx handles TS transpilation) */
async function getTSPrompt(modulePath: string, exportName: string): Promise<string> {
  const mod = await import(modulePath);
  const prompt: string = mod[exportName];
  assert(typeof prompt === 'string', `${modulePath} did not export a string for ${exportName}`);
  return prompt;
}

/** 4: Python -- run via subprocess with venv */
function getPythonPrompt(): string {
  const pythonBin = resolve(ROOT, 'python', '.venv', 'bin', 'python3');
  const cmd = `${pythonBin} -c "from totalreclaw.hermes.debrief import DEBRIEF_SYSTEM_PROMPT; print(DEBRIEF_SYSTEM_PROMPT)"`;

  const result = execSync(cmd, {
    cwd: resolve(ROOT, 'python'),
    encoding: 'utf-8',
    timeout: 15_000,
    shell: '/bin/bash',
  });

  return result;
}

/** 5: Rust -- extract the raw string between r#"..."# from source file */
function getRustPrompt(): string {
  const rsPath = resolve(ROOT, 'rust', 'totalreclaw-memory', 'src', 'debrief.rs');
  const source = readFileSync(rsPath, 'utf-8');

  const startMarker = 'pub const DEBRIEF_SYSTEM_PROMPT: &str = r#"';
  const startIdx = source.indexOf(startMarker);
  assert(startIdx !== -1, 'Could not find DEBRIEF_SYSTEM_PROMPT start in debrief.rs');

  const contentStart = startIdx + startMarker.length;
  const endMarker = '"#;';
  const endIdx = source.indexOf(endMarker, contentStart);
  assert(endIdx !== -1, 'Could not find closing "#; in debrief.rs');

  return source.slice(contentStart, endIdx);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('Test 4: DEBRIEF_SYSTEM_PROMPT Parity\n');

  // --- Collect all 5 prompts ---

  // 1. TypeScript MCP
  const mcpPrompt = await getTSPrompt(
    resolve(ROOT, 'mcp', 'src', 'tools', 'debrief.ts'),
    'DEBRIEF_SYSTEM_PROMPT',
  );

  // 2. TypeScript OpenClaw Plugin
  const pluginPrompt = await getTSPrompt(
    resolve(ROOT, 'skill', 'plugin', 'extractor.ts'),
    'DEBRIEF_SYSTEM_PROMPT',
  );

  // 3. TypeScript NanoClaw
  const nanoClawPrompt = await getTSPrompt(
    resolve(ROOT, 'skill-nanoclaw', 'src', 'extraction', 'prompts.ts'),
    'DEBRIEF_SYSTEM_PROMPT',
  );

  // 4. Python Hermes
  const pythonPrompt = getPythonPrompt();

  // 5. Rust ZeroClaw
  const rustPrompt = getRustPrompt();

  // Normalize all
  const normalizedMcp = normalize(mcpPrompt);
  const normalizedPlugin = normalize(pluginPrompt);
  const normalizedNanoClaw = normalize(nanoClawPrompt);
  const normalizedPython = normalize(pythonPrompt);
  const normalizedRust = normalize(rustPrompt);

  // --- Pairwise comparisons (MCP is canonical reference) ---

  await runTest('MCP prompt matches OpenClaw Plugin prompt', async () => {
    assert(
      normalizedMcp === normalizedPlugin,
      `MCP and Plugin prompts differ.\n` +
        `  MCP (normalized, ${normalizedMcp.length} chars): "${normalizedMcp.slice(0, 80)}..."\n` +
        `  Plugin (normalized, ${normalizedPlugin.length} chars): "${normalizedPlugin.slice(0, 80)}..."`,
    );
  });

  await runTest('MCP prompt matches NanoClaw prompt', async () => {
    assert(
      normalizedMcp === normalizedNanoClaw,
      `MCP and NanoClaw prompts differ.\n` +
        `  MCP length: ${normalizedMcp.length}\n` +
        `  NanoClaw length: ${normalizedNanoClaw.length}`,
    );
  });

  await runTest('MCP prompt matches Python (Hermes) prompt', async () => {
    assert(
      normalizedMcp === normalizedPython,
      `MCP and Python prompts differ.\n` +
        `  MCP length: ${normalizedMcp.length}\n` +
        `  Python length: ${normalizedPython.length}`,
    );
  });

  await runTest('MCP prompt matches Rust (ZeroClaw) prompt', async () => {
    assert(
      normalizedMcp === normalizedRust,
      `MCP and Rust prompts differ.\n` +
        `  MCP length: ${normalizedMcp.length}\n` +
        `  Rust length: ${normalizedRust.length}`,
    );
  });

  // Cross-language pairs
  await runTest('Python prompt matches Rust prompt', async () => {
    assert(
      normalizedPython === normalizedRust,
      `Python and Rust prompts differ.\n` +
        `  Python length: ${normalizedPython.length}\n` +
        `  Rust length: ${normalizedRust.length}`,
    );
  });

  // --- Structural checks ---

  await runTest('All prompts contain {already_stored_facts} placeholder', async () => {
    const placeholder = '{already_stored_facts}';
    assert(mcpPrompt.includes(placeholder), 'MCP prompt missing {already_stored_facts}');
    assert(pluginPrompt.includes(placeholder), 'Plugin prompt missing {already_stored_facts}');
    assert(nanoClawPrompt.includes(placeholder), 'NanoClaw prompt missing {already_stored_facts}');
    assert(pythonPrompt.includes(placeholder), 'Python prompt missing {already_stored_facts}');
    assert(rustPrompt.includes(placeholder), 'Rust prompt missing {already_stored_facts}');
  });

  await runTest('All prompts contain key instructional sections', async () => {
    const keyPhrases = [
      'Broader context',
      'Outcomes & conclusions',
      'What was attempted',
      'Relationships',
      'Open threads',
      'Maximum 5 items',
      'summary|context',
    ];

    for (const phrase of keyPhrases) {
      assert(mcpPrompt.includes(phrase), `MCP prompt missing phrase: "${phrase}"`);
      assert(pluginPrompt.includes(phrase), `Plugin prompt missing phrase: "${phrase}"`);
      assert(nanoClawPrompt.includes(phrase), `NanoClaw prompt missing phrase: "${phrase}"`);
      assert(pythonPrompt.includes(phrase), `Python prompt missing phrase: "${phrase}"`);
      assert(rustPrompt.includes(phrase), `Rust prompt missing phrase: "${phrase}"`);
    }
  });

  // --- Summary ---
  console.log(`\n${'='.repeat(60)}`);
  console.log(`  Results: ${passed} passed, ${failed} failed`);
  console.log(`${'='.repeat(60)}`);

  if (failed > 0) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
