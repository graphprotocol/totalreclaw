/**
 * @jest-environment node
 *
 * Issue #778: totalreclaw_import_from (self-hosted; Mem0 and MCP Memory)
 * counted a store error as a skipped duplicate whenever its text contained
 * "409", "duplicate" or "fingerprint". The self-hosted server never answers
 * a duplicate with an error (a fingerprint match comes back in a successful
 * response), so those were real failures: missing from `errors`, and able
 * to report `success: true` with nothing imported.
 *
 * Runs the real handler. The adapter module it loads at runtime and the
 * client are stand-ins (ts-jest type-checks the real adapters, and one of
 * them does not pass under this package's test config).
 */
import { handleImportFrom } from '../src/tools/import-from';

// The handler loads <repo>/skill/plugin/import-adapters/index.js at runtime.
jest.mock('../../skill/plugin/import-adapters/index.js', () => ({
  getAdapter: () => ({
    source: 'mcp-memory',
    displayName: 'MCP Memory',
    parse: async () => ({
      facts: [
        { text: 'Alice likes green tea', type: 'preference', importance: 6, source: 'mcp-memory' },
        { text: 'Alice lives in Porto', type: 'claim', importance: 6, source: 'mcp-memory' },
      ],
      chunks: [],
      totalMessages: 0,
      warnings: [],
      errors: [],
    }),
  }),
}));

/** A client whose n-th `remember` throws `outcomes[n]`, or stores when it is null. */
function clientWith(outcomes: Array<string | null>) {
  let n = 0;
  return {
    remember: jest.fn(async () => {
      const error = outcomes[n++];
      if (error) throw new Error(error);
      return `fact-${n}`;
    }),
  };
}

async function runImport(client: ReturnType<typeof clientWith>) {
  const out = await handleImportFrom({ client } as never, { source: 'mcp-memory', content: '{}' });
  return JSON.parse(out.content[0].text);
}

describe('totalreclaw_import_from store errors (#778)', () => {
  test.each([
    'HTTP 502: <html>Bad gateway. Request id 7c409e1a</html>',
    'HTTP 500: duplicate key value violates unique constraint "facts_pkey"',
    'Store failed: fingerprint index unavailable',
  ])('is reported as an error, not a skipped duplicate: %s', async (error) => {
    const client = clientWith([error, null]);
    const result = await runImport(client);

    expect(client.remember).toHaveBeenCalledTimes(2);
    expect(result.imported).toBe(1);
    expect(result.skipped_duplicate).toBe(0);
    expect(result.errors).toEqual([expect.objectContaining({ index: 0, error })]);
  });

  test('an import that stored nothing is not a success', async () => {
    const error = 'HTTP 409: Conflict';
    const result = await runImport(clientWith([error, error]));

    expect(result.imported).toBe(0);
    expect(result.skipped_duplicate).toBe(0);
    expect(result.errors).toHaveLength(2);
    expect(result.success).toBe(false);
  });
});
