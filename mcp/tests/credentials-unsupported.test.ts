/**
 * A credentials.json written by another TotalReclaw client in a form the MCP
 * server can't read (Hermes OS-keychain storage, or a newer `version`) must
 * not crash the server at startup. A crash makes the host drop every tool and
 * leaves the reason in a log the user never sees.
 *
 * `resolveManagedCredential` throws `UnsupportedCredentialsError` for those
 * (index.ts then starts unconfigured and reports the reason on every tool
 * call). A v2 bundle that fails validation still throws a plain Error, which
 * stays fatal: that is evidence of a corrupt or tampered store.
 *
 * Temp dirs and synthetic fixture files only.
 */

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { resolveManagedCredential, UnsupportedCredentialsError } from '../src/subgraph/credentials';
import { createCallToolHandler, type DispatchDeps } from '../src/dispatch';

const SA = '0x2c0CF74B2b76110708CA431796367779e3738250';

function tmpCredentialsPath(): string {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-creds-unsup-')), 'credentials.json');
}

function resolveFile(obj: unknown): () => unknown {
  const credentialsPath = tmpCredentialsPath();
  fs.writeFileSync(credentialsPath, JSON.stringify(obj), 'utf-8');
  return () => resolveManagedCredential({ credentialsPath, env: {} });
}

function thrown(fn: () => unknown): unknown {
  try {
    fn();
  } catch (err) {
    return err;
  }
  throw new Error('expected a throw');
}

describe('resolveManagedCredential: files from other clients are "unsupported", not corrupt', () => {
  it('Hermes v2 keychain-wrapped bundle file', () => {
    const err = thrown(
      resolveFile({
        version: 2,
        schema: 'derived-bundle-v1',
        keychain_wrapped: true,
        account: { smart_account: SA, chain_id: 100 },
        signing: { kind: 'owner-eoa', address: '0x9858EfFD232B4033E47d90003D41EC34EcaEda94' },
        provisioned_at: '2026-08-02T09:41:00Z',
        provisioned_by: 'local-migration',
      }),
    );
    expect(err).toBeInstanceOf(UnsupportedCredentialsError);
    expect((err as UnsupportedCredentialsError).reason).toBe('keychain_wrapped');
    expect((err as Error).message).toMatch(/keychain/i);
  });

  it('Hermes v1 keychain marker file (no version key) is reported, not read as "nothing configured"', () => {
    const err = thrown(
      resolveFile({
        mnemonic: '__keychain__:v1:0x0000000000000000000000000000000000000001',
        keychain_wrapped: true,
        scope_address: SA,
      }),
    );
    expect(err).toBeInstanceOf(UnsupportedCredentialsError);
    expect((err as UnsupportedCredentialsError).reason).toBe('keychain_wrapped');
  });

  it('a bare v1 marker without the flag is still recognised', () => {
    const err = thrown(resolveFile({ mnemonic: '__keychain__:v1:0x0000000000000000000000000000000000000001' }));
    expect((err as UnsupportedCredentialsError).reason).toBe('keychain_wrapped');
  });

  it.each([3, 99, '3'])('unknown version=%p', (version) => {
    const err = thrown(resolveFile({ version, whatever: true }));
    expect(err).toBeInstanceOf(UnsupportedCredentialsError);
    expect((err as UnsupportedCredentialsError).reason).toBe('unknown_version');
  });

  it('the message never echoes file contents beyond a short version value', () => {
    const secretish = 'z'.repeat(200);
    const err = thrown(resolveFile({ version: secretish }));
    expect((err as Error).message).not.toContain(secretish);
  });

  it('a v2 bundle that fails validation stays a plain (fatal) Error', () => {
    const err = thrown(
      resolveFile({
        version: 2,
        schema: 'derived-bundle-v1',
        vault: { auth_key: 'not-hex' },
        signing: { kind: 'owner-eoa', address: 'x', private_key: 'y' },
        account: { smart_account: SA, chain_id: 100 },
        provisioned_at: '2026-08-02T09:41:00Z',
        provisioned_by: 'local-migration',
      }),
    );
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(UnsupportedCredentialsError);
  });
});

describe('dispatch: unconfigured because of an unusable file says why', () => {
  function harness(unusable?: { reason: 'keychain_wrapped' | 'unknown_version'; message: string }) {
    const deps: DispatchDeps = {
      getMode: () => 'unconfigured',
      handleSupport: async () => ({ content: [{ type: 'text', text: '{}' }] }),
      handlePair: async () => ({ content: [{ type: 'text', text: '{}' }] }),
      common: {},
      resolveBundle: async () => ({}),
      isQuotaExceededError: () => false,
      quotaExceededResponse: () => ({ content: [{ type: 'text', text: '{}' }] }),
      isAuthError: () => false,
      authHintResponse: () => ({ content: [{ type: 'text', text: '{}' }] }),
      onMutate: () => undefined,
      getUnusableCredentials: () => unusable,
    };
    return createCallToolHandler(deps);
  }

  it('returns not_configured with the reason and tells the agent not to pair', async () => {
    const res = await harness({ reason: 'keychain_wrapped', message: 'Keychain file, cannot read.' })(
      'totalreclaw_recall',
      { query: 'x' },
    );
    const body = JSON.parse(res.content[0].text);
    expect(res.isError).toBe(true);
    expect(body.error).toBe('not_configured');
    expect(body.reason).toBe('keychain_wrapped');
    expect(body.message).toMatch(/^Keychain file, cannot read\./);
    expect(body.message).toMatch(/Do not call totalreclaw_pair/);
  });

  it('without an unusable file the generic not_configured envelope is unchanged', async () => {
    const body = JSON.parse((await harness()('totalreclaw_recall', { query: 'x' })).content[0].text);
    expect(body.error).toBe('not_configured');
    expect(body.reason).toBeUndefined();
  });
});
