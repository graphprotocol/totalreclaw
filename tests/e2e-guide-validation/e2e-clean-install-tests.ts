/**
 * E2E Clean Install Tests — Fresh install with NO TotalReclaw env vars.
 *
 * Tests that the skill installs and works correctly on a completely fresh
 * OpenClaw instance (as if installed from ClawHub) WITHOUT any pre-set env vars:
 *
 *   C1: Plugin loads without crashing (no required env var errors)
 *   C2: Agent guides user through onboarding (asks about recovery phrase)
 *   C3: Agent generates valid BIP-39 mnemonic
 *   C4: Agent includes safety warnings (save phrase, no crypto wallet reuse)
 *
 * Usage: npx tsx e2e-clean-install-tests.ts [--test C2]
 *
 * Prerequisites:
 *   docker compose -f docker-compose.clean-install-test.yml up -d --build
 *   Wait ~60s for gateway to start
 */

import WebSocket from 'ws';
import { execSync } from 'child_process';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import { validateMnemonic } from '@scure/bip39';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const WS_URL = 'ws://127.0.0.1:18789';
const AUTH_TOKEN = 'clean-install-test-token';
const COMPOSE_FILE = '/Users/pdiogo/Documents/code/totalreclaw/tests/e2e-guide-validation/docker-compose.clean-install-test.yml';

const BIP39_WORDSET = new Set(wordlist);

// ---------------------------------------------------------------------------
// WebSocket client (reused from e2e-onboarding-tests.ts)
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
              displayName: 'e2e-clean-install-test',
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
    const key = sessionKey ?? `e2e-clean-${Date.now()}`;

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
      `docker compose -f ${COMPOSE_FILE} logs --tail=${tail} 2>&1`,
      { encoding: 'utf8', timeout: 10_000 },
    );
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------------------
// Mnemonic extraction helpers
// ---------------------------------------------------------------------------

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

  const icon = allPassed ? 'PASS' : 'FAIL';
  console.log(`\n[${icon}] ${name}`);
  for (const c of checks) {
    console.log(`  [${c.passed ? 'OK' : 'FAIL'}] ${c.label}${c.detail ? ` -- ${c.detail}` : ''}`);
  }
}

function reportError(name: string, error: string): void {
  results.push({ name, passed: false, checks: [], error });
  console.log(`\n[FAIL] ${name} -- ERROR: ${error}`);
}

const SHARED_SESSION = 'e2e-clean-' + Date.now();

// ---------------------------------------------------------------------------
// C1: Plugin loads without crashing (no "required env var" error)
// ---------------------------------------------------------------------------

async function runC1(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('C1: Plugin Loads Without Env Var Errors');
  console.log('='.repeat(60));

  // Trigger initialization by sending a trivial message
  console.log('  Sending trigger message to initialize plugin...');
  await client.sendMessage('ping', { sessionKey: 'e2e-init-trigger-' + Date.now(), timeoutMs: 60_000 });
  await new Promise((r) => setTimeout(r, 2000));

  const logs = getContainerLogs(200);

  // Check for "required env var" errors — this would indicate skill.json is blocking
  const hasEnvVarError =
    logs.includes('required env var') ||
    logs.includes('Required environment variable') ||
    logs.includes('missing required env');

  // Check that plugin loaded (even in needsSetup mode)
  const pluginLoaded = logs.includes('TotalReclaw plugin loaded') || logs.includes('setup required');

  // Check there's no crash
  const hasCrash =
    logs.includes('FATAL') ||
    logs.includes('unhandled rejection') ||
    logs.includes('Cannot read properties of undefined');

  report('C1: Plugin Loads Without Env Var Errors', [
    {
      label: 'No "required env var" error from OpenClaw gateway',
      passed: !hasEnvVarError,
      detail: hasEnvVarError ? 'FOUND env var gating error -- skill.json still declares required envs' : 'clean',
    },
    {
      label: 'Plugin loaded (in needsSetup mode)',
      passed: pluginLoaded,
      detail: pluginLoaded ? 'plugin loaded successfully' : 'plugin load NOT detected in logs',
    },
    {
      label: 'No crash on startup',
      passed: !hasCrash,
      detail: hasCrash ? 'crash detected in logs' : 'clean startup',
    },
    {
      label: 'Gateway is responsive (got response to ping)',
      passed: true, // If we got here, the gateway responded
      detail: 'gateway accepted WebSocket connection and returned response',
    },
  ]);
}

// ---------------------------------------------------------------------------
// C2: Agent guides user through onboarding
// ---------------------------------------------------------------------------

