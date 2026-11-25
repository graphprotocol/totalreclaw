jest.mock('../src/extraction/llm-client.js', () => {
  const actual = jest.requireActual('../src/extraction/llm-client.js');
  return {
    ...actual,
    chatCompletion: jest.fn(),
    resolveLLMConfig: jest.fn(() => ({
      apiKey: 'k',
      baseUrl: 'https://llm.invalid/v1',
      model: 'm',
      apiFormat: 'openai',
    })),
  };
});
import { chatCompletion, LLMUpstreamOutageError } from '../src/extraction/llm-client.js';
import { extractFacts } from '../src/extraction/extractor.js';

const MSGS = [
  { role: 'user', content: 'I live in Porto and I work at Tidewater as an engineer.' },
  { role: 'assistant', content: 'Noted.' },
];
const mocked = chatCompletion as jest.Mock;

it('rethrows LLMUpstreamOutageError (z.ai 1302)', async () => {
  mocked.mockRejectedValueOnce(
    new LLMUpstreamOutageError('LLM upstream outage after 3 attempts: LLM API 429: {"code":"1302"}', 3, 429),
  );
  await expect(extractFacts(MSGS, 'turn')).rejects.toBeInstanceOf(LLMUpstreamOutageError);
});

it('still returns [] for a non-retryable error', async () => {
  mocked.mockRejectedValueOnce(new Error('LLM API 400: bad request'));
  await expect(extractFacts(MSGS, 'turn')).resolves.toEqual([]);
});

it('returns [] for a zero-facts answer', async () => {
  mocked.mockResolvedValueOnce('{"topics":[],"facts":[]}');
  await expect(extractFacts(MSGS, 'turn')).resolves.toEqual([]);
});

it('hard-cuts a single oversized first message instead of dropping it', async () => {
  mocked.mockClear();
  mocked.mockResolvedValueOnce('{"topics":[],"facts":[]}');
  const big = [
    { role: 'user', content: 'I work at Tidewater. ' + 'y'.repeat(30_000) },
    { role: 'assistant', content: 'Noted.' },
  ];
  await extractFacts(big, 'full');
  expect(mocked).toHaveBeenCalledTimes(1);
  const userPrompt = (mocked.mock.calls[0][1] as Array<{ role: string; content: string }>)[1].content;
  expect(userPrompt).toContain('I work at Tidewater.');
  expect(userPrompt.length).toBeGreaterThan(11_000);
  expect(userPrompt.length).toBeLessThan(12_500);
});
