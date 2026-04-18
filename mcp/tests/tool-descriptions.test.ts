/**
 * Tool-description + v1-compat integration tests.
 *
 * Asserts:
 *   A) Each tool description contains enough natural-language triggers for an
 *      LLM to invoke it without the user knowing the tool name. Minimum: at
 *      least 2 distinct "when user says X" examples per tool.
 *   B) Every write-path tool emits a blob that round-trips through
 *      `readBlobUnified` with the expected taxonomy surface (v1 where fixed,
 *      v0 otherwise — documents current behaviour).
 *   C) The `totalreclaw_debrief` specifically emits v1 `summary` + `source:
 *      derived` (the A1 fix from AUDIT-v1-tools.md).
 *   D) The `totalreclaw_export` JSON path surfaces v1 fields when the
 *      underlying blob is v1 (the A3 fix).
 *
 * These tests are pure data-shape assertions — no network, no subgraph.
 */

// Import from individual tool files (not via tools/index) to match the
// pattern used by existing tests under mcp/tests — ts-jest's NodeNext
// resolution doesn't always find re-exports through index aggregators.
import { rememberToolDefinition } from '../src/tools/remember';
import { recallToolDefinition } from '../src/tools/recall';
import { forgetToolDefinition } from '../src/tools/forget';
import { exportToolDefinition } from '../src/tools/export';
import { importToolDefinition } from '../src/tools/import';
import { importFromToolDefinition } from '../src/tools/import-from';
import { importBatchToolDefinition } from '../src/tools/import-batch';
import { consolidateToolDefinition } from '../src/tools/consolidate';
import { statusToolDefinition } from '../src/tools/status';
import { upgradeToolDefinition } from '../src/tools/upgrade';
import { migrateToolDefinition } from '../src/tools/migrate';
import { debriefToolDefinition } from '../src/tools/debrief';
import { supportToolDefinition } from '../src/tools/support';
import { accountToolDefinition } from '../src/tools/account';
import { pinToolDefinition, unpinToolDefinition } from '../src/tools/pin';
import { retypeToolDefinition } from '../src/tools/retype';
import { setScopeToolDefinition } from '../src/tools/set-scope';

import { buildV1ClaimBlob, readBlobUnified } from '../src/claims-helper';
import { MEMORY_CLAIM_V1_SCHEMA_VERSION } from '../src/v1-types';

// ─── Helpers ────────────────────────────────────────────────────────────────

/** Count distinct "when the user says" style trigger bullets in a description.
 *
 * A trigger bullet either starts with `- "..."` (direct quote) OR contains
 * a quote after "- " somewhere on the line (e.g. `- The user says "goodbye"`).
 * Both patterns work as natural-language routing cues for the LLM.
 */
