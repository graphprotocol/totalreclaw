/**
 * Memory Taxonomy v1 type guards + constants — sanity tests.
 */

import {
  VALID_MEMORY_TYPES_V1,
  VALID_MEMORY_SOURCES,
  VALID_MEMORY_SCOPES,
  VALID_MEMORY_VOLATILITIES,
  MEMORY_CLAIM_V1_SCHEMA_VERSION,
  PROTOBUF_WRAPPER_VERSION_V1,
  LEGACY_TYPE_TO_V1,
  V1_TYPE_TO_SHORT_CATEGORY,
  isValidMemoryTypeV1,
  isValidMemoryScope,
  isValidMemorySource,
  isValidMemoryVolatility,
} from '../src/v1-types';

describe('v1-types — enum constants', () => {
  test('VALID_MEMORY_TYPES_V1 is the 6-value spec enum', () => {
    expect(VALID_MEMORY_TYPES_V1).toEqual([
      'claim',
      'preference',
      'directive',
      'commitment',
      'episode',
      'summary',
    ]);
  });

  test('VALID_MEMORY_SOURCES is the 5-value spec enum', () => {
    expect(VALID_MEMORY_SOURCES).toEqual([
      'user',
      'user-inferred',
      'assistant',
      'external',
      'derived',
    ]);
  });

  test('VALID_MEMORY_SCOPES is the 8-value spec enum', () => {
    expect(VALID_MEMORY_SCOPES).toEqual([
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

  test('VALID_MEMORY_VOLATILITIES is the 3-value spec enum', () => {
    expect(VALID_MEMORY_VOLATILITIES).toEqual(['stable', 'updatable', 'ephemeral']);
  });

  test('MEMORY_CLAIM_V1_SCHEMA_VERSION is "1.0"', () => {
    expect(MEMORY_CLAIM_V1_SCHEMA_VERSION).toBe('1.0');
  });

  test('PROTOBUF_WRAPPER_VERSION_V1 is 4', () => {
    expect(PROTOBUF_WRAPPER_VERSION_V1).toBe(4);
  });
});

describe('v1-types — legacy-type migration map', () => {
  test('covers all legacy 8-type values', () => {
    expect(Object.keys(LEGACY_TYPE_TO_V1).sort()).toEqual([
      'context',
      'decision',
      'episodic',
      'fact',
      'goal',
      'preference',
      'rule',
      'summary',
    ]);
  });

  test('maps fact+context+decision to claim', () => {
    expect(LEGACY_TYPE_TO_V1.fact).toBe('claim');
    expect(LEGACY_TYPE_TO_V1.context).toBe('claim');
    expect(LEGACY_TYPE_TO_V1.decision).toBe('claim');
  });

  test('maps rule → directive, goal → commitment, episodic → episode', () => {
    expect(LEGACY_TYPE_TO_V1.rule).toBe('directive');
    expect(LEGACY_TYPE_TO_V1.goal).toBe('commitment');
    expect(LEGACY_TYPE_TO_V1.episodic).toBe('episode');
  });
});

describe('v1-types — V1_TYPE_TO_SHORT_CATEGORY', () => {
  test('maps every v1 type to a short-key', () => {
    for (const t of VALID_MEMORY_TYPES_V1) {
      expect(V1_TYPE_TO_SHORT_CATEGORY[t]).toBeTruthy();
    }
  });

  test('directive maps to rule (legacy short-form)', () => {
    expect(V1_TYPE_TO_SHORT_CATEGORY.directive).toBe('rule');
  });
});

describe('v1-types — runtime guards', () => {
  test('isValidMemoryTypeV1 accepts valid + rejects invalid', () => {
    expect(isValidMemoryTypeV1('claim')).toBe(true);
    expect(isValidMemoryTypeV1('directive')).toBe(true);
    expect(isValidMemoryTypeV1('rule')).toBe(false); // legacy, not v1
    expect(isValidMemoryTypeV1('fact')).toBe(false); // legacy
    expect(isValidMemoryTypeV1('')).toBe(false);
    expect(isValidMemoryTypeV1(null)).toBe(false);
    expect(isValidMemoryTypeV1(123)).toBe(false);
  });

  test('isValidMemoryScope rejects non-v1 scopes', () => {
    expect(isValidMemoryScope('work')).toBe(true);
    expect(isValidMemoryScope('unspecified')).toBe(true);
    expect(isValidMemoryScope('travel')).toBe(false);
    expect(isValidMemoryScope(undefined)).toBe(false);
  });

  test('isValidMemorySource enforces kebab-case', () => {
    expect(isValidMemorySource('user')).toBe(true);
    expect(isValidMemorySource('user-inferred')).toBe(true);
    expect(isValidMemorySource('user_inferred')).toBe(false);
    expect(isValidMemorySource('User')).toBe(false);
  });

  test('isValidMemoryVolatility', () => {
    expect(isValidMemoryVolatility('stable')).toBe(true);
    expect(isValidMemoryVolatility('updatable')).toBe(true);
    expect(isValidMemoryVolatility('ephemeral')).toBe(true);
    expect(isValidMemoryVolatility('permanent')).toBe(false);
  });
});
