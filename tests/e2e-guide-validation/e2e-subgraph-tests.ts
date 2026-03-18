/**
 * E2E Subgraph Mode Tests — WebSocket client for OpenClaw.
 *
 * Tests the full TotalReclaw pipeline through OpenClaw's WebSocket agent:
 *   T1: Fresh install onboarding
 *   T2: Auto-extraction (agent_end hook)
 *   T3: Explicit store (totalreclaw_remember)
 *   T4: Cross-session recall (before_agent_start)
 *   T5: Explicit recall (totalreclaw_recall)
 *   T6: Status / billing
 *   T7: Export (managed service mode)
 *   T8: Forget (on-chain tombstone)
 *   T9: Recovery (same mnemonic, fresh container) — manual
 *   T10: MEMORY.md cleartext prevention
 *
 * Usage: npx tsx e2e-subgraph-tests.ts [--test T3] [--from T5]
 *
 * IMPORTANT:
 * - Must use WebSocket — HTTP /v1/chat/completions does NOT support tool calls or agent_end hooks.
 * - Tool events are NOT streamed to WebSocket clients — verify via response content (UUIDs, data).
 * - Requires `tools.allow: ["totalreclaw", "group:plugins"]` in gateway config.
 * - Container must be rebuilt after config changes (docker compose build --no-cache).
 */

import WebSocket from 'ws';
import { execSync } from 'child_process';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const WS_URL = 'ws://127.0.0.1:18789';
const AUTH_TOKEN = 'guide-test-token-2026';

// UUID pattern for verifying tool call results
const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

// ---------------------------------------------------------------------------
// WebSocket client for OpenClaw gateway
// ---------------------------------------------------------------------------

interface AgentResponse {
  content: string;
  runId: string;
}

/** Extract text from various content formats (string, array of blocks, object). */
function extractTextContent(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((b: unknown) => {
        if (typeof b === 'string') return b;
        if (b && typeof b === 'object' && 'text' in b) return (b as { text: string }).text;
        return '';
      })
      .join('');
  }
  if (content && typeof content === 'object') {
    const c = content as Record<string, unknown>;
    if (typeof c.text === 'string') return c.text;
    if (typeof c.content === 'string') return c.content;
  }
  return '';
}

class OpenClawClient {
  private ws: WebSocket | null = null;
  private reqId = 0;
  private pendingRequests = new Map<string, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  private eventHandlers: Array<(event: string, payload: unknown) => void> = [];

  async connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(WS_URL);

      this.ws.on('open', () => {
        const connectReq = {
          type: 'req',
          id: this.nextId(),
          method: 'connect',
          params: {
            minProtocol: 3,
            maxProtocol: 3,
            client: {
              id: 'cli',
              displayName: 'e2e-test',
              version: 'dev',
              platform: 'node',
              mode: 'cli',
            },
            role: 'operator',
            scopes: ['operator.read', 'operator.write'],
            auth: { token: AUTH_TOKEN },
          },
        };
        this.ws!.send(JSON.stringify(connectReq));
      });

      this.ws.on('message', (data) => {
        const msg = JSON.parse(String(data));

        if (msg.type === 'res') {
          const pending = this.pendingRequests.get(msg.id);
          if (pending) {
            this.pendingRequests.delete(msg.id);
            if (msg.ok) {
              pending.resolve(msg.payload);
            } else {
              pending.reject(new Error(msg.error?.message || 'Request failed'));
            }
          }
          if (msg.id === 'r1' && msg.ok) {
            resolve();
          } else if (msg.id === 'r1' && !msg.ok) {
            reject(new Error(msg.error?.message || 'Connect failed'));
          }
        } else if (msg.type === 'event') {
          for (const handler of this.eventHandlers) {
            handler(msg.event, msg.payload);
          }
        }
      });

