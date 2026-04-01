import { parseDebriefResponse, DEBRIEF_SYSTEM_PROMPT } from '../src/tools/debrief.js';

describe('parseDebriefResponse', () => {
  test('valid JSON array', () => {
    const input = JSON.stringify([
      { text: 'Session was about refactoring the auth module', type: 'summary', importance: 8 },
      { text: 'Migration to new API is still pending', type: 'context', importance: 7 },
    ]);
    const result = parseDebriefResponse(input);
    expect(result).toHaveLength(2);
    expect(result[0].type).toBe('summary');
    expect(result[0].importance).toBe(8);
    expect(result[1].type).toBe('context');
  });

  test('empty array', () => {
    expect(parseDebriefResponse('[]')).toEqual([]);
  });

  test('strips markdown code fences', () => {
    const input = '```json\n[{"text": "Session summary here with enough text", "type": "summary", "importance": 8}]\n```';
    const result = parseDebriefResponse(input);
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe('summary');
  });

  test('strips bare code fences', () => {
    const input = '```\n[{"text": "Session summary here with enough text", "type": "context", "importance": 7}]\n```';
    const result = parseDebriefResponse(input);
    expect(result).toHaveLength(1);
  });

  test('caps at 5 items', () => {
    const items = Array.from({ length: 8 }, (_, i) => ({
      text: `Debrief item number ${i + 1} with enough text`,
      type: 'summary',
      importance: 7,
    }));
    const result = parseDebriefResponse(JSON.stringify(items));
    expect(result).toHaveLength(5);
  });

  test('filters importance below 6', () => {
    const input = JSON.stringify([
      { text: 'Important finding from the session', type: 'summary', importance: 8 },
      { text: 'Trivial detail that should be filtered out', type: 'context', importance: 3 },
    ]);
    const result = parseDebriefResponse(input);
    expect(result).toHaveLength(1);
    expect(result[0].importance).toBe(8);
  });

  test('importance exactly 6 passes', () => {
    const input = JSON.stringify([
      { text: 'Borderline importance item at exactly six', type: 'summary', importance: 6 },
    ]);
    const result = parseDebriefResponse(input);
    expect(result).toHaveLength(1);
  });

  test('importance exactly 5 filtered', () => {
    const input = JSON.stringify([
      { text: 'Below threshold importance item at five', type: 'summary', importance: 5 },
    ]);
    const result = parseDebriefResponse(input);
    expect(result).toHaveLength(0);
  });

  test('validates type — defaults invalid to context', () => {
    const input = JSON.stringify([
      { text: 'Valid summary item for the session', type: 'summary', importance: 7 },
      { text: 'This has an invalid type value set', type: 'fact', importance: 7 },
    ]);
    const result = parseDebriefResponse(input);
    expect(result).toHaveLength(2);
    expect(result[0].type).toBe('summary');
    expect(result[1].type).toBe('context');
  });

  test('handles invalid JSON gracefully', () => {
    expect(parseDebriefResponse('not json')).toEqual([]);
  });

  test('handles non-array JSON', () => {
    expect(parseDebriefResponse('{"text": "not an array"}')).toEqual([]);
  });

  test('handles empty string', () => {
    expect(parseDebriefResponse('')).toEqual([]);
  });

  test('filters short text (< 5 chars)', () => {
    const input = JSON.stringify([
      { text: 'ok', type: 'summary', importance: 8 },
      { text: 'This is a valid debrief item text', type: 'summary', importance: 8 },
    ]);
    const result = parseDebriefResponse(input);
    expect(result).toHaveLength(1);
  });

  test('defaults importance to 7 if missing', () => {
    const input = JSON.stringify([
      { text: 'A debrief item without importance score', type: 'summary' },
    ]);
    const result = parseDebriefResponse(input);
    expect(result).toHaveLength(1);
    expect(result[0].importance).toBe(7);
  });

  test('clamps importance to 1-10 range', () => {
    const input = JSON.stringify([
      { text: 'Huge importance value far above maximum', type: 'summary', importance: 99 },
    ]);
    const result = parseDebriefResponse(input);
    expect(result[0].importance).toBe(10);
  });

  test('truncates text to 512 characters', () => {
    const longText = 'x'.repeat(600);
    const input = JSON.stringify([{ text: longText, type: 'summary', importance: 8 }]);
    const result = parseDebriefResponse(input);
    expect(result[0].text).toHaveLength(512);
  });
});

describe('DEBRIEF_SYSTEM_PROMPT', () => {
  test('contains all required sections', () => {
    expect(DEBRIEF_SYSTEM_PROMPT).toContain('Broader context');
    expect(DEBRIEF_SYSTEM_PROMPT).toContain('Outcomes & conclusions');
    expect(DEBRIEF_SYSTEM_PROMPT).toContain('What was attempted');
    expect(DEBRIEF_SYSTEM_PROMPT).toContain('Relationships');
    expect(DEBRIEF_SYSTEM_PROMPT).toContain('Open threads');
    expect(DEBRIEF_SYSTEM_PROMPT).toContain('Maximum 5 items');
    expect(DEBRIEF_SYSTEM_PROMPT).toContain('{already_stored_facts}');
    expect(DEBRIEF_SYSTEM_PROMPT).toContain('summary|context');
  });
});
