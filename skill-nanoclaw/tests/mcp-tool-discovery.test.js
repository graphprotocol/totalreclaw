/**
 * @jest-environment node
 *
 * MCP tool discovery test for NanoClaw 3.0.0.
 *
 * NanoClaw's agent-runner spawns `@totalreclaw/mcp-server` over stdio and
 * the Claude Agent SDK exposes its tools under the `mcp__totalreclaw__*`
 * prefix. For the new v1 MCP tools (`totalreclaw_pin`, `totalreclaw_unpin`,
 * `totalreclaw_retype`, `totalreclaw_set_scope`) to reach the NanoClaw agent
 * LLM, TWO things must be true:
 *
 *   1. The agent-runner's `allowedTools` array must whitelist
 *      `mcp__totalreclaw__*` (or a more permissive pattern).
 *   2. The agent-runner's `mcpServers.totalreclaw` config must spawn the
 *      `@totalreclaw/mcp-server` process with the recovery phrase + env vars.
 *
 * This test parses the agent-runner source and asserts both.
 *
 * It is a STATIC check — we don't spin up a real MCP session in Jest.
 * End-to-end tool discovery (actual JSON-RPC `tools/list` → schema) is
 * exercised by the cross-client E2E suite in `totalreclaw-internal/e2e`.
 */

const fs = require('fs');
const path = require('path');

describe('NanoClaw agent-runner — MCP v1 tool discovery', () => {
  const runnerPath = path.join(__dirname, '..', 'mcp', 'nanoclaw-agent-runner.ts');
  let runner;

  beforeAll(() => {
    runner = fs.readFileSync(runnerPath, 'utf-8');
  });

  it('allowlists the mcp__totalreclaw__* tool namespace', () => {
    // The SDK uses `mcp__<server-name>__<tool-name>` for tools exposed by
    // an MCP server. The catch-all glob must be present so every v1 tool
    // (pin, retype, set_scope, etc.) is reachable.
    expect(runner).toMatch(/['"]mcp__totalreclaw__\*['"]/);
  });

  it('configures an "totalreclaw" MCP server spawn', () => {
    // The server key in `mcpServers` becomes the `<server-name>` in the
    // SDK's `mcp__<server-name>__<tool-name>` prefix.
    expect(runner).toMatch(/totalreclaw:\s*\{/);
  });

  it('spawns @totalreclaw/mcp-server via npx or TOTALRECLAW_MCP_PATH', () => {
    expect(runner).toMatch(/@totalreclaw\/mcp-server/);
    // Allows local dev override for self-built MCP
    expect(runner).toMatch(/TOTALRECLAW_MCP_PATH/);
  });

  it('forwards required env vars to the spawned MCP server', () => {
    expect(runner).toMatch(/TOTALRECLAW_RECOVERY_PHRASE/);
    expect(runner).toMatch(/TOTALRECLAW_SERVER_URL/);
    expect(runner).toMatch(/TOTALRECLAW_NAMESPACE/);
    expect(runner).toMatch(/TOTALRECLAW_CREDENTIALS_PATH/);
  });

  it('does NOT pin to a v0-only tool allowlist', () => {
    // Regression guard: an earlier design gated individual tools
    // (`mcp__totalreclaw__totalreclaw_remember`) which would silently
    // block newly-added v1 tools (pin/retype/set_scope). The glob
    // `mcp__totalreclaw__*` is the correct pattern.
    // Confirm no individual gate on a core v1 tool:
    expect(runner).not.toMatch(/mcp__totalreclaw__totalreclaw_remember['"]/);
    expect(runner).not.toMatch(/mcp__totalreclaw__totalreclaw_recall['"]/);
  });
});

/**
 * v1 MCP tool name expectations.
 *
 * These are the tool names NanoClaw's agent must be able to discover and
 * invoke via MCP tool-discovery. If the MCP server side changes a tool
 * name, this test will still pass (it doesn't inspect MCP); but the set of
 * expected names is tracked here as a cross-client checklist.
 *
 * When the MCP server is installed at runtime (via `npx @totalreclaw/mcp-server`)
 * the JSON-RPC `tools/list` response includes each of these. The E2E suite
 * verifies actual discovery; this test documents the contract.
 */
describe('Expected v1 MCP tool names (documentation)', () => {
  const EXPECTED_TOOLS = [
    // Core v1 CRUD
    'totalreclaw_remember',
    'totalreclaw_recall',
    'totalreclaw_forget',
    'totalreclaw_export',
    // v1 KG ops (new in MCP 3.0.0)
    'totalreclaw_pin',
    'totalreclaw_unpin',
    'totalreclaw_retype',
    'totalreclaw_set_scope',
    // Account / billing
    'totalreclaw_status',
    'totalreclaw_upgrade',
    'totalreclaw_migrate',
    // Consolidation / debrief
    'totalreclaw_consolidate',
    'totalreclaw_debrief',
    // Import
    'totalreclaw_import',
    'totalreclaw_import_from',
    'totalreclaw_import_batch',
    // Support
    'totalreclaw_support',
    'totalreclaw_account',
  ];

  it('documents the expected v1 tool set', () => {
    // Snapshot check — any new tool added/removed in MCP 3.0.0+ should
    // be reflected here so cross-client test writers know the contract.
    expect(EXPECTED_TOOLS).toHaveLength(18);
    expect(EXPECTED_TOOLS).toContain('totalreclaw_pin');
    expect(EXPECTED_TOOLS).toContain('totalreclaw_retype');
    expect(EXPECTED_TOOLS).toContain('totalreclaw_set_scope');
  });
});
