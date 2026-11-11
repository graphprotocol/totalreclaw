/**
 * Environment-binding guard for the MCP server (p-diogo/totalreclaw-internal#673).
 *
 * Contract (post "F flip", public PR #198): rc AND stable artifacts default to
 * https://api.totalreclaw.xyz; staging is opt-in via TOTALRECLAW_SERVER_URL.
 * `scripts/check-url-binding.cjs` enforces it in the publish job and in
 * `prepublishOnly`. These tests pin the guard's behaviour and run it against
 * the real source tree so a stray staging default fails CI, not the publish.
 */

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { execFileSync } from 'node:child_process';

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { checkUrlBinding, CANONICAL_SITES } = require('../scripts/check-url-binding.cjs');

const MCP_ROOT = path.resolve(__dirname, '..');
const SCRIPT = path.join(MCP_ROOT, 'scripts', 'check-url-binding.cjs');

const STAGING = 'https://api-staging.totalreclaw.xyz';

/** Build a fake package root with a dist/ tree holding every canonical site. */
function fakeRoot(overrides: Record<string, string> = {}): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mcp-url-binding-'));
  const files: Record<string, string> = {
    'index.js': "const SERVER_URL = process.env.TOTALRECLAW_SERVER_URL || 'https://api.totalreclaw.xyz';\n",
    'cli/setup.js': "exports.DEFAULT_SERVER_URL = 'https://api.totalreclaw.xyz';\n",
    'subgraph/store.js': "relayUrl: process.env.TOTALRECLAW_SERVER_URL || 'https://api.totalreclaw.xyz',\n",
    'tools/pair.js':
      "// RC users on api-staging stay on staging when they set the env var.\n" +
      "const DEFAULT_RELAY_URL_WSS = 'wss://api.totalreclaw.xyz';\n",
    'pair-remote-client.js': "exports.DEFAULT_RELAY_URL = 'wss://api.totalreclaw.xyz';\n",
    ...overrides,
  };
  for (const [rel, body] of Object.entries(files)) {
    const p = path.join(root, 'dist', rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, body);
  }
  return root;
}

function runCli(root: string, releaseType: string): { code: number; out: string } {
  try {
    const out = execFileSync(process.execPath, [SCRIPT, `--root=${root}`, `--release-type=${releaseType}`], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    return { code: 0, out };
  } catch (err: any) {
    return { code: err.status ?? -1, out: `${err.stdout ?? ''}${err.stderr ?? ''}` };
  }
}

describe('MCP default relay URL binding (#673)', () => {
  test.each(['rc', 'stable'])('the real source tree binds production (%s)', (releaseType) => {
    // Scan src/ explicitly so the result does not depend on a stale local dist/.
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mcp-url-binding-src-'));
    fs.cpSync(path.join(MCP_ROOT, 'src'), path.join(root, 'src'), { recursive: true });
    const r = checkUrlBinding({ root, releaseType });
    expect(r.errors).toEqual([]);
    expect(r.ok).toBe(true);
    expect(r.treeKind).toBe('src');
  });

  test.each(['rc', 'stable'])('a clean production tree passes (%s)', (releaseType) => {
    expect(runCli(fakeRoot(), releaseType).code).toBe(0);
  });

  test('staging mentioned only in a comment passes', () => {
    const root = fakeRoot({
      'index.js':
        `/* default is prod; QA exports TOTALRECLAW_SERVER_URL=${STAGING} */\n` +
        "const SERVER_URL = process.env.TOTALRECLAW_SERVER_URL || 'https://api.totalreclaw.xyz';\n",
    });
    expect(runCli(root, 'rc').code).toBe(0);
  });

  test.each(['rc', 'stable'])('a staging default literal fails (%s)', (releaseType) => {
    const root = fakeRoot({
      'index.js': `const SERVER_URL = process.env.TOTALRECLAW_SERVER_URL || '${STAGING}';\n`,
    });
    const { code, out } = runCli(root, releaseType);
    expect(code).toBe(1);
    expect(out).toContain('dist/index.js:1');
    expect(out).toContain('staging URL bound as a default');
  });

  test('a staging wss literal in a non-canonical file fails', () => {
    const root = fakeRoot({ 'extra/relay.js': 'const u = "wss://api-staging.totalreclaw.xyz/v1/pair";\n' });
    const { code, out } = runCli(root, 'stable');
    expect(code).toBe(1);
    expect(out).toContain('dist/extra/relay.js:1');
  });

  test('a staging template literal fails', () => {
    const root = fakeRoot({ 'extra/t.js': 'const u = `https://api-staging.totalreclaw.xyz`;\n' });
    expect(runCli(root, 'rc').code).toBe(1);
  });

  test('a canonical site that lost the production default fails', () => {
    const root = fakeRoot({ 'cli/setup.js': "exports.DEFAULT_SERVER_URL = 'http://localhost:8080';\n" });
    const { code, out } = runCli(root, 'stable');
    expect(code).toBe(1);
    expect(out).toContain('dist/cli/setup.js (no quoted api.totalreclaw.xyz default)');
  });

  test('a missing canonical site fails', () => {
    const root = fakeRoot();
    fs.rmSync(path.join(root, 'dist', 'pair-remote-client.js'));
    const { code, out } = runCli(root, 'rc');
    expect(code).toBe(1);
    expect(out).toContain('pair-remote-client.js (file missing)');
  });

  test('an unknown release type is a usage error', () => {
    expect(runCli(fakeRoot(), 'beta').code).toBe(2);
  });

  test('every canonical site exists in src/', () => {
    for (const site of CANONICAL_SITES) {
      expect(fs.existsSync(path.join(MCP_ROOT, 'src', `${site}.ts`))).toBe(true);
    }
  });
});
