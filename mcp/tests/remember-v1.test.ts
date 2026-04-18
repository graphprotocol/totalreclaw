/**
 * Extended `totalreclaw_remember` tool schema tests — v1 fields.
 *
 * Verifies that the tool schema advertises:
 *   - the union of legacy v0 + v1 types in the enum
 *   - a `scope` input (both batch and single-fact modes)
 *   - a `reasoning` input (both batch and single-fact modes)
 *
 * Plus unit tests for the `normalizeTypeToV1` + `normalizeScope` helpers.
 */

import {
  rememberToolDefinition,
  normalizeTypeToV1,
  normalizeScope,
} from '../src/tools/remember';

describe('rememberToolDefinition — v1 schema', () => {
  test('tool name unchanged', () => {
    expect(rememberToolDefinition.name).toBe('totalreclaw_remember');
  });

  test('type enum includes v1 values (claim, preference, directive, commitment, episode, summary)', () => {
    const facts = rememberToolDefinition.inputSchema.properties.facts;
    const typeEnum = (facts.items as any).properties.type.enum as string[];
    expect(typeEnum).toEqual(
      expect.arrayContaining([
        'claim',
        'preference',
        'directive',
        'commitment',
        'episode',
        'summary',
      ]),
    );
  });

  test('type enum also includes legacy v0 values for migration', () => {
    const facts = rememberToolDefinition.inputSchema.properties.facts;
    const typeEnum = (facts.items as any).properties.type.enum as string[];
    expect(typeEnum).toEqual(
      expect.arrayContaining(['fact', 'context', 'decision', 'rule', 'goal', 'episodic']),
    );
  });

  test('facts[].scope is declared with 8 valid values', () => {
    const facts = rememberToolDefinition.inputSchema.properties.facts;
    const scopeEnum = (facts.items as any).properties.scope.enum as string[];
    expect(scopeEnum.length).toBe(8);
    expect(scopeEnum).toContain('work');
    expect(scopeEnum).toContain('unspecified');
  });

  test('facts[].reasoning is a free-form string', () => {
    const facts = rememberToolDefinition.inputSchema.properties.facts;
    expect((facts.items as any).properties.reasoning.type).toBe('string');
  });

  test('single-fact mode advertises top-level scope + reasoning', () => {
    const props = rememberToolDefinition.inputSchema.properties as any;
    expect(props.scope).toBeDefined();
    expect(props.scope.enum).toContain('work');
    expect(props.reasoning).toBeDefined();
    expect(props.reasoning.type).toBe('string');
  });

  test('metadata.scope + metadata.reasoning are also accepted', () => {
    const metaProps = (rememberToolDefinition.inputSchema.properties.metadata as any)
      .properties;
    expect(metaProps.scope).toBeDefined();
    expect(metaProps.reasoning).toBeDefined();
  });

  test('remains a non-destructive idempotent tool', () => {
    expect(rememberToolDefinition.annotations.destructiveHint).toBe(false);
    expect(rememberToolDefinition.annotations.idempotentHint).toBe(true);
  });
});

describe('normalizeTypeToV1', () => {
  test('identity for v1 types', () => {
    expect(normalizeTypeToV1('claim')).toBe('claim');
    expect(normalizeTypeToV1('directive')).toBe('directive');
    expect(normalizeTypeToV1('commitment')).toBe('commitment');
  });

  test('legacy fact → claim', () => {
    expect(normalizeTypeToV1('fact')).toBe('claim');
    expect(normalizeTypeToV1('context')).toBe('claim');
    expect(normalizeTypeToV1('decision')).toBe('claim');
  });

  test('legacy rule → directive', () => {
    expect(normalizeTypeToV1('rule')).toBe('directive');
  });

  test('legacy goal → commitment', () => {
    expect(normalizeTypeToV1('goal')).toBe('commitment');
  });

  test('legacy episodic → episode', () => {
    expect(normalizeTypeToV1('episodic')).toBe('episode');
  });

  test('unknown input defaults to claim', () => {
    expect(normalizeTypeToV1('unknown')).toBe('claim');
    expect(normalizeTypeToV1(undefined)).toBe('claim');
    expect(normalizeTypeToV1(123)).toBe('claim');
    expect(normalizeTypeToV1(null)).toBe('claim');
  });

  test('trims + lowercases input', () => {
    expect(normalizeTypeToV1('  DIRECTIVE  ')).toBe('directive');
    expect(normalizeTypeToV1('Rule')).toBe('directive');
  });
});

describe('normalizeScope', () => {
  test('returns valid scopes unchanged', () => {
    expect(normalizeScope('work')).toBe('work');
    expect(normalizeScope('health')).toBe('health');
  });

  test('falls back to unspecified', () => {
    expect(normalizeScope('travel')).toBe('unspecified');
    expect(normalizeScope(undefined)).toBe('unspecified');
    expect(normalizeScope(null)).toBe('unspecified');
    expect(normalizeScope(42)).toBe('unspecified');
  });
});