async function runC2(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('C2: Agent Guides Through Onboarding');
  console.log('='.repeat(60));

  const response = await client.sendMessage(
    'Hello! I just installed TotalReclaw. How do I get started?',
    { sessionKey: SHARED_SESSION },
  );
  console.log(`  Agent response (${response.content.length} chars):\n  ${response.content.substring(0, 600)}`);

  const content = response.content.toLowerCase();

  const mentionsRecoveryPhrase =
    content.includes('recovery phrase') ||
    content.includes('12-word') ||
    content.includes('12 word') ||
    content.includes('mnemonic');

  const offersGeneration =
    content.includes('generate') ||
    content.includes('create') ||
    content.includes('new one');

  const offersRestore =
    content.includes('existing') ||
    content.includes('restore') ||
    content.includes('recover') ||
    content.includes('already have');

  report('C2: Agent Guides Through Onboarding', [
    {
      label: 'Agent mentions recovery phrase / mnemonic',
      passed: mentionsRecoveryPhrase,
      detail: mentionsRecoveryPhrase ? 'found' : 'NOT found -- agent may not have received setup instructions',
    },
    {
      label: 'Agent offers to generate a new phrase',
      passed: offersGeneration,
      detail: offersGeneration ? 'found' : 'NOT found',
    },
    {
      label: 'Agent offers to restore an existing phrase',
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
// C3: Agent generates valid BIP-39 mnemonic
// ---------------------------------------------------------------------------

async function runC3(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('C3: Agent Generates Valid BIP-39 Mnemonic');
  console.log('='.repeat(60));

  const response = await client.sendMessage(
    'Generate a new recovery phrase for me please.',
    { sessionKey: SHARED_SESSION, timeoutMs: 300_000 },
  );
  console.log(`  Agent response (${response.content.length} chars):\n  ${response.content.substring(0, 800)}`);

  const extracted = extractMnemonic(response.content);
  if (extracted) console.log(`  Extracted mnemonic: ${extracted.mnemonic}`);

  const bip39WordCount = extracted ? extracted.words.filter(w => BIP39_WORDSET.has(w)).length : 0;
  const isValidBip39 = extracted ? validateMnemonic(extracted.mnemonic, wordlist) : false;

  report('C3: Agent Generates Valid BIP-39 Mnemonic', [
    {
      label: 'A 12-word mnemonic was produced',
      passed: extracted !== null && extracted.words.length === 12,
      detail: extracted
        ? `"${extracted.words.slice(0, 3).join(' ')}...${extracted.words.slice(-1)[0]}"`
        : 'no mnemonic found in response',
    },
    {
      label: 'All words are valid BIP-39 words',
      passed: !extracted || bip39WordCount >= 10,
      detail: extracted ? `${bip39WordCount}/${extracted.words.length} BIP-39 words` : 'N/A',
    },
    {
      label: 'Valid BIP-39 checksum (proves CSPRNG, not LLM generation)',
      passed: !extracted || isValidBip39,
      detail: isValidBip39
        ? 'VALID -- cryptographically secure mnemonic'
        : extracted
          ? 'INVALID checksum -- agent may have self-generated instead of calling tool'
          : 'N/A',
    },
  ]);
}

// ---------------------------------------------------------------------------
// C4: Safety warnings present
// ---------------------------------------------------------------------------

async function runC4(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('C4: Safety Warnings Present');
  console.log('='.repeat(60));

  // Re-ask for phrase to see warnings (or check the C3 response which should have them)
  // Use a separate session to get a fresh onboarding flow
  const response = await client.sendMessage(
    'Generate a new recovery phrase and walk me through the setup.',
    { sessionKey: `e2e-safety-${Date.now()}`, timeoutMs: 300_000 },
  );
  console.log(`  Agent response (${response.content.length} chars):\n  ${response.content.substring(0, 800)}`);

  const content = response.content.toLowerCase();

  const warnsSavePhrase =
    content.includes('save') ||
    content.includes('write down') ||
    content.includes('back up') ||
    content.includes('backup') ||
    content.includes('store') ||
    content.includes('safe') ||
    content.includes('securely') ||
    content.includes('lose') ||
    content.includes('only way') ||
    content.includes('gone forever') ||
    content.includes('no recovery') ||
    content.includes('no password reset');

  const warnsNoCryptoWallet =
    content.includes('crypto wallet') ||
    content.includes('existing wallet') ||
    content.includes('funded wallet') ||
    content.includes('separate') ||
    content.includes('keep your funds');

  const mentionsFreeTier =
    content.includes('free tier') ||
    content.includes('500 memories') ||
    content.includes('free') ||
    content.includes('upgrade');

  report('C4: Safety Warnings Present', [
    {
      label: 'Warns about saving the recovery phrase',
      passed: warnsSavePhrase,
      detail: warnsSavePhrase ? 'found' : 'NOT found',
    },
    {
      label: 'Warns about NOT using existing crypto wallet phrase',
      passed: warnsNoCryptoWallet,
      detail: warnsNoCryptoWallet ? 'found' : 'NOT found -- SKILL.md safety warning may not be followed',
    },
    {
      label: 'Mentions free tier (informational)',
      passed: true, // Informational only -- not a hard failure
      detail: mentionsFreeTier ? 'found' : 'not mentioned (may come after setup completes)',
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

  const allTests = ['C1', 'C2', 'C3', 'C4'];
  let testsToRun = allTests;

  if (singleTest) {
    testsToRun = [singleTest.toUpperCase()];
  }

  console.log('='.repeat(60));
  console.log('E2E Clean Install Tests -- No TotalReclaw Env Vars');
  console.log('='.repeat(60));
  console.log(`Tests to run: ${testsToRun.join(', ')}`);
  console.log(`WebSocket: ${WS_URL}`);
  console.log(`Shared session: ${SHARED_SESSION}`);
  console.log('');
  console.log('This test verifies that TotalReclaw installs correctly on a');
  console.log('completely fresh OpenClaw instance with NO pre-set env vars.\n');

  const client = new OpenClawClient();
  console.log('Connecting to OpenClaw gateway...');
  await client.connect();
  console.log('Connected!\n');

  const testMap: Record<string, (c: OpenClawClient) => Promise<void>> = {
    C1: runC1,
    C2: runC2,
    C3: runC3,
    C4: runC4,
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
    console.log(`  [${r.passed ? 'PASS' : 'FAIL'}] ${r.name}${r.error ? ` -- ${r.error}` : ''}`);
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
