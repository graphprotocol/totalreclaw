/**
 * @jest-environment node
 *
 * The Claude Desktop "main memory" tip appears exactly once, only after a
 * reload activation (pairing in this process), only on totalreclaw_account /
 * totalreclaw_status, and only for Claude Desktop (clientInfo.name `claude-ai`).
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { createCredentialActivator } from '../src/credential-activation';
import { armSetupNote, resetSetupNote, withSetupNote, SETUP_NOTE_TEXT } from '../src/setup-note';
import { resolveManagedCredential } from '../src/subgraph/credentials';
import type { ToolResponse } from '../src/tools/types';

const MNEMONIC = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const DESKTOP = 'claude-ai';

const ok = (text: string): ToolResponse => ({ content: [{ type: 'text', text }] });
const textOf = (r: ToolResponse): string => (r.content[0] as { text: string }).text;

let tmpDir: string;
let credsPath: string;
beforeEach(() => {
  resetSetupNote();
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-setupnote-'));
  credsPath = path.join(tmpDir, 'credentials.json');
});
afterEach(() => fs.rmSync(tmpDir, { recursive: true, force: true }));

function writeCreds(): void {
  fs.writeFileSync(credsPath, JSON.stringify({ userId: 'u', salt: 'aa', serverUrl: 'https://relay.test', mnemonic: MNEMONIC }), { mode: 0o600 });
}

/** Same wiring as index.ts: arm on a reload commit with a configured state. */
function makeActivator() {
  let state: { id: number } | null = null;
  return createCredentialActivator<{ id: number }>({
    credentialsPath: credsPath,
    isSelfHosted: () => false,
    resolveCredential: () => resolveManagedCredential({ credentialsPath: credsPath, env: {} }),
    initFromBundle: async () => ({ id: 1 }),
    initFromMnemonic: async () => ({ id: 1 }),
    isUsableMnemonic: () => true,
    ownerAddress: () => '0xSA',
    getState: () => state,
    commit: (next, { phase }) => {
      if (phase === 'reload' && next) armSetupNote();
      state = next;
    },
    getUnusable: () => undefined,
    setUnusable: () => {},
    log: () => {},
  });
}

describe('Claude Desktop main-memory tip', () => {
  it('has the approved text, byte for byte', () => {
    expect(SETUP_NOTE_TEXT).toBe(
      'Tip for the user: in Claude apps, Claude\'s own memory is usually on and is used before TotalReclaw. ' +
        'To have Claude use TotalReclaw automatically in future chats, just say: "Make TotalReclaw my main memory."',
    );
  });

  it('contains none of the forbidden words', () => {
    for (const w of ['phrase', 'mnemonic', 'seed', 'recovery']) {
      expect(SETUP_NOTE_TEXT.toLowerCase()).not.toContain(w);
    }
  });

  it('does not appear when credentials existed at startup', async () => {
    writeCreds();
    await makeActivator().activate('startup');
    expect(withSetupNote(ok('status body'), DESKTOP).content[0]).toEqual({ type: 'text', text: 'status body' });
  });

  it('appears once, as a final paragraph, after pairing activation', async () => {
    const a = makeActivator();
    await a.activate('startup');
    writeCreds();
    await a.reloadAfterPair();

    const first = withSetupNote(ok('account body'), DESKTOP);
    expect(first.content).toHaveLength(1);
    expect(textOf(first)).toBe(`account body\n\n${SETUP_NOTE_TEXT}`);

    expect(textOf(withSetupNote(ok('account body'), DESKTOP))).toBe('account body');
  });

  it('an error result neither gets the tip nor consumes it', () => {
    armSetupNote();
    const err: ToolResponse = { content: [{ type: 'text', text: 'boom' }], isError: true };
    expect(withSetupNote(err, DESKTOP)).toBe(err);
    expect(textOf(withSetupNote(ok('x'), DESKTOP))).toContain(SETUP_NOTE_TEXT);
  });

  it('a soft error ({"error": ...} text, isError unset) keeps it armed and unchanged', () => {
    armSetupNote();
    const soft = ok('{"error":"relay 503"}');
    expect(withSetupNote(soft, DESKTOP)).toBe(soft);
    expect(textOf(withSetupNote(ok('{"tier":"free"}'), DESKTOP))).toBe(`{"tier":"free"}\n\n${SETUP_NOTE_TEXT}`);
  });

  it('a success JSON payload and non-JSON text both get the tip', () => {
    armSetupNote();
    expect(textOf(withSetupNote(ok('{"ok":true}'), DESKTOP))).toContain(SETUP_NOTE_TEXT);
    armSetupNote();
    expect(textOf(withSetupNote(ok('plain text'), DESKTOP))).toBe(`plain text\n\n${SETUP_NOTE_TEXT}`);
  });

  it('is wired only to account and status in index.ts', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'index.ts'), 'utf8');
    expect(src.match(/withSetupNote\(/g)).toHaveLength(2);
    expect(src).toMatch(/totalreclaw_status: async \(args\) => withSetupNote/);
    expect(src).toMatch(/totalreclaw_account: async \(args\) => withSetupNote/);
  });

  describe('client gate', () => {
    it('claude-ai gets it', () => {
      armSetupNote();
      expect(textOf(withSetupNote(ok('x'), 'claude-ai'))).toBe(`x\n\n${SETUP_NOTE_TEXT}`);
    });

    it.each([['claude-code'], ['codex-mcp-client'], ['cursor'], ['Claude-AI'], [undefined]])(
      '%s never sees it, and the tip is gone for good',
      (client) => {
        armSetupNote();
        expect(textOf(withSetupNote(ok('x'), client as string | undefined))).toBe('x');
        // Even if the client later reports claude-ai, the tip was disarmed.
        expect(textOf(withSetupNote(ok('x'), DESKTOP))).toBe('x');
      },
    );
  });

  describe('TOTALRECLAW_HOST fallback (mcpb manifest env)', () => {
    const prev = process.env.TOTALRECLAW_HOST;
    afterEach(() => {
      if (prev === undefined) delete process.env.TOTALRECLAW_HOST;
      else process.env.TOTALRECLAW_HOST = prev;
    });

    it('unknown client + env claude-desktop: tip shown', () => {
      process.env.TOTALRECLAW_HOST = 'claude-desktop';
      armSetupNote();
      expect(textOf(withSetupNote(ok('x'), undefined))).toBe(`x\n\n${SETUP_NOTE_TEXT}`);
    });

    it('known other client + env: no tip', () => {
      process.env.TOTALRECLAW_HOST = 'claude-desktop';
      armSetupNote();
      expect(textOf(withSetupNote(ok('x'), 'claude-code'))).toBe('x');
    });

    it('unknown client, no env: no tip', () => {
      delete process.env.TOTALRECLAW_HOST;
      armSetupNote();
      expect(textOf(withSetupNote(ok('x'), undefined))).toBe('x');
    });

    it('unknown client + a different env value: no tip', () => {
      process.env.TOTALRECLAW_HOST = 'something-else';
      armSetupNote();
      expect(textOf(withSetupNote(ok('x'), undefined))).toBe('x');
    });
  });
});
