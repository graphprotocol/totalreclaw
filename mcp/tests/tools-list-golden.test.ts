/**
 * @jest-environment node
 *
 * DEP-15 parity pin: the tool surface the MCP server advertises (names,
 * order, descriptions, inputSchema, annotations) must not change across the
 * `@modelcontextprotocol/sdk` 1.x → `@modelcontextprotocol/server` 2.x
 * upgrade. `tests/fixtures/tools-list.golden.json` was generated at public
 * main 17ffb82 (v1 SDK) by `scripts/gen-tools-golden.cjs`; it equals the
 * v1 `tools/list` wire payload byte-for-byte.
 *
 * If this fails because a PR INTENTIONALLY changed a tool definition,
 * regenerate: `npm run build && node scripts/gen-tools-golden.cjs`, and say
 * so in the PR body. Otherwise the failure is a regression.
 */

import * as fs from 'fs';
import * as path from 'path';

import { TOOL_MANIFEST } from '../src/dispatch';

const GOLDEN_PATH = path.join(__dirname, 'fixtures', 'tools-list.golden.json');

describe('tools/list golden (DEP-15 parity pin)', () => {
  it('TOOL_MANIFEST serialises byte-identically to the committed golden', () => {
    const golden = fs.readFileSync(GOLDEN_PATH, 'utf8');
    expect(JSON.stringify(TOOL_MANIFEST, null, 2) + '\n').toBe(golden);
  });

  it('golden lists the 18 tools in the shipped order', () => {
    const golden = JSON.parse(fs.readFileSync(GOLDEN_PATH, 'utf8')) as Array<{ name: string }>;
    expect(golden.map((t) => t.name)).toEqual([
      'totalreclaw_remember',
      'totalreclaw_recall',
      'totalreclaw_forget',
      'totalreclaw_export',
      'totalreclaw_import',
      'totalreclaw_import_from',
      'totalreclaw_import_batch',
      'totalreclaw_consolidate',
      'totalreclaw_status',
      'totalreclaw_upgrade',
      'totalreclaw_debrief',
      'totalreclaw_support',
      'totalreclaw_account',
      'totalreclaw_pin',
      'totalreclaw_unpin',
      'totalreclaw_retype',
      'totalreclaw_set_scope',
      'totalreclaw_pair',
    ]);
  });
});
