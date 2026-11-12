/**
 * Recall top-k default — 16 facts unless the caller, the env, or the relay
 * says otherwise. Precedence mirrors the Python client: explicit k > env
 * TOTALRECLAW_RECALL_TOP_K > relay features.recall_top_k > 16.
 */

export {};

const mockRecall = jest.fn();

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

import {
  DEFAULT_RECALL_TOP_K,
  getRecallTopK,
  setServerRecallTopK,
  _resetServerRecallTopKForTests,
} from '../src/recall-top-k';
import { handleRecall, recallToolDefinition } from '../src/tools/recall';

const ENV = 'TOTALRECLAW_RECALL_TOP_K';

describe('recall top-k resolution', () => {
  const saved = process.env[ENV];

  beforeEach(() => {
    delete process.env[ENV];
    _resetServerRecallTopKForTests();
    mockRecall.mockReset();
    mockRecall.mockResolvedValue([]);
  });

  afterAll(() => {
    if (saved === undefined) delete process.env[ENV];
    else process.env[ENV] = saved;
  });

  test('default is 16', () => {
    expect(DEFAULT_RECALL_TOP_K).toBe(16);
    expect(getRecallTopK()).toBe(16);
  });

  test('relay value beats the default', () => {
    setServerRecallTopK(12);
    expect(getRecallTopK()).toBe(12);
  });

  test('env beats the relay value', () => {
    setServerRecallTopK(12);
    process.env[ENV] = '5';
    expect(getRecallTopK()).toBe(5);
  });

  test('invalid env and relay values are ignored', () => {
    process.env[ENV] = 'abc';
    setServerRecallTopK(0);
    setServerRecallTopK(-3);
    setServerRecallTopK('nope');
    setServerRecallTopK(undefined);
    expect(getRecallTopK()).toBe(16);
    process.env[ENV] = '0';
    expect(getRecallTopK()).toBe(16);
  });

  test('a later invalid relay value keeps the last valid one', () => {
    setServerRecallTopK(10);
    setServerRecallTopK(null);
    expect(getRecallTopK()).toBe(10);
  });

  test('tool schema advertises 16', () => {
    expect(recallToolDefinition.inputSchema.properties.k.default).toBe(16);
  });

  test('handleRecall asks the client for 16 when k is omitted', async () => {
    await handleRecall({ client: { recall: mockRecall } } as any, { query: 'what do I like' });
    expect(mockRecall).toHaveBeenCalledWith('what do I like', 16);
  });

  test('handleRecall honours the relay value when k is omitted', async () => {
    setServerRecallTopK(8);
    await handleRecall({ client: { recall: mockRecall } } as any, { query: 'q' });
    expect(mockRecall).toHaveBeenCalledWith('q', 8);
  });

  test('explicit k wins over env, relay and default', async () => {
    process.env[ENV] = '20';
    setServerRecallTopK(12);
    await handleRecall({ client: { recall: mockRecall } } as any, { query: 'q', k: 3 });
    expect(mockRecall).toHaveBeenCalledWith('q', 3);
  });
});
