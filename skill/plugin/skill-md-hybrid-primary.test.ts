/**
 * skill-md-hybrid-primary.test.ts (3.3.9-rc.1)
 *
 * Asserts that both skill/plugin/SKILL.md and skill/SKILL.md contain:
 *   1. The relay-based architecture assertion ("TotalReclaw is RELAY-BASED")
 *   2. The hallucination-guard denylist (all required forbidden words)
 *   3. Hybrid-primary CLI path instructions (tr CLI as primary)
 *   4. Autonomous pair call (no consent gate language)
 *   5. JSON flag documentation in CLI reference
 *
 * Run with: `npx tsx skill-md-hybrid-primary.test.ts`
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

// ---------------------------------------------------------------------------
// File paths
// ---------------------------------------------------------------------------

const pluginSkillMdPath = path.join(__dirname, 'SKILL.md');
const topLevelSkillMdPath = path.resolve(__dirname, '..', '..', 'skill', 'SKILL.md');

const pluginSkillMd = fs.readFileSync(pluginSkillMdPath, 'utf8');
const topLevelSkillMd = fs.readFileSync(topLevelSkillMdPath, 'utf8');

// ---------------------------------------------------------------------------
// 1. Relay-based architecture assertion (both files)
// ---------------------------------------------------------------------------

assert(
  pluginSkillMd.includes('TotalReclaw is RELAY-BASED'),
  'skill/plugin/SKILL.md: contains relay-based architecture assertion',
);

assert(
  topLevelSkillMd.includes('TotalReclaw is RELAY-BASED'),
  'skill/SKILL.md: contains relay-based architecture assertion',
);

assert(
  pluginSkillMd.includes('NO local-only mode'),
  'skill/plugin/SKILL.md: explicitly states NO local-only mode',
);

assert(
  topLevelSkillMd.includes('NO local-only mode'),
  'skill/SKILL.md: explicitly states NO local-only mode',
);

// ---------------------------------------------------------------------------
// 2. Hallucination-guard denylist (both files)
// ---------------------------------------------------------------------------

const REQUIRED_DENYLIST_ENTRIES = [
  '"local"',
  '"local-only"',
  '"stored locally"',
  '"on disk"',
  '"without an account"',
  '"local memory"',
  '"local storage"',
];

for (const entry of REQUIRED_DENYLIST_ENTRIES) {
  assert(
    pluginSkillMd.includes(entry),
    `skill/plugin/SKILL.md: forbidden vocabulary denylist contains ${entry}`,
  );
  assert(
    topLevelSkillMd.includes(entry),
    `skill/SKILL.md: forbidden vocabulary denylist contains ${entry}`,
  );
}

// ---------------------------------------------------------------------------
// 3. Hybrid-primary CLI path (plugin SKILL.md)
// ---------------------------------------------------------------------------

assert(
  pluginSkillMd.includes('tr-cli.js'),
  'skill/plugin/SKILL.md: references tr-cli.js as primary path',
);

assert(
  pluginSkillMd.includes('hybrid-primary'),
  'skill/plugin/SKILL.md: mentions hybrid-primary architecture',
);

assert(
  pluginSkillMd.includes('Never rely on `totalreclaw_*` tool binding'),
  'skill/plugin/SKILL.md: instructs agent NOT to rely on tool binding',
);

assert(
  pluginSkillMd.includes('status --json'),
  'skill/plugin/SKILL.md: CLI reference includes status --json',
);

assert(
  pluginSkillMd.includes('pair --json'),
  'skill/plugin/SKILL.md: CLI reference includes pair --json',
);

assert(
  pluginSkillMd.includes('remember --json'),
  'skill/plugin/SKILL.md: CLI reference includes remember --json',
);

assert(
  pluginSkillMd.includes('recall --json'),
  'skill/plugin/SKILL.md: CLI reference includes recall --json',
);

// ---------------------------------------------------------------------------
// 4. Autonomous pair call — no consent gate language
// ---------------------------------------------------------------------------

// The plugin SKILL.md must say "no consent gate" or "UNCONDITIONAL"
assert(
  pluginSkillMd.includes('no consent gate') || pluginSkillMd.includes('UNCONDITIONAL'),
  'skill/plugin/SKILL.md: pair step is unconditional (no consent gate)',
);

// The SKILL.md must NOT instruct the agent TO ask permission before pairing.
// It's acceptable for consent-gate phrases to appear in "Do NOT ask X" examples.
// Patterns that would only appear if the agent was told TO ask:
const STRICTLY_FORBIDDEN_CONSENT_PATTERNS = [
  'Should I set up an account?',
  'Do you want me to pair',
  'Shall I pair',
  'Ask the user before pairing',
];
for (const pattern of STRICTLY_FORBIDDEN_CONSENT_PATTERNS) {
  assert(
    !pluginSkillMd.includes(pattern),
    `skill/plugin/SKILL.md: does NOT instruct agent to ask: "${pattern}"`,
  );
}

// ---------------------------------------------------------------------------
// 5. JSON output shapes documented
// ---------------------------------------------------------------------------

// status --json shape
assert(
  pluginSkillMd.includes('"hybrid_mode"') || pluginSkillMd.includes('hybrid_mode'),
  'skill/plugin/SKILL.md: documents hybrid_mode field in status --json output',
);

// pair --json shape — url and pin keys
assert(
  pluginSkillMd.includes('"url"') && pluginSkillMd.includes('"pin"'),
  'skill/plugin/SKILL.md: documents url and pin keys in pair --json output',
);

// recall --json shape — results key
assert(
  pluginSkillMd.includes('"results"') && pluginSkillMd.includes('"score"'),
  'skill/plugin/SKILL.md: documents results and score keys in recall --json output',
);

// ---------------------------------------------------------------------------
// 6. Setup flow: CLI verify BEFORE tool-binding check
// ---------------------------------------------------------------------------

// The setup flow must NOT tell agent to wait for tool binding after install
const FORBIDDEN_TOOLBIND_WAIT = [
  'wait for tool-bind',
  'confirm `totalreclaw_pair` is in your toolset',
  'confirm that `totalreclaw_pair` is bound',
];
for (const pattern of FORBIDDEN_TOOLBIND_WAIT) {
  assert(
    !pluginSkillMd.includes(pattern),
    `skill/plugin/SKILL.md: does NOT contain tool-binding wait: "${pattern}"`,
  );
}

// ---------------------------------------------------------------------------
// 7. Version consistency in plugin SKILL.md frontmatter
// ---------------------------------------------------------------------------

assert(
  /^version: 3\.3\.(9|1\d)-rc\./m.test(pluginSkillMd),
  'skill/plugin/SKILL.md: frontmatter version is 3.3.9-rc.N or 3.3.1[0-9]-rc.N',
);

// ---------------------------------------------------------------------------

console.log(`\n# ${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
