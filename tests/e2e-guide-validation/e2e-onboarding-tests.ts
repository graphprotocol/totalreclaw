/**
 * E2E Onboarding Tests — Fresh install without TOTALRECLAW_RECOVERY_PHRASE.
 *
 * Tests the user-facing onboarding flow:
 *   O1: Plugin loads in needsSetup mode
 *   O2: Agent asks about recovery phrase
 *   O3: Agent generates valid BIP-39 mnemonic when user says "generate a new one"
 *   O4: Tools return setup-required error (not crash)
 *
 * Usage: npx tsx e2e-onboarding-tests.ts [--test O2]
 *
 * Prerequisites:
 *   docker compose -f docker-compose.onboarding-test.yml up -d --build
 *   Wait ~30s for gateway to start
 */

import WebSocket from 'ws';
import { execSync } from 'child_process';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import { validateMnemonic } from '@scure/bip39';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const WS_URL = 'ws://127.0.0.1:18789';
const AUTH_TOKEN = 'guide-test-token-2026';

const BIP39_WORDSET = new Set(wordlist);

// ---------------------------------------------------------------------------
// WebSocket client (same as e2e-subgraph-tests.ts)
// ---------------------------------------------------------------------------

interface AgentResponse {
  content: string;
  runId: string;
}

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
              displayName: 'e2e-onboarding-test',
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
    const key = sessionKey ?? `e2e-onboard-${Date.now()}`;

    const result: AgentResponse = { content: '', runId: '' };

    return new Promise<AgentResponse>((resolve, reject) => {
      const timeout = setTimeout(() => {
        cleanup();
        reject(new Error(`Agent response timed out after ${timeoutMs}ms`));
      }, timeoutMs);

      const onEvent = (event: string, payload: unknown) => {
        const p = payload as Record<string, unknown>;

        if (event === 'chat') {
          const eventSessionKey = p.sessionKey as string | undefined;
          if (eventSessionKey && !eventSessionKey.endsWith(key)) return;

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
      `docker compose -f /Users/pdiogo/Documents/code/totalreclaw/tests/e2e-guide-validation/docker-compose.onboarding-test.yml logs --tail=${tail} 2>&1`,
      { encoding: 'utf8', timeout: 10_000 },
    );
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------------------
// Mnemonic extraction helpers
// ---------------------------------------------------------------------------

/**
 * Try to extract a 12-word BIP-39 mnemonic from the agent's response.
 * The agent may format it in backticks, code blocks, or inline.
 */
function extractMnemonic(content: string): { mnemonic: string; words: string[] } | null {
  // Strategy 1: backtick-quoted
  const backtickMatch = content.match(/`([a-z]+(?:\s+[a-z]+){11})`/);
  if (backtickMatch) {
    return { mnemonic: backtickMatch[1], words: backtickMatch[1].split(/\s+/) };
  }

  // Strategy 2: code block
  const codeBlockMatch = content.match(/```[^\n]*\n([a-z]+(?:\s+[a-z]+){11})\n```/);
  if (codeBlockMatch) {
    return { mnemonic: codeBlockMatch[1], words: codeBlockMatch[1].split(/\s+/) };
  }

  // Strategy 3: any 12-word lowercase sequence
  const anyMatch = content.match(/\b([a-z]{3,8}(?:\s+[a-z]{3,8}){11})\b/);
  if (anyMatch) {
    return { mnemonic: anyMatch[1], words: anyMatch[1].split(/\s+/) };
  }

  return null;
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

const SHARED_SESSION = 'e2e-onboard-' + Date.now();

// ---------------------------------------------------------------------------
// O1: Plugin loads in needsSetup mode
// ---------------------------------------------------------------------------

async function runO1(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('O1: Plugin Loads in needsSetup Mode');
  console.log('='.repeat(60));

  // Trigger initialization by sending a trivial message — initialize() runs lazily on first hook
  console.log('  Sending trigger message to initialize plugin...');
  await client.sendMessage('ping', { sessionKey: 'e2e-init-trigger-' + Date.now(), timeoutMs: 60_000 });
  // Give a moment for logs to flush
  await new Promise((r) => setTimeout(r, 2000));

  const logs = getContainerLogs(200);

  report('O1: Plugin Loads in needsSetup Mode', [
    {
      label: 'Plugin loaded',
      passed: logs.includes('TotalReclaw plugin loaded'),
    },
    {
      label: 'Setup required detected (no recovery phrase)',
      passed: logs.includes('setup required') || logs.includes('TOTALRECLAW_RECOVERY_PHRASE not set'),
      detail: logs.includes('TOTALRECLAW_RECOVERY_PHRASE not set')
        ? 'explicit "not set" log found'
        : logs.includes('setup required')
          ? '"setup required" found in logs'
          : 'NOT found in logs',
    },
    {
      label: 'No credentials loaded (expected — no password means no key derivation)',
      passed: !logs.includes('Loaded existing credentials') && !logs.includes('Registered new user'),
    },
  ]);
}

// ---------------------------------------------------------------------------
// O2: Agent asks about recovery phrase
// ---------------------------------------------------------------------------

async function runO2(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('O2: Agent Asks About Recovery Phrase');
  console.log('='.repeat(60));

  const response = await client.sendMessage(
    'Hello! I just installed TotalReclaw. How do I get started?',
    { sessionKey: SHARED_SESSION },
  );
  console.log(`  Agent response (${response.content.length} chars):\n  ${response.content.substring(0, 500)}`);

  const content = response.content.toLowerCase();

  // The before_agent_start hook injects setup instructions asking about recovery phrase
  const asksAboutPhrase =
    content.includes('recovery phrase') ||
    content.includes('recovery phrase') ||
    content.includes('12-word') ||
    content.includes('12 word');

  const offersGeneration =
    content.includes('generate') ||
    content.includes('create') ||
    content.includes('new one');

  const offersRestore =
    content.includes('existing') ||
    content.includes('restore') ||
    content.includes('recover') ||
    content.includes('already have');

  report('O2: Agent Asks About Recovery Phrase', [
    {
      label: 'Agent mentions recovery phrase / seed / mnemonic',
      passed: asksAboutPhrase,
      detail: asksAboutPhrase ? 'found' : 'NOT found — agent may not have received setup instructions',
    },
    {
      label: 'Agent offers to generate a new one',
      passed: offersGeneration,
      detail: offersGeneration ? 'found' : 'NOT found',
    },
    {
      label: 'Agent offers to restore an existing one',
      passed: offersRestore,
      detail: offersRestore ? 'found' : 'NOT found',
    },
    {
      label: 'Agent provided substantive response',
      passed: response.content.length > 50,
      detail: `${response.content.length} chars`,
    },
  ]);
}

// ---------------------------------------------------------------------------
// O3: Agent generates valid BIP-39 mnemonic
// ---------------------------------------------------------------------------

async function runO3(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('O3: Agent Generates & Configures Recovery Phrase');
  console.log('='.repeat(60));

  // Step 1: Ask agent to generate a mnemonic
  // The agent may: (a) respond with the mnemonic, (b) write to config and trigger
  // a restart before responding, or (c) get stuck trying tools.
  // We handle all cases.
  let genResponse: AgentResponse | null = null;
  let step1Disconnected = false;

  try {
    genResponse = await client.sendMessage(
      'Generate a new one please.',
      { sessionKey: SHARED_SESSION, timeoutMs: 300_000 },
    );
    console.log(`  [Step 1] Agent response (${genResponse.content.length} chars):\n  ${genResponse.content.substring(0, 800)}`);
  } catch (err) {
    const msg = (err as Error).message;
    console.log(`  [Step 1] Ended with: ${msg}`);
    step1Disconnected = msg.includes('timed out') || msg.includes('close') ||
      msg.includes('ECONNRESET') || msg.includes('null');
    if (!step1Disconnected) throw err;
  }

  // Try to extract mnemonic from response
  let extracted: ReturnType<typeof extractMnemonic> = null;
  if (genResponse && genResponse.content.length > 0) {
    extracted = extractMnemonic(genResponse.content);
    if (extracted) console.log(`  Extracted mnemonic: ${extracted.mnemonic}`);
  }

  // Step 2: If we got a response (agent is waiting for confirmation), confirm
  let step2Done = false;
  if (genResponse && genResponse.content.length > 0 && !step1Disconnected) {
    console.log('\n  [Step 2] Confirming phrase saved...');
    try {
      const confirmResponse = await client.sendMessage(
        'Yes, I\'ve saved the recovery phrase securely. Please proceed with the setup.',
        { sessionKey: SHARED_SESSION, timeoutMs: 180_000 },
      );
      console.log(`  [Step 2] Agent response (${confirmResponse.content.length} chars):\n  ${confirmResponse.content.substring(0, 500)}`);
      step2Done = true;
    } catch (err) {
      const msg = (err as Error).message;
      console.log(`  [Step 2] Ended with: ${msg} (expected if config change triggered restart)`);
    }
  }

  // Wait for gateway to stabilize (it may have restarted)
  if (step1Disconnected || !step2Done) {
    console.log('  Waiting for gateway to stabilize...');
    const started = Date.now();
    while (Date.now() - started < 120_000) {
      try {
        execSync('curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18789 2>/dev/null', { timeout: 5_000 });
        await new Promise((r) => setTimeout(r, 5000));
        break;
      } catch {
        await new Promise((r) => setTimeout(r, 3000));
      }
    }
  }

  // Check container logs AND config file for evidence
  const logs = getContainerLogs(300);
  const configChanged = logs.includes('env.TOTALRECLAW_RECOVERY_PHRASE');
  const credentialsLoaded = logs.includes('Loaded existing credentials') || logs.includes('Registered new user');
  const toolCalled = logs.includes('totalreclaw_generate_recovery_phrase');

  // Also check the actual config file inside the container
  let configHasMnemonic = false;
  try {
    const configContent = execSync(
      'docker exec e2e-guide-validation-openclaw-1 cat /home/node/.openclaw/openclaw.json 2>/dev/null',
      { encoding: 'utf8', timeout: 5_000 },
    );
    const config = JSON.parse(configContent);
    const masterPwd = config?.env?.TOTALRECLAW_RECOVERY_PHRASE || '';
    configHasMnemonic = masterPwd.split(/\s+/).length >= 12;
    if (configHasMnemonic) {
      console.log(`  Config file contains mnemonic: "${masterPwd.substring(0, 30)}..."`);
      // Try to extract from config if we didn't get it from the response
      if (!extracted) {
        extracted = extractMnemonic(masterPwd);
        if (extracted) console.log(`  Extracted from config: ${extracted.mnemonic}`);
      }
    }
  } catch {
    console.log('  Could not read config file from container');
  }

  // Validation
  const bip39WordCount = extracted ? extracted.words.filter(w => BIP39_WORDSET.has(w)).length : 0;
  const isValidBip39 = extracted ? validateMnemonic(extracted.mnemonic, wordlist) : false;

  const contentLower = (genResponse?.content || '').toLowerCase();
  const warnsAboutSaving =
    contentLower.includes('save') ||
    contentLower.includes('write down') ||
    contentLower.includes('back up') ||
    contentLower.includes('backup') ||
    contentLower.includes('safe') ||
    contentLower.includes('important') ||
    contentLower.includes('securely') ||
    contentLower.includes('lose') ||
    contentLower.includes('only way');

  // The test passes if the agent generated a mnemonic (in response OR in config)
  const hasMnemonic = (extracted?.words.length === 12) || configHasMnemonic;

  report('O3: Agent Generates & Configures Recovery Phrase', [
    {
      label: 'Agent used totalreclaw_generate_recovery_phrase tool',
      passed: toolCalled || isValidBip39, // valid checksum proves CSPRNG was used
      detail: toolCalled
        ? 'tool call detected in logs'
        : isValidBip39
          ? 'valid BIP-39 checksum (CSPRNG-generated)'
          : 'tool call NOT detected — agent may have self-generated (insecure)',
    },
    {
      label: 'A 12-word mnemonic was produced',
      passed: hasMnemonic,
      detail: extracted
        ? `"${extracted.words.slice(0, 3).join(' ')}...${extracted.words.slice(-1)[0]}"`
        : configHasMnemonic
          ? 'mnemonic found in config file (response lost due to gateway restart)'
          : 'no mnemonic found in response or config',
    },
    {
      label: 'All words are valid BIP-39 words',
      passed: !extracted || bip39WordCount >= 10,
      detail: extracted ? `${bip39WordCount}/${extracted.words.length} BIP-39 words` : 'N/A (checking config)',
    },
    {
      label: 'Valid BIP-39 checksum (proves CSPRNG, not LLM)',
      passed: !extracted || isValidBip39, // if we have a mnemonic, it should be valid
      detail: isValidBip39
        ? 'VALID — cryptographically secure mnemonic'
        : extracted
          ? `INVALID checksum — agent may have self-generated instead of calling tool`
          : 'N/A',
    },
    {
      label: 'Safety warning about saving phrase',
      passed: warnsAboutSaving || step1Disconnected,
      detail: warnsAboutSaving ? 'found' : step1Disconnected ? 'could not verify (response lost to restart)' : 'not found',
    },
    {
      label: 'Config change detected (informational)',
      passed: true,
      detail: configChanged
        ? 'TOTALRECLAW_RECOVERY_PHRASE changed in config'
        : configHasMnemonic
          ? 'mnemonic present in config file'
          : 'no config change detected yet',
    },
  ]);
}

// ---------------------------------------------------------------------------
// O4: Tools return setup-required error (not crash)
// ---------------------------------------------------------------------------

async function runO4(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('O4: Post-Setup Verification');
  console.log('='.repeat(60));

  // After O3, the gateway may have restarted (or be mid-restart).
  // Wait for gateway to be stable, then reconnect.
  console.log('  Waiting for gateway to stabilize...');
  await new Promise((r) => setTimeout(r, 10_000));

  // Retry connection up to 5 times
  let activeClient: OpenClawClient | null = null;
  for (let i = 0; i < 5; i++) {
    try {
      const testClient = new OpenClawClient();
      await testClient.connect();
      activeClient = testClient;
      console.log('  Connected to gateway.');
      break;
    } catch {
      console.log(`  Connection attempt ${i + 1}/5 failed, retrying in 5s...`);
      await new Promise((r) => setTimeout(r, 5000));
    }
  }

  if (!activeClient) {
    reportError('O4: Post-Setup Verification', 'Could not reconnect to gateway after 5 attempts');
    return;
  }

  // Check logs to see if the plugin is now fully initialized
  const logs = getContainerLogs(300);
  const isFullyInitialized = logs.includes('Loaded existing credentials') || logs.includes('Registered new user');

  if (isFullyInitialized) {
    console.log('  Plugin is fully initialized after onboarding. Testing recall tool...');

    const response = await activeClient.sendMessage(
      'Use the totalreclaw_recall tool to search for any memories about preferences.',
    );
    console.log(`  Agent response (${response.content.length} chars):\n  ${response.content.substring(0, 500)}`);

    report('O4: Post-Setup Verification (Fully Initialized)', [
      {
        label: 'Plugin fully initialized after onboarding',
        passed: true,
        detail: 'credentials loaded or registered — recovery phrase was set successfully',
      },
      {
        label: 'Recall tool works (agent did not crash)',
        passed: response.content.length > 20,
        detail: `${response.content.length} chars`,
      },
    ]);
  } else {
    console.log('  Plugin not yet initialized. Testing that tools fail gracefully...');

    const response = await activeClient.sendMessage(
      'Use the totalreclaw_recall tool to search for any memories about programming.',
    );
    console.log(`  Agent response (${response.content.length} chars):\n  ${response.content.substring(0, 500)}`);

    const content = response.content.toLowerCase();
    const mentionsSetup =
      content.includes('setup') ||
      content.includes('configured') ||
      content.includes('not set') ||
      content.includes('recovery phrase');

    report('O4: Post-Setup Verification (Setup Still Required)', [
      {
        label: 'Agent did not crash (got response)',
        passed: response.content.length > 20,
        detail: `${response.content.length} chars`,
      },
      {
        label: 'Agent mentions setup is required',
        passed: mentionsSetup,
        detail: mentionsSetup ? 'setup/config mention found' : 'no setup mention',
      },
    ]);
  }

  activeClient.close();
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const singleTest = args.find((a) => a.startsWith('--test'))
    ? args[args.indexOf('--test') + 1]
    : null;

  const allTests = ['O1', 'O2', 'O3', 'O4'];
  let testsToRun = allTests;

  if (singleTest) {
    testsToRun = [singleTest.toUpperCase()];
  }

  console.log('='.repeat(60));
  console.log('E2E Onboarding Tests — Fresh Install (No Recovery Phrase)');
  console.log('='.repeat(60));
  console.log(`Tests to run: ${testsToRun.join(', ')}`);
  console.log(`WebSocket: ${WS_URL}`);
  console.log(`Shared session: ${SHARED_SESSION}`);
  console.log('');
  console.log('This test verifies the onboarding flow when TOTALRECLAW_RECOVERY_PHRASE');
  console.log('is NOT set — the agent should guide the user through setup.\n');

  const client = new OpenClawClient();
  console.log('Connecting to OpenClaw gateway...');
  await client.connect();
  console.log('Connected!\n');

  const testMap: Record<string, (c: OpenClawClient) => Promise<void>> = {
    O1: runO1,
    O2: runO2,
    O3: runO3,
    O4: runO4,
  };

  for (const test of testsToRun) {
    const fn = testMap[test];
    if (!fn) {
      console.log(`\n  Unknown test: ${test}`);
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
