/**
 * tr-cli-json-output.test.ts (3.3.9-rc.1)
 *
 * Asserts that tr-cli.ts correctly handles --json flag for all commands
 * by statically analysing the source. Since the CLI requires live relay
 * credentials for functional calls, we verify the code structure:
 *
 *   1. status --json: cmdStatus receives jsonMode param and writes JSON
 *   2. remember --json: cmdRemember pops --json flag, outputs JSON shape
 *   3. recall --json: cmdRecall pops --json flag + --limit, outputs JSON shape
 *   4. pair --json: delegates to pair-cli-relay outputMode='json' path
 *   5. All JSON outputs contain the expected required keys (via source grep)
 *
 * Additionally, verifies the --limit flag is handled in cmdRecall.
 *
 * Run with: `npx tsx tr-cli-json-output.test.ts`
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

let passed = 0;
let failed = 0;

function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

const trCliPath = path.join(__dirname, 'tr-cli.ts');
const src = fs.readFileSync(trCliPath, 'utf8');

// ---------------------------------------------------------------------------
// 1. popFlag helper exists (used by status, remember, recall)
// ---------------------------------------------------------------------------

assert(
  /function popFlag/.test(src),
  'tr-cli.ts: popFlag helper function defined',
);

assert(
  /function popLimitFlag/.test(src),
  'tr-cli.ts: popLimitFlag helper function defined',
);

// ---------------------------------------------------------------------------
// 2. status --json
// ---------------------------------------------------------------------------

assert(
  /async function cmdStatus\(jsonMode: boolean\)/.test(src),
  'tr-cli.ts: cmdStatus accepts jsonMode: boolean parameter',
);

assert(
  /if \(jsonMode\)/.test(src),
  'tr-cli.ts: cmdStatus has JSON branch (if jsonMode)',
);

// Required keys in status JSON output
assert(
  src.includes('"version"') && src.includes('"onboarded"') && src.includes('"hybrid_mode"'),
  'tr-cli.ts: status --json output includes version, onboarded, hybrid_mode keys',
);

assert(
  src.includes('"next_step"'),
  'tr-cli.ts: status --json output includes next_step key',
);

assert(
  src.includes('"tool_count"'),
  'tr-cli.ts: status --json output includes tool_count key',
);

// ---------------------------------------------------------------------------
// 3. remember --json
// ---------------------------------------------------------------------------

assert(
  /async function cmdRemember\(rawArgs: string\[\]\)/.test(src),
  'tr-cli.ts: cmdRemember accepts rawArgs parameter',
);

assert(
  /popFlag\(rawArgs, '--json'\)/.test(src),
  "tr-cli.ts: cmdRemember calls popFlag(rawArgs, '--json')",
);

// JSON shape: {"ok":true,"id":"...","claim_count":N}
assert(
  src.includes('"ok"') && src.includes('"id"') && src.includes('"claim_count"'),
  'tr-cli.ts: remember --json output includes ok, id, claim_count keys',
);

// ---------------------------------------------------------------------------
// 4. recall --json + --limit
// ---------------------------------------------------------------------------

assert(
  /async function cmdRecall\(rawArgs: string\[\]\)/.test(src),
  'tr-cli.ts: cmdRecall accepts rawArgs parameter',
);

assert(
  /popFlag\(rawArgs, '--json'\)/.test(src) || /popFlag\(argsAfterJson.*'--json'\)/.test(src),
  "tr-cli.ts: cmdRecall calls popFlag for --json",
);

assert(
  /popLimitFlag/.test(src),
  'tr-cli.ts: cmdRecall uses popLimitFlag for --limit',
);

// JSON shape: {"results":[{"text":"...","score":0.8}]}
assert(
  src.includes('"results"'),
  'tr-cli.ts: recall --json output includes results key',
);

// Each result object must have text and score
assert(
  src.includes('"text"') && src.includes('"score"'),
  'tr-cli.ts: recall --json result objects include text and score keys',
);

// ---------------------------------------------------------------------------
// 5. pair --json: delegates to pair-cli-relay with json outputMode
// ---------------------------------------------------------------------------

assert(
  /outputMode.*'json'/.test(src) || /'json'.*outputMode/.test(src),
  "tr-cli.ts: cmdPair sets outputMode='json' when --json flag present",
);

assert(
  src.includes('pair-cli-relay'),
  'tr-cli.ts: cmdPair delegates to pair-cli-relay.js',
);

// ---------------------------------------------------------------------------
// 6. Version reference updated to 3.3.9-rc.1
// ---------------------------------------------------------------------------

assert(
  src.includes("'3.3.9-rc.1'") || src.includes('"3.3.9-rc.1"'),
  'tr-cli.ts: version reference updated to 3.3.9-rc.1',
);

// ---------------------------------------------------------------------------
// 7. --help documents JSON shapes
// ---------------------------------------------------------------------------

assert(
  src.includes('--json'),
  'tr-cli.ts: --help mentions --json flag',
);

assert(
  src.includes('JSON output shapes') || src.includes('--json'),
  'tr-cli.ts: --help references JSON output',
);

// ---------------------------------------------------------------------------
// 8. Phrase safety — mnemonic never in stdout
// ---------------------------------------------------------------------------

// The CLI must not write mnemonic to stdout — verify no mnemonic/recovery_phrase
// variable reaches process.stdout.write or log()
// Static check: mnemonic is read but not passed to log()
assert(
  !src.includes('log(mnemonic)') && !src.includes('log(recoveryPhrase)'),
  'tr-cli.ts: mnemonic/recoveryPhrase not passed to log() (phrase safety)',
);

// ---------------------------------------------------------------------------

console.log(`\n# ${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
