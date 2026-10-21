/**
 * @jest-environment node
 *
 * DEP-15 — interop with the reference MCP TypeScript client (SDK v2), in
 * each of its three negotiation modes, against `serveTotalReclawStdio`
 * over the SDK's in-process `InMemoryTransport`:
 *   - 'legacy' (default): plain 2025 `initialize` — what an SDK-1.x host or
 *     Claude Code with MCP_PROTOCOL_NEGOTIATION unset/legacy does.
 *   - 'auto': probe with `server/discover`, fall back to `initialize` on a
 *     legacy server — what Claude Code with MCP_PROTOCOL_NEGOTIATION=auto does.
 *   - { pin: '2026-07-28' }: modern only.
 * Every mode must see the same tools (golden), instructions and serverInfo,
 * and a tool call must return the same content.
 */

import * as fs from 'fs';
import * as path from 'path';

import { Client } from '@modelcontextprotocol/client';
import { InMemoryTransport } from '@modelcontextprotocol/server';
import type { StdioServerHandle } from '@modelcontextprotocol/server/stdio';

import { serveTotalReclawStdio } from '../src/server-setup';
import { PROMPT_DEFINITIONS, SERVER_INSTRUCTIONS } from '../src/prompts';
import { memoryContextResource } from '../src/resources';
import type { ToolResponse } from '../src/tools/types';

const GOLDEN_TOOLS = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures', 'tools-list.golden.json'), 'utf8'),
) as unknown[];

type Mode = 'legacy' | 'auto' | { pin: string };

let handle: StdioServerHandle | undefined;
let client: Client | undefined;

async function connect(mode: Mode): Promise<Client> {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  handle = serveTotalReclawStdio(
    {
      callTool: async (name: string): Promise<ToolResponse> => ({
        content: [{ type: 'text', text: JSON.stringify({ ok: true, tool: name }) }],
      }),
      isManagedMode: () => true,
      getClient: async () => {
        throw new Error('getClient must not be called in these tests');
      },
    },
    { transport: serverSide },
  );
  client = new Client(
    { name: 'dep15-interop-client', version: '0.0.0' },
    { versionNegotiation: { mode } },
  );
  await client.connect(clientSide);
  return client;
}

let stderrSpy: jest.SpyInstance;

beforeEach(() => {
  stderrSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(async () => {
  await client?.close();
  await handle?.close();
  client = undefined;
  handle = undefined;
  stderrSpy.mockRestore();
});

describe.each<[string, Mode, 'legacy' | 'modern']>([
  ['legacy (default)', 'legacy', 'legacy'],
  ['auto (probe + fallback)', 'auto', 'modern'],
  ['pinned 2026-07-28', { pin: '2026-07-28' }, 'modern'],
])('SDK v2 client, versionNegotiation %s', (_label, mode, expectedEra) => {
  it(`negotiates the ${expectedEra} era`, async () => {
    const c = await connect(mode);
    expect(c.getProtocolEra()).toBe(expectedEra);
  });

  it('sees the golden tool list, the server instructions and serverInfo', async () => {
    const c = await connect(mode);
    const { tools } = await c.listTools();
    expect(tools).toEqual(GOLDEN_TOOLS);
    expect(c.getInstructions()).toBe(SERVER_INSTRUCTIONS);
    expect(c.getServerVersion()).toEqual({ name: 'totalreclaw', version: '1.0.0' });
  });

  it('sees the same prompts and resources as the v1-SDK server advertised', async () => {
    const c = await connect(mode);
    const { prompts } = await c.listPrompts();
    expect(prompts).toEqual([
      { name: 'totalreclaw_instructions', description: 'Instructions for using TotalReclaw tools' },
      ...PROMPT_DEFINITIONS,
    ]);
    const { resources } = await c.listResources();
    expect(resources).toEqual([memoryContextResource]);
  });

  it('gets identical tool-call content', async () => {
    const c = await connect(mode);
    const res = await c.callTool({ name: 'totalreclaw_status', arguments: {} });
    expect(res.content).toEqual([
      { type: 'text', text: JSON.stringify({ ok: true, tool: 'totalreclaw_status' }) },
    ]);
    expect(res.isError).toBeFalsy();
  });
});
