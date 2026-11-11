/**
 * server-instructions-budget.test.ts — length budget for SERVER_INSTRUCTIONS.
 *
 * Hosts truncate the `initialize` instructions. ChatGPT keeps about the first
 * 512 characters and Claude Code about 2 KB (internal research digest
 * 2026-09-24 §5; MCP-Hermes parity plan 2026-11-03, PR 0.4). Before this
 * budget the text was 7,123 characters and the "store proactively" rule
 * started at character 2,125, so Claude Code likely never saw it.
 *
 * Contract:
 *   1. The first 512 characters say what TotalReclaw is, when to recall, when
 *      to remember, and carry the phrase-safety one-liner.
 *   2. The whole text is at most 2,000 characters, and every hard rule sits
 *      inside it.
 *   3. Detail lives in SERVER_GUIDE (resource `totalreclaw://guide` and the
 *      `totalreclaw_instructions` prompt), which keeps the full phrase-safety
 *      rules too.
 */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';

import {
  SERVER_INSTRUCTIONS,
  SERVER_GUIDE,
  SERVER_GUIDE_URI,
  getPromptMessages,
} from '../src/prompts';
import { TOOL_MANIFEST } from '../src/dispatch';
import { createTotalReclawServer } from '../src/server-setup';

const FIRST_WINDOW = 512;
const FULL_BUDGET = 2000;

const first = SERVER_INSTRUCTIONS.slice(0, FIRST_WINDOW);

describe('SERVER_INSTRUCTIONS length budget', () => {
  it(`is at most ${FULL_BUDGET} characters`, () => {
    expect(SERVER_INSTRUCTIONS.length).toBeLessThanOrEqual(FULL_BUDGET);
  });

  it('does not waste the first window on leading whitespace', () => {
    expect(SERVER_INSTRUCTIONS).toBe(SERVER_INSTRUCTIONS.trimStart());
  });
});

describe(`first ${FIRST_WINDOW} characters carry the essentials`, () => {
  it.each([
    ['what the server is', /TotalReclaw is the user's encrypted long-term memory/],
    ['use it unprompted', /unprompted/],
    ['when to recall', /totalreclaw_recall at the start of every conversation/],
    ['when to remember', /totalreclaw_remember when the user shares a durable fact/],
    ['phrase-safety one-liner', /recovery phrase must never enter this chat/],
    ['never ask, echo or print it', /Never ask for it, echo it or run a CLI that prints it/],
  ])('%s', (_label, pattern) => {
    expect(first).toMatch(pattern);
  });
});

describe(`every hard rule sits inside ${FULL_BUDGET} characters`, () => {
  const full = SERVER_INSTRUCTIONS.slice(0, FULL_BUDGET);
  it.each([
    ['never ask for the phrase in chat', /NEVER ask the user to paste their recovery phrase into chat/],
    ['never echo a phrase', /NEVER echo, print or summarize a phrase/],
    ['never run a phrase-touching CLI', /NEVER run a phrase-touching CLI through a shell tool/],
    ['named CLIs', /mcp-server setup[\s\S]*show-phrase/],
    ['pasted phrase is compromised', /compromised/],
    ['no secrets in memory', /secrets \(passwords, API keys, tokens\)/],
    ['atomic facts', /atomic facts/],
    ['setup via pair', /not_configured[\s\S]*totalreclaw_pair/],
    ['pair gives link + PIN, phrase only in the browser', /link \+ PIN[\s\S]*browser only, never in config, env vars or chat/],
    ['fully quit and reopen after pairing', /fully quit and reopen this app/],
    ['scope filing phrases reach set_scope', /file that under X[\s\S]*totalreclaw_set_scope/],
    ['stop storing after quota error', /quota_exceeded[\s\S]*stop storing/],
    ['debrief at the end', /totalreclaw_debrief/],
    ['pointer to the full guide', new RegExp(SERVER_GUIDE_URI.replace(/[/.]/g, '\\$&'))],
  ])('%s', (_label, pattern) => {
    expect(full).toMatch(pattern);
  });
});

describe('tool names in the instructions are real tools', () => {
  const advertised = new Set(TOOL_MANIFEST.map((t) => t.name));
  const mentioned = new Set(SERVER_INSTRUCTIONS.match(/totalreclaw_[a-z_]+/g) ?? []);
  it.each([...mentioned])('%s is in the tool manifest', (name) => {
    expect(advertised.has(name)).toBe(true);
  });
});

describe('SERVER_GUIDE keeps the detail', () => {
  it.each([
    ['phrase must never cross context', /MUST NEVER cross your context window/],
    ['never ask for the phrase', /NEVER ask the user to paste their phrase into chat/],
    ['never echo a phrase', /NEVER echo, print or summarize a phrase/],
    ['phrase-touching CLIs', /show-phrase/],
    ['compromised', /compromised/],
    ['credential vault tip', /credential vault/],
    ['remember triggers', /I prefer\.\.\./],
    ['do not store credentials', /credentials \(passwords, API keys, tokens\)/],
    ['importance scale', /7-9 for core identity/],
    ['quota handling', /quota_exceeded/],
    ['no hardcoded pricing', /Do not guess or hardcode pricing/],
    ['billing cache', /cached for up to 2 hours/],
    ['imports dry run', /dry_run=true first/],
    ['import keys never stored', /never stored/],
    ['scope filing phrases', /put that in my work memories[\s\S]*that's a work thing/],
    ['pin / retype / set_scope', /totalreclaw_pin[\s\S]*totalreclaw_retype[\s\S]*totalreclaw_set_scope/],
    ['supersession', /superseded_by/],
    ['debrief limits', /At most 5 items/],
  ])('%s', (_label, pattern) => {
    expect(SERVER_GUIDE).toMatch(pattern);
  });

  it('the totalreclaw_instructions prompt returns the instructions and the guide', () => {
    const [msg] = getPromptMessages('totalreclaw_instructions');
    expect(msg.content.text.startsWith(SERVER_INSTRUCTIONS)).toBe(true);
    expect(msg.content.text).toContain(SERVER_GUIDE);
  });
});

describe('MCP wiring', () => {
  async function connect() {
    const server = createTotalReclawServer({
      callTool: async () => ({ content: [{ type: 'text', text: '{}' }] }),
      isManagedMode: () => true,
      getClient: async () => {
        throw new Error('not used');
      },
    });
    const [clientT, serverT] = InMemoryTransport.createLinkedPair();
    const client = new Client({ name: 'budget-test', version: '0.0.0' });
    await Promise.all([server.connect(serverT), client.connect(clientT)]);
    return { client, server };
  }

  it('sends the budgeted instructions on initialize and serves the guide resource', async () => {
    const { client, server } = await connect();
    try {
      expect(client.getInstructions()).toBe(SERVER_INSTRUCTIONS);

      const { resources } = await client.listResources();
      expect(resources.map((r) => r.uri)).toContain(SERVER_GUIDE_URI);

      const read = await client.readResource({ uri: SERVER_GUIDE_URI });
      expect(read.contents[0]).toMatchObject({ uri: SERVER_GUIDE_URI, mimeType: 'text/markdown' });
      expect((read.contents[0] as { text: string }).text).toBe(SERVER_GUIDE);
    } finally {
      await client.close();
      await server.close();
    }
  });
});