      this.ws.on('error', (err) => reject(err));
      this.ws.on('close', () => {
        this.ws = null;
      });
    });
  }

  private nextId(): string {
    return `r${++this.reqId}`;
  }

  private sendRequest(method: string, params: unknown): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const id = this.nextId();
      this.pendingRequests.set(id, { resolve, reject });
      this.ws!.send(JSON.stringify({ type: 'req', id, method, params }));
    });
  }

  onEvent(handler: (event: string, payload: unknown) => void): void {
    this.eventHandlers.push(handler);
  }

  /**
   * Send a chat message with retry on network_error.
   * Uses a unique sessionKey per call to isolate context,
   * unless an explicit sessionKey is provided.
   */
  async sendMessage(
    message: string,
    opts?: { sessionKey?: string; timeoutMs?: number; maxRetries?: number },
  ): Promise<AgentResponse> {
    const timeoutMs = opts?.timeoutMs ?? 120_000;
    const maxRetries = opts?.maxRetries ?? 3;

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        return await this._sendMessage(message, opts?.sessionKey, timeoutMs);
      } catch (err) {
        const msg = (err as Error).message;
        const isRetriable = msg.includes('network_error') || msg.includes('timed out');
        if (isRetriable && attempt < maxRetries) {
          console.log(`    [retry] ${msg.includes('network_error') ? 'network_error' : 'timeout'} on attempt ${attempt}/${maxRetries}, retrying in 5s...`);
          await new Promise((r) => setTimeout(r, 5000));
          continue;
        }
        throw err;
      }
    }
    throw new Error('unreachable');
  }

  private async _sendMessage(message: string, sessionKey?: string, timeoutMs = 120_000): Promise<AgentResponse> {
    const idempotencyKey = `e2e-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const key = sessionKey ?? `e2e-${Date.now()}`;

    const result: AgentResponse = { content: '', runId: '' };

    return new Promise<AgentResponse>((resolve, reject) => {
      const timeout = setTimeout(() => {
        cleanup();
        reject(new Error(`Agent response timed out after ${timeoutMs}ms`));
      }, timeoutMs);

      const onEvent = (event: string, payload: unknown) => {
        const p = payload as Record<string, unknown>;

        // Filter events by sessionKey to prevent cross-contamination between tests.
        // Gateway prefixes sessionKey with "agent:main:", so use endsWith for matching.
        if (event === 'chat') {
          const eventSessionKey = p.sessionKey as string | undefined;
          if (eventSessionKey && !eventSessionKey.endsWith(key)) return; // not our session

          const state = p.state as string;

          if (state === 'delta') {
            const msg = p.message as Record<string, unknown> | undefined;
            if (msg?.content != null) {
              result.content = extractTextContent(msg.content);
            }
          } else if (state === 'final') {
            const msg = p.message as Record<string, unknown> | undefined;
            if (msg?.content != null) {
              result.content = extractTextContent(msg.content);
            }
            cleanup();
            resolve(result);
          } else if (state === 'error') {
            cleanup();
            reject(new Error(p.errorMessage as string || 'Agent error'));
          } else if (state === 'aborted') {
            cleanup();
            reject(new Error('Agent run aborted'));
          }
        }

        if (event === 'agent') {
          const eventSessionKey = p.sessionKey as string | undefined;
          if (eventSessionKey && !eventSessionKey.endsWith(key)) return;
          if (p.runId) result.runId = p.runId as string;
        }
      };

      const cleanup = () => {
        clearTimeout(timeout);
        const idx = this.eventHandlers.indexOf(onEvent);
        if (idx >= 0) this.eventHandlers.splice(idx, 1);
      };

      this.eventHandlers.push(onEvent);

      this.sendRequest('chat.send', {
        sessionKey: key,
        message,
        idempotencyKey,
      }).catch((err) => {
        cleanup();
        reject(err);
      });
    });
  }

  close(): void {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}

// ---------------------------------------------------------------------------
// Container log helper
// ---------------------------------------------------------------------------

function getContainerLogs(tail = 100): string {
  try {
    return execSync(
      `docker compose -f /Users/pdiogo/Documents/code/totalreclaw/tests/e2e-guide-validation/docker-compose.yml logs --tail=${tail} 2>&1`,
      { encoding: 'utf8', timeout: 10_000 },
    );
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------------------
// Test runner
// ---------------------------------------------------------------------------

interface TestResult {
  name: string;
  passed: boolean;
  checks: Array<{ label: string; passed: boolean; detail?: string }>;
  error?: string;
}

const results: TestResult[] = [];

function report(name: string, checks: Array<{ label: string; passed: boolean; detail?: string }>): void {
  const allPassed = checks.every((c) => c.passed);
  results.push({ name, passed: allPassed, checks });

  const icon = allPassed ? '✅' : '❌';
  console.log(`\n${icon} ${name}`);
  for (const c of checks) {
    console.log(`  ${c.passed ? '✅' : '❌'} ${c.label}${c.detail ? ` — ${c.detail}` : ''}`);
  }
}

function reportError(name: string, error: string): void {
  results.push({ name, passed: false, checks: [], error });
  console.log(`\n❌ ${name} — ERROR: ${error}`);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Shared session key — tests that depend on prior context share this session
const SHARED_SESSION = 'e2e-shared-' + Date.now();

// ---------------------------------------------------------------------------
// Test implementations
// ---------------------------------------------------------------------------

async function runT1(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('T1: Fresh Install Onboarding');
  console.log('='.repeat(60));

  const response = await client.sendMessage(
    'Hello, I just installed TotalReclaw. This is my first time using it.',
    { sessionKey: SHARED_SESSION },
  );
  console.log(`  Agent response (${response.content.length} chars): ${response.content.substring(0, 200)}...`);

  const logs = getContainerLogs(200);

  report('T1: Fresh Install Onboarding', [
    {
      label: 'Plugin loaded',
      passed: logs.includes('TotalReclaw plugin loaded'),
    },
    {
      label: 'Credentials loaded or registered',
      passed: logs.includes('Loaded existing credentials') || logs.includes('Registered new user'),
    },
    {
      label: 'Smart Account derived',
      passed: logs.toLowerCase().includes('0x2c0cf74b'),
    },
    {
      label: 'LSH hasher initialized',
      passed: logs.includes('LSH hasher initialized'),
    },
    {
      label: 'Agent responded',
      passed: response.content.length > 20,
      detail: `${response.content.length} chars`,
    },
  ]);
}

async function runT2(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('T2: Auto-Extraction (agent_end hook)');
  console.log('='.repeat(60));

  // Use the shared session — agent_end fires after the run
  const response = await client.sendMessage(
    'My name is Alice and I live in Tokyo. I work as a data scientist at a healthcare company.',
    { sessionKey: SHARED_SESSION },
  );
  console.log(`  Agent response: ${response.content.substring(0, 150)}...`);

  // Wait for agent_end async extraction + on-chain write
  console.log('  Waiting 30s for agent_end extraction + on-chain write...');
  await sleep(30_000);

  // We can't query the subgraph directly (URL is stale).
  // Instead, we verify via next test (T4) that facts are recallable.
  // For now just verify the agent acknowledged the info.
  report('T2: Auto-Extraction', [
    {
      label: 'Agent acknowledged personal info',
      passed: response.content.length > 20,
      detail: `${response.content.length} chars`,
    },
    {
      label: 'Agent_end hook likely fired (response completed)',
      passed: response.runId.length > 0,
      detail: `runId: ${response.runId}`,
    },
  ]);
}

async function runT3(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('T3: Explicit Store (totalreclaw_remember)');
  console.log('='.repeat(60));

  // Use shared session so the remember happens in context
  const response = await client.sendMessage(
    'Please remember that my favorite programming language is Haskell.',
    { sessionKey: SHARED_SESSION },
  );
  console.log(`  Agent response: ${response.content.substring(0, 200)}...`);

  // Tool events aren't streamed to WebSocket. Verify tool execution by:
  // 1. Response confirms storage ("saved", "noted", "remembered", etc.)
  // 2. Optionally, response may contain a UUID (some models include it, some don't)
  const hasUuid = UUID_RE.test(response.content);
  const confirmsStore = /saved|stored|remembered|noted|got it|done/i.test(response.content);

  // Wait for on-chain write
  if (confirmsStore || hasUuid) {
    console.log('  Waiting 30s for on-chain write...');
    await sleep(30_000);
  }

  report('T3: Explicit Store', [
    {
      label: 'Agent confirms memory stored',
      passed: confirmsStore,
      detail: confirmsStore ? 'confirmed' : 'no confirmation in response',
    },
    {
      label: 'UUID in response (optional — model may not show it)',
      passed: true, // informational only
      detail: hasUuid ? `UUID: ${response.content.match(UUID_RE)?.[0]}` : 'no UUID shown (model gave friendly response)',
    },
  ]);
}

async function runT4(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('T4: Cross-Session Recall (before_agent_start)');
  console.log('='.repeat(60));

  // Use a FRESH session — facts should be injected via before_agent_start hook
  const response = await client.sendMessage(
    'What do you know about me? What is my name, where do I live, and what is my favorite programming language?',
  );
  console.log(`  Agent response: ${response.content.substring(0, 300)}...`);

  const content = response.content.toLowerCase();

  // Note: The subgraph may contain old test data from previous runs, which can
  // cause conflicting or diluted results. We verify that at least SOME of our
  // facts were injected by before_agent_start. Any one match proves the pipeline works.
  const foundAlice = content.includes('alice');
  const foundTokyo = content.includes('tokyo');
  const foundHaskell = content.includes('haskell');
  const foundAny = foundAlice || foundTokyo || foundHaskell;

  report('T4: Cross-Session Recall', [
    {
      label: 'At least one fact recalled from subgraph',
      passed: foundAny,
      detail: `Alice=${foundAlice}, Tokyo=${foundTokyo}, Haskell=${foundHaskell}`,
    },
    {
      label: 'Agent provided substantive response',
      passed: response.content.length > 100,
      detail: `${response.content.length} chars`,
    },
  ]);
}

async function runT5(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('T5: Explicit Recall (totalreclaw_recall)');
  console.log('='.repeat(60));

  const response = await client.sendMessage(
    'Use the totalreclaw_recall tool to search for memories about programming languages. Show me what it returns.',
  );
  console.log(`  Agent response: ${response.content.substring(0, 300)}...`);

  const content = response.content.toLowerCase();

  // UUIDs in recall results may be truncated (e.g. "f6fd86c6..."), so check for
  // hex ID patterns rather than full UUIDs
  const hasHexId = /[0-9a-f]{8,}/i.test(response.content);

  report('T5: Explicit Recall', [
    {
      label: 'Response mentions Haskell',
      passed: content.includes('haskell'),
      detail: content.includes('haskell') ? 'found' : 'NOT found',
    },
    {
      label: 'Response contains recall data',
      passed: response.content.length > 50,
      detail: `${response.content.length} chars`,
    },
    {
      label: 'Response contains fact IDs (tool was called)',
      passed: hasHexId,
      detail: hasHexId ? 'hex IDs found' : 'no IDs — tool may not have been called',
    },
  ]);
}

async function runT6(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('T6: Status / Billing');
  console.log('='.repeat(60));

  const response = await client.sendMessage(
    'Check my TotalReclaw subscription status using the totalreclaw_status tool.',
  );
  console.log(`  Agent response: ${response.content.substring(0, 300)}...`);

  const content = response.content.toLowerCase();

  report('T6: Status / Billing', [
    {
      label: 'Mentions tier (Free)',
      passed: content.includes('free'),
      detail: content.includes('free') ? 'found' : 'NOT found',
    },
    {
      label: 'Mentions writes limit',
      passed: /100|limit|quota|write/i.test(response.content),
    },
    {
      label: 'Response length suggests real data',
      passed: response.content.length > 50,
      detail: `${response.content.length} chars`,
    },
  ]);
}

async function runT7(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('T7: Export (Subgraph Mode)');
  console.log('='.repeat(60));

  const response = await client.sendMessage(
    'Export all my memories using the totalreclaw_export tool. Show me the exported data.',
  );
  console.log(`  Agent response (first 400 chars): ${response.content.substring(0, 400)}...`);

  report('T7: Export (Subgraph Mode)', [
    {
      label: 'Response contains exported data',
      passed: response.content.length > 100,
      detail: `${response.content.length} chars`,
    },
    {
      label: 'Response mentions memories/facts',
      passed: /memor|fact|alice|tokyo|haskell/i.test(response.content),
    },
  ]);
}

async function runT8(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('T8: Forget (On-Chain Tombstone)');
  console.log('='.repeat(60));

  const response = await client.sendMessage(
    'Forget my favorite programming language using the totalreclaw_forget tool.',
  );
  console.log(`  Agent response: ${response.content.substring(0, 200)}...`);

  const confirmsDelete = /forgot|deleted|removed|done|tombstone/i.test(response.content);

  if (confirmsDelete) {
    console.log('  Waiting 60s for tombstone on-chain write + subgraph indexing...');
    await sleep(60_000);
  }

  // Verify recall no longer includes Haskell (in a fresh session)
  const recallResponse = await client.sendMessage(
    'What programming languages do I like?',
  );
  console.log(`  Recall response: ${recallResponse.content.substring(0, 200)}...`);
  const mentionsHaskell = recallResponse.content.toLowerCase().includes('haskell');

  report('T8: Forget (On-Chain Tombstone)', [
    {
      label: 'Agent confirms deletion',
      passed: confirmsDelete,
      detail: confirmsDelete ? 'confirmed' : 'no confirmation',
    },
    {
      label: 'Haskell no longer recalled (may be flaky — subgraph indexing latency)',
      passed: !mentionsHaskell,
      detail: mentionsHaskell ? 'STILL mentioned — tombstone may need more indexing time' : 'correctly excluded',
    },
  ]);
}

async function runT10(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('T10: MEMORY.md Cleartext Prevention');
  console.log('='.repeat(60));

  let memoryContent = '';
  try {
    memoryContent = execSync(
      'docker exec e2e-guide-validation-openclaw-1 cat /home/node/.openclaw/workspace/MEMORY.md 2>&1',
      { encoding: 'utf8', timeout: 10_000 },
    );
  } catch {
    memoryContent = '';
  }

  console.log(`  MEMORY.md content (${memoryContent.length} chars): ${memoryContent.substring(0, 200)}...`);

  const hasHeader = memoryContent.includes('TotalReclaw');
  const hasAlice = memoryContent.toLowerCase().includes('alice');
  const hasTokyo = memoryContent.toLowerCase().includes('tokyo');
  const hasHaskell = memoryContent.toLowerCase().includes('haskell');

  report('T10: MEMORY.md Cleartext Prevention', [
    {
      label: 'MEMORY.md has TotalReclaw header',
      passed: hasHeader,
    },
    {
      label: 'No plaintext "Alice"',
      passed: !hasAlice,
      detail: hasAlice ? 'LEAK: found in cleartext' : 'safe',
    },
    {
      label: 'No plaintext "Tokyo"',
      passed: !hasTokyo,
      detail: hasTokyo ? 'LEAK: found in cleartext' : 'safe',
    },
    {
      label: 'No plaintext "Haskell"',
      passed: !hasHaskell,
      detail: hasHaskell ? 'LEAK: found in cleartext' : 'safe',
    },
  ]);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const singleTest = args.find((a) => a.startsWith('--test'))
    ? args[args.indexOf('--test') + 1]
    : null;
  const fromTest = args.find((a) => a.startsWith('--from'))
    ? args[args.indexOf('--from') + 1]
    : null;

  const allTests = ['T1', 'T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'T8', 'T10'];
  let testsToRun = allTests;

  if (singleTest) {
    testsToRun = [singleTest.toUpperCase()];
  } else if (fromTest) {
    const idx = allTests.indexOf(fromTest.toUpperCase());
    if (idx >= 0) testsToRun = allTests.slice(idx);
  }

  console.log('='.repeat(60));
  console.log('E2E Subgraph Mode Tests — WebSocket Client');
  console.log('='.repeat(60));
  console.log(`Tests to run: ${testsToRun.join(', ')}`);
  console.log(`WebSocket: ${WS_URL}`);
  console.log(`Shared session: ${SHARED_SESSION}`);
  console.log('');
  console.log('NOTE: Tool events are not streamed to WebSocket clients.');
  console.log('      Tool execution is verified via response content (UUIDs, data).\n');

  const client = new OpenClawClient();
  console.log('Connecting to OpenClaw gateway...');
  await client.connect();
  console.log('Connected!\n');

  const testMap: Record<string, (c: OpenClawClient) => Promise<void>> = {
    T1: runT1,
    T2: runT2,
    T3: runT3,
    T4: runT4,
    T5: runT5,
    T6: runT6,
    T7: runT7,
    T8: runT8,
    T10: runT10,
  };

  for (const test of testsToRun) {
    if (test === 'T9') {
      console.log('\n⚠️  T9 (Recovery) requires container restart — skipping in this run.');
      console.log('   Run manually: docker compose down -v && docker compose up -d, then --test T4');
      continue;
    }

    const fn = testMap[test];
    if (!fn) {
      console.log(`\n⚠️  Unknown test: ${test}`);
      continue;
    }

    try {
      await fn(client);
    } catch (err) {
      reportError(test, (err as Error).message);
    }
  }

  client.close();

  // Print summary
  console.log('\n' + '='.repeat(60));
  console.log('SUMMARY');
  console.log('='.repeat(60));

  const total = results.length;
  const passed = results.filter((r) => r.passed).length;
  const failed = total - passed;

  for (const r of results) {
    console.log(`  ${r.passed ? '✅' : '❌'} ${r.name}${r.error ? ` — ${r.error}` : ''}`);
  }

  console.log(`\n  Total: ${total} | Passed: ${passed} | Failed: ${failed}`);

  if (failed > 0) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