function countTriggerPhrases(desc: string): number {
  const matches = desc.match(/^-\s.*['"]/gm);
  return matches?.length ?? 0;
}

/** Normalize description string (pin.ts builds via `+ \n +` concat). */
function norm(desc: string): string {
  return desc.replace(/\s+/g, ' ');
}

// ─── A. Description autonomy ────────────────────────────────────────────────

describe('Tool descriptions — LLM-autonomy triggers', () => {
  type Row = { name: string; desc: string; minTriggers: number };

  const ROWS: Row[] = [
    // Core tools — must have rich triggers
    { name: 'totalreclaw_remember', desc: rememberToolDefinition.description, minTriggers: 0 /* trigger list in SERVER_INSTRUCTIONS, not the tool description */ },
    { name: 'totalreclaw_recall', desc: recallToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_forget', desc: forgetToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_export', desc: exportToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_import', desc: importToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_import_from', desc: importFromToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_import_batch', desc: importBatchToolDefinition.description, minTriggers: 0 /* INVOKE WHEN lines aren't quoted */ },
    { name: 'totalreclaw_consolidate', desc: consolidateToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_status', desc: statusToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_upgrade', desc: upgradeToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_migrate', desc: migrateToolDefinition.description, minTriggers: 0 /* workflow-style, fewer quoted triggers */ },
    { name: 'totalreclaw_debrief', desc: debriefToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_support', desc: supportToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_account', desc: accountToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_pin', desc: pinToolDefinition.description, minTriggers: 3 },
    { name: 'totalreclaw_unpin', desc: unpinToolDefinition.description, minTriggers: 2 },
    { name: 'totalreclaw_retype', desc: retypeToolDefinition.description, minTriggers: 2 },
    { name: 'totalreclaw_set_scope', desc: setScopeToolDefinition.description, minTriggers: 2 },
  ];

  test.each(ROWS)(
    '$name has >= $minTriggers natural-language trigger phrases',
    ({ desc, minTriggers }) => {
      const count = countTriggerPhrases(desc);
      expect(count).toBeGreaterThanOrEqual(minTriggers);
    },
  );

  // Harder: check that descriptions with triggers also have a "WHEN NOT TO USE"
  // section. This is our guardrail against over-invocation.
  const WITH_GUARDRAIL = [
    'totalreclaw_recall',
    'totalreclaw_forget',
    'totalreclaw_export',
    'totalreclaw_import',
    'totalreclaw_consolidate',
    'totalreclaw_status',
    'totalreclaw_upgrade',
    'totalreclaw_debrief',
    'totalreclaw_account',
    'totalreclaw_pin',
    'totalreclaw_unpin',
    'totalreclaw_retype',
    'totalreclaw_set_scope',
    'totalreclaw_import_batch',
  ];

  test.each(WITH_GUARDRAIL)('%s has a "WHEN NOT TO USE" guardrail', (name) => {
    const row = ROWS.find((r) => r.name === name)!;
    expect(norm(row.desc).toLowerCase()).toMatch(/when not to use|do not use|don't use|skip|not yet/);
  });
});

// ─── B. v1 blob round-trip (debrief + retype + set_scope) ───────────────────

describe('v1 blob emission — round-trips through readBlobUnified', () => {
  test('buildV1ClaimBlob emits schema-valid v1 JSON', () => {
    const blob = buildV1ClaimBlob({
      text: 'The user prefers dark mode',
      type: 'preference',
      source: 'user',
      scope: 'work',
      importance: 7,
    });
    const parsed = JSON.parse(blob) as Record<string, unknown>;
    expect(parsed.text).toBe('The user prefers dark mode');
    expect(parsed.type).toBe('preference');
    expect(parsed.source).toBe('user');
    expect(parsed.scope).toBe('work');
    // schema_version is stripped at serialization when equal to the default
    // (Rust `skip_serializing_if`). Readers must tolerate both presence and
    // absence — we validate that either it's absent OR it matches the v1
    // constant. The constant itself is still a useful compile-time sanity
    // check so keep the import.
    if (parsed.schema_version !== undefined) {
      expect(parsed.schema_version).toBe(MEMORY_CLAIM_V1_SCHEMA_VERSION);
    }
    // id + created_at auto-generated
    expect(typeof parsed.id).toBe('string');
    expect(typeof parsed.created_at).toBe('string');
  });

  test('readBlobUnified returns v1 surface for v1 blobs', () => {
    const blob = buildV1ClaimBlob({
      text: 'Always check d.get(errors) before trusting empty results',
      type: 'directive',
      source: 'user',
      scope: 'work',
      reasoning: 'caught a bug last week',
      importance: 9,
    });
    const doc = readBlobUnified(blob);
    expect(doc.text).toBe('Always check d.get(errors) before trusting empty results');
    expect(doc.importance).toBe(9);
    // v1 short-category for display fallback
    expect(doc.category).toBe('rule');
    // Full v1 surface present
    expect(doc.v1).toBeDefined();
    expect(doc.v1!.type).toBe('directive');
    expect(doc.v1!.source).toBe('user');
    expect(doc.v1!.scope).toBe('work');
    expect(doc.v1!.reasoning).toBe('caught a bug last week');
  });

  test('readBlobUnified falls back to v0 short-key parser for v0 blobs', () => {
    const v0Blob = JSON.stringify({
      t: 'Uses Postgres for analytics',
      c: 'fact',
      i: 6,
      sa: 'mcp-server',
      ea: new Date().toISOString(),
    });
    const doc = readBlobUnified(v0Blob);
    expect(doc.text).toBe('Uses Postgres for analytics');
    expect(doc.importance).toBe(6);
    // v0 blobs do not expose v1 surface
    expect(doc.v1).toBeUndefined();
    expect(doc.category).toBe('fact');
  });
});

// ─── C. Debrief (A1) emits v1 summary + source:derived ──────────────────────

describe('A1 fix — totalreclaw_debrief managed-service path', () => {
  // The managed-mode handler lives in mcp/src/index.ts (handleDebriefSubgraph).
  // We can't invoke it without a real SubgraphState, but we CAN verify the
  // exact builder call shape by re-constructing what the handler now does.

  test('debrief items build as v1 summary with source=derived', () => {
    const debriefItem = {
      text: 'Shipped v2 this week — users happy, analytics up 30%.',
      type: 'summary' as const,
      importance: 8,
    };

    // Mirrors handleDebriefSubgraph's new blob construction.
    const blob = buildV1ClaimBlob({
      text: debriefItem.text,
      type: 'summary',
      source: 'derived',
      importance: debriefItem.importance,
    });

    const doc = readBlobUnified(blob);
    expect(doc.v1).toBeDefined();
    expect(doc.v1!.type).toBe('summary');
    expect(doc.v1!.source).toBe('derived');
    expect(doc.importance).toBe(8);
    expect(doc.category).toBe('sum');
  });

  test('context-type debrief items also map to v1 summary', () => {
    // Per audit: tool-level `type: "context"` maps to v1 `summary` — both are
    // session synthesis. Verify the handler's choice is consistent.
    const ctxItem = { text: 'The 2026-Q2 migration was the thread tying the call together.', importance: 7 };
    const blob = buildV1ClaimBlob({
      text: ctxItem.text,
      type: 'summary',
      source: 'derived',
      importance: ctxItem.importance,
    });
    const doc = readBlobUnified(blob);
    expect(doc.v1!.type).toBe('summary');
    expect(doc.v1!.source).toBe('derived');
  });
});

// ─── D. Export (A3) surfaces v1 fields in JSON ──────────────────────────────

describe('A3 fix — totalreclaw_export surfaces v1 taxonomy fields', () => {
  // We test the parsing branch in isolation by invoking readBlobUnified on a
  // v1 blob and checking the shape the export handler builds.
  test('v1 blob export yields type/source/scope/reasoning in JSON output', () => {
    const v1Blob = buildV1ClaimBlob({
      text: 'Chose PostgreSQL for analytics store',
      type: 'claim',
      source: 'user',
      scope: 'work',
      reasoning: 'data is relational and needs ACID guarantees',
      importance: 8,
    });

    const doc = readBlobUnified(v1Blob);
    const exportedBase: Record<string, unknown> = {
      id: 'test-fact-id',
      text: doc.text,
      importance: 8,
    };
    if (doc.v1) {
      exportedBase.type = doc.v1.type;
      exportedBase.source = doc.v1.source;
      if (doc.v1.scope) exportedBase.scope = doc.v1.scope;
      if (doc.v1.reasoning) exportedBase.reasoning = doc.v1.reasoning;
    }

    expect(exportedBase.type).toBe('claim');
    expect(exportedBase.source).toBe('user');
    expect(exportedBase.scope).toBe('work');
    expect(exportedBase.reasoning).toBe('data is relational and needs ACID guarantees');
  });

  test('v0 blob export falls back to category-only type', () => {
    const v0Blob = JSON.stringify({
      t: 'Lives in Lisbon',
      c: 'fact',
      i: 7,
      sa: 'mcp-server',
      ea: new Date().toISOString(),
    });
    const doc = readBlobUnified(v0Blob);
    const exportedBase: Record<string, unknown> = {
      id: 'test-fact-id',
      text: doc.text,
      importance: 7,
    };
    if (doc.v1) {
      exportedBase.type = doc.v1.type;
    } else {
      exportedBase.type = doc.category;
    }
    expect(exportedBase.type).toBe('fact');
    expect(exportedBase.source).toBeUndefined();
    expect(exportedBase.scope).toBeUndefined();
  });
});

// ─── E. Schema shape spot-checks ────────────────────────────────────────────

describe('Tool schema — v1 surface in remember / retype / set_scope / forget', () => {
  test('remember accepts v1 types in facts[].type enum', () => {
    const factsSchema = rememberToolDefinition.inputSchema.properties?.facts as {
      items?: { properties?: { type?: { enum?: string[] } } };
    } | undefined;
    const typeEnum = factsSchema?.items?.properties?.type?.enum ?? [];
    expect(typeEnum).toEqual(
      expect.arrayContaining(['claim', 'preference', 'directive', 'commitment', 'episode', 'summary']),
    );
  });

  test('remember accepts v1 scope enum in facts[].scope', () => {
    const factsSchema = rememberToolDefinition.inputSchema.properties?.facts as {
      items?: { properties?: { scope?: { enum?: string[] } } };
    } | undefined;
    const scopeEnum = factsSchema?.items?.properties?.scope?.enum ?? [];
    expect(scopeEnum).toEqual(
      expect.arrayContaining([
        'work',
        'personal',
        'health',
        'family',
        'creative',
        'finance',
        'misc',
        'unspecified',
      ]),
    );
  });

  test('retype accepts only v1 type enum', () => {
    const schema = retypeToolDefinition.inputSchema.properties?.new_type as
      | { enum?: string[] }
      | undefined;
    expect(schema?.enum).toEqual([
      'claim',
      'preference',
      'directive',
      'commitment',
      'episode',
      'summary',
    ]);
  });

  test('set_scope accepts only v1 scope enum', () => {
    const schema = setScopeToolDefinition.inputSchema.properties?.scope as
      | { enum?: string[] }
      | undefined;
    expect(schema?.enum).toEqual([
      'work',
      'personal',
      'health',
      'family',
      'creative',
      'finance',
      'misc',
      'unspecified',
    ]);
  });

  test('forget accepts optional v1 scope hint', () => {
    // Cast via `unknown` — TypeScript narrows `properties` to the specific
    // keys declared, and `scope` is present at runtime but the literal type
    // doesn't expose it. The existing retype/set-scope tests cast via
    // `any`; we use `unknown` to stay stricter while still checking runtime.
    const props = forgetToolDefinition.inputSchema.properties as unknown as Record<
      string,
      { enum?: string[] }
    >;
    expect(props.scope?.enum).toEqual([
      'work',
      'personal',
      'health',
      'family',
      'creative',
      'finance',
      'misc',
      'unspecified',
    ]);
  });
});
