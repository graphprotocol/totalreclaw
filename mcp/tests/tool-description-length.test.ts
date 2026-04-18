/**
 * tool-description-length.test.ts — Phase 2.3 audit.
 *
 * Enforces the compression contract introduced by the v3.0.0 audit:
 *
 * 1. Every externally-visible MCP tool description is ≤ 500 characters
 *    (per-turn LLM context cost budget).
 * 2. Every description contains an `INVOKE WHEN` marker so the LLM can
 *    auto-route user turns to the right tool.
 * 3. Every description contains a disambiguation guard
 *    (`WHEN NOT TO USE` / `NOT FOR` / similar) so the LLM knows when to
 *    decline.
 *
 * A regression on any of these = CI fail. If you need to grow a
 * description above 500 chars, compress elsewhere or move the material
 * into SERVER_INSTRUCTIONS (the persistent system-prompt fragment).
 */

import {
  REMEMBER_TOOL_DESCRIPTION,
  RECALL_TOOL_DESCRIPTION,
  FORGET_TOOL_DESCRIPTION,
  EXPORT_TOOL_DESCRIPTION,
  STATUS_TOOL_DESCRIPTION,
  UPGRADE_TOOL_DESCRIPTION,
  MIGRATE_TOOL_DESCRIPTION,
  IMPORT_FROM_TOOL_DESCRIPTION,
  IMPORT_TOOL_DESCRIPTION,
  SUPPORT_TOOL_DESCRIPTION,
  ACCOUNT_TOOL_DESCRIPTION,
} from '../src/prompts';

import { pinToolDefinition, unpinToolDefinition } from '../src/tools/pin';
import { retypeToolDefinition } from '../src/tools/retype';
import { setScopeToolDefinition } from '../src/tools/set-scope';
import { consolidateToolDefinition } from '../src/tools/consolidate';
import { debriefToolDefinition } from '../src/tools/debrief';
import { importBatchToolDefinition } from '../src/tools/import-batch';

// Collected set of every description the LLM ever sees. If a new tool
// is added, append it here — the test file is the enforcement layer.
const DESCRIPTIONS: Array<{ name: string; text: string }> = [
  { name: 'REMEMBER_TOOL_DESCRIPTION', text: REMEMBER_TOOL_DESCRIPTION },
  { name: 'RECALL_TOOL_DESCRIPTION', text: RECALL_TOOL_DESCRIPTION },
  { name: 'FORGET_TOOL_DESCRIPTION', text: FORGET_TOOL_DESCRIPTION },
  { name: 'EXPORT_TOOL_DESCRIPTION', text: EXPORT_TOOL_DESCRIPTION },
  { name: 'STATUS_TOOL_DESCRIPTION', text: STATUS_TOOL_DESCRIPTION },
  { name: 'UPGRADE_TOOL_DESCRIPTION', text: UPGRADE_TOOL_DESCRIPTION },
  { name: 'MIGRATE_TOOL_DESCRIPTION', text: MIGRATE_TOOL_DESCRIPTION },
  { name: 'IMPORT_FROM_TOOL_DESCRIPTION', text: IMPORT_FROM_TOOL_DESCRIPTION },
  { name: 'IMPORT_TOOL_DESCRIPTION', text: IMPORT_TOOL_DESCRIPTION },
  { name: 'SUPPORT_TOOL_DESCRIPTION', text: SUPPORT_TOOL_DESCRIPTION },
  { name: 'ACCOUNT_TOOL_DESCRIPTION', text: ACCOUNT_TOOL_DESCRIPTION },
  { name: 'pinToolDefinition.description', text: pinToolDefinition.description },
  { name: 'unpinToolDefinition.description', text: unpinToolDefinition.description },
  { name: 'retypeToolDefinition.description', text: retypeToolDefinition.description },
  { name: 'setScopeToolDefinition.description', text: setScopeToolDefinition.description },
  { name: 'consolidateToolDefinition.description', text: consolidateToolDefinition.description },
  { name: 'debriefToolDefinition.description', text: debriefToolDefinition.description },
  { name: 'importBatchToolDefinition.description', text: importBatchToolDefinition.description },
];

const MAX_LEN = 500;
const INVOKE_MARKER = /INVOKE WHEN/i;
// Matches `WHEN NOT TO USE`, `NOT FOR:`, `don't use`, `do not use`, etc.
// Stays in sync with the existing autonomy-regression test's guard regex.
const GUARD_MARKER = /WHEN NOT TO USE|NOT FOR|do not use|don't use|skip|not yet/i;

describe('tool-description-length — ≤500 chars per tool', () => {
  test.each(DESCRIPTIONS)('$name is ≤ 500 chars', ({ text }) => {
    expect(text.length).toBeLessThanOrEqual(MAX_LEN);
  });
});

describe('tool-description-length — autonomy markers present', () => {
  test.each(DESCRIPTIONS)('$name contains an INVOKE WHEN trigger', ({ text }) => {
    expect(text).toMatch(INVOKE_MARKER);
  });

  test.each(DESCRIPTIONS)('$name contains a WHEN NOT TO USE / NOT FOR guard', ({ text }) => {
    expect(text).toMatch(GUARD_MARKER);
  });
});
