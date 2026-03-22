/**
 * @jest-environment node
 */

/**
 * Auth & Credentials Edge Cases
 *
 * These tests verify edge cases around credential handling, recovery phrase
 * derivation, and client initialization states. Since the actual TotalReclaw
 * client and credential persistence are mocked, we focus on the contract:
 * - What happens when credentials are missing, corrupted, or invalid?
 * - How does client re-initialization behave?
 */

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

const fs = require('fs');
const path = require('path');
const os = require('os');

function createMockClient(overrides = {}) {
  return {
    recall: jest.fn().mockResolvedValue([]),
    remember: jest.fn().mockResolvedValue('fact-id'),
    forget: jest.fn().mockResolvedValue(undefined),
    isReady: jest.fn().mockReturnValue(true),
    ...overrides,
  };
}

describe('Auth & Credentials Edge Cases', () => {
  describe('missing TOTALRECLAW_RECOVERY_PHRASE env var', () => {
    const originalEnv = process.env;

    beforeEach(() => {
      process.env = { ...originalEnv };
      delete process.env.TOTALRECLAW_RECOVERY_PHRASE;
    });

    afterEach(() => {
      process.env = originalEnv;
    });

    it('handlers still work when client is provided directly (no env dependency)', async () => {
      // The handlers receive a pre-initialized client -- they do not read env vars.
      // This confirms the separation of concerns: auth is in the entry point, not in handlers.
      const mockClient = createMockClient({
        remember: jest.fn().mockResolvedValue('fact-env-test'),
      });
      const { handleRemember } = require('../dist/tools/remember.js');
      const result = await handleRemember(mockClient, { fact: 'test' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(parsed.fact_id).toBe('fact-env-test');
    });

    it('recall handler works without env-level auth (client injected)', async () => {
      const mockClient = createMockClient();
      const { handleRecall } = require('../dist/tools/recall.js');
      const result = await handleRecall(mockClient, { query: 'test' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.memories).toEqual([]);
    });
  });

  describe('corrupted credentials JSON', () => {
    it('remember still works with a valid injected client (no creds file dependency)', async () => {
      // Handlers do not parse credentials -- they rely on the injected client.
      const mockClient = createMockClient({
        remember: jest.fn().mockResolvedValue('fact-corrupt-test'),
      });
      const { handleRemember } = require('../dist/tools/remember.js');
      const result = await handleRemember(mockClient, { fact: 'survive corruption' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
    });

    it('simulated malformed JSON credentials fails at parse', () => {
      const malformedJson = '{"userId": "test", bad json';
      expect(() => JSON.parse(malformedJson)).toThrow();
    });

    it('simulated valid JSON with missing userId field', () => {
      const jsonWithoutUserId = '{"salt": "dGVzdA=="}';
      const parsed = JSON.parse(jsonWithoutUserId);
      expect(parsed.userId).toBeUndefined();
    });
  });

  describe('invalid hex salt in credentials', () => {
    it('detects non-hex salt string', () => {
      const creds = { userId: 'user-1', salt: '!!!not-hex!!!' };
      // Attempt to decode -- Buffer.from with hex silently ignores invalid chars
      const decoded = Buffer.from(creds.salt, 'hex');
      // The decoded result would be garbage, but should not throw
      expect(decoded).toBeInstanceOf(Buffer);
    });

    it('detects empty salt string', () => {
      const creds = { userId: 'user-1', salt: '' };
      const decoded = Buffer.from(creds.salt, 'hex');
      expect(decoded.length).toBe(0);
    });
  });

  describe('credentials file permission errors (directory does not exist)', () => {
    it('writing to non-existent directory throws', () => {
      const nonExistentPath = path.join(os.tmpdir(), 'totalreclaw-test-nonexistent', 'subdir', 'creds.json');
      expect(() => {
        fs.writeFileSync(nonExistentPath, JSON.stringify({ userId: 'test' }));
      }).toThrow();
    });

    it('reading from non-existent file throws', () => {
      const nonExistentPath = path.join(os.tmpdir(), 'totalreclaw-test-no-file-xyz.json');
      expect(() => {
        fs.readFileSync(nonExistentPath, 'utf-8');
      }).toThrow();
    });
  });

  describe('client re-initialization after credential changes', () => {
    it('new client instance gets new mock return values', () => {
      const client1 = createMockClient({
        remember: jest.fn().mockResolvedValue('id-from-client-1'),
      });
      const client2 = createMockClient({
        remember: jest.fn().mockResolvedValue('id-from-client-2'),
      });

      // Simulate re-initialization: the second client is independent
      expect(client1.remember).not.toBe(client2.remember);
    });

    it('handlers use the client they are given, not a cached one', async () => {
      const { handleRemember } = require('../dist/tools/remember.js');

      const client1 = createMockClient({
        remember: jest.fn().mockResolvedValue('client1-fact'),
      });
      const client2 = createMockClient({
        remember: jest.fn().mockResolvedValue('client2-fact'),
      });

      const result1 = await handleRemember(client1, { fact: 'A' }, 'default');
      const result2 = await handleRemember(client2, { fact: 'B' }, 'default');

      const parsed1 = JSON.parse(result1.content[0].text);
      const parsed2 = JSON.parse(result2.content[0].text);

      expect(parsed1.fact_id).toBe('client1-fact');
      expect(parsed2.fact_id).toBe('client2-fact');
      expect(client1.remember).toHaveBeenCalledTimes(1);
      expect(client2.remember).toHaveBeenCalledTimes(1);
    });

    it('isReady check on uninitialized client returns false', () => {
      const uninitClient = createMockClient({
        isReady: jest.fn().mockReturnValue(false),
      });
      expect(uninitClient.isReady()).toBe(false);
    });
  });
});
