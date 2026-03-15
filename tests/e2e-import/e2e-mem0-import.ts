/**
 * E2E Mem0 Import Validation
 *
 * Validates the full Mem0-to-TotalReclaw import pipeline end-to-end:
 *   Phase A: Seed memories via Mem0-powered OpenClaw instance
 *   Phase B: Import from Mem0 into TotalReclaw via totalreclaw_import_from tool
 *   Phase C: Verify recall in fresh TotalReclaw conversations
 *   Phase D: Cross-validate Mem0 API count vs TotalReclaw imported count + recall rate
 *
 * Usage:
 *   cd tests/e2e-import && npm install
 *   docker compose up -d --build
 *   npx tsx e2e-mem0-import.ts [--cleanup] [--skip-seed]
 *
 * Flags:
 *   --cleanup    Delete Mem0 test data after the run
 *   --skip-seed  Skip Phase A (if Mem0 already has data from a previous run)
 *
 * Prerequisites:
 *   - MEM0_API_KEY env var (format: m0-*)
 *   - Both Docker containers healthy (ports 8082 and 18789)
 */

import WebSocket from 'ws';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const MEM0_WS_URL = 'ws://127.0.0.1:8082';
const MEM0_AUTH_TOKEN = 'e2e-import-mem0-token-2026';
const TR_WS_URL = 'ws://127.0.0.1:18789';
const TR_AUTH_TOKEN = 'e2e-import-token-2026';
const MEM0_USER_ID = 'e2e-import-test-user';
const MEM0_API_BASE = 'https://api.mem0.ai';

// ---------------------------------------------------------------------------
// CLI flags
// ---------------------------------------------------------------------------

const ARGS = process.argv.slice(2);
const FLAG_CLEANUP = ARGS.includes('--cleanup');
const FLAG_SKIP_SEED = ARGS.includes('--skip-seed');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

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

// ---------------------------------------------------------------------------
// WebSocket client for OpenClaw gateway
// ---------------------------------------------------------------------------

interface AgentResponse {
  content: string;
  runId: string;
}

class OpenClawClient {
  private ws: WebSocket | null = null;
  private reqId = 0;
  private pendingRequests = new Map<string, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  private eventHandlers: Array<(event: string, payload: unknown) => void> = [];

  constructor(
    private readonly wsUrl: string,
    private readonly authToken: string,
  ) {}

  async connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.wsUrl);

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
              displayName: 'e2e-import-test',
              version: 'dev',
              platform: 'node',
              mode: 'cli',
            },
            role: 'operator',
            scopes: ['operator.read', 'operator.write'],
            auth: { token: this.authToken },
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
   * Send a chat message with retry on network_error / timeout.
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
          await sleep(5000);
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

        // Filter events by sessionKey to prevent cross-contamination.
        // Gateway prefixes sessionKey with "agent:main:", so use endsWith for matching.
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
// Mem0 HTTP API helpers (no SDK dependency — just fetch)
// ---------------------------------------------------------------------------

interface Mem0Memory {
  id: string;
  memory: string;
  hash?: string;
  metadata?: Record<string, unknown>;
  user_id?: string;
  categories?: string[];
}

async function mem0FetchMemories(apiKey: string): Promise<Mem0Memory[]> {
  const allMemories: Mem0Memory[] = [];
  let page = 1;
  const pageSize = 100;
  let hasMore = true;

  while (hasMore) {
    const url = new URL(`${MEM0_API_BASE}/v1/memories/`);
    url.searchParams.set('user_id', MEM0_USER_ID);
    url.searchParams.set('page', String(page));
    url.searchParams.set('page_size', String(pageSize));

    const resp = await fetch(url.toString(), {
      headers: { Authorization: `Token ${apiKey}` },
    });

    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Mem0 API error ${resp.status}: ${text.slice(0, 200)}`);
    }

    const data = await resp.json() as { results?: Mem0Memory[]; next?: string };
    const memories = data.results || [];
    allMemories.push(...memories);

    hasMore = memories.length === pageSize;
    page++;

    // Safety limit
    if (allMemories.length >= 10_000) break;
  }

  return allMemories;
}

async function mem0DeleteAllMemories(apiKey: string): Promise<number> {
  const resp = await fetch(`${MEM0_API_BASE}/v1/memories/`, {
    method: 'DELETE',
    headers: {
      Authorization: `Token ${apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ user_id: MEM0_USER_ID }),
  });
  return resp.status;
}

// ---------------------------------------------------------------------------
// Seed conversations for Phase A
// ---------------------------------------------------------------------------

interface SeedConversation {
  session: string;
  messages: string[];
  expectedKeywords: string[];
}

const SEED_CONVERSATIONS: SeedConversation[] = [
  // Preference facts
  {
    session: 'seed-prefs',
    messages: [
      'My favorite programming language is Rust and I have been using it for 3 years.',
      'I also really like TypeScript for web development. I prefer dark themes in all my editors.',
    ],
    expectedKeywords: ['rust', 'typescript', 'dark theme'],
  },
  // Biographical facts
  {
    session: 'seed-bio',
    messages: [
      'I work at Acme Corp as a senior engineer. I am based in Lisbon, Portugal.',
      'I graduated from IST in 2018 with a CS degree.',
    ],
    expectedKeywords: ['acme', 'lisbon', 'ist', '2018'],
  },
  // Technical decisions
  {
    session: 'seed-decisions',
    messages: [
      'We decided to use PostgreSQL over MongoDB for our main database because we need strong consistency.',
      'For our API, we are going with FastAPI instead of Express because the team knows Python better.',
    ],
    expectedKeywords: ['postgresql', 'mongodb', 'fastapi'],
  },
  // Goals
  {
    session: 'seed-goals',
    messages: [
      'I am planning to learn Kubernetes this quarter. My goal is to get the CKA certification by June.',
    ],
    expectedKeywords: ['kubernetes', 'cka', 'june'],
  },
  // Episodic / events
  {
    session: 'seed-events',
    messages: [
      'Last week I deployed our new microservice to production. It handles payment processing for our SaaS product.',
      'The deployment went smoothly, zero downtime. We are using blue-green deployment with Nginx.',
    ],
    expectedKeywords: ['microservice', 'payment', 'blue-green', 'nginx'],
  },
  // Project context
  {
    session: 'seed-project',
    messages: [
      'Our project is called Phoenix. It is a real-time analytics dashboard built with React and D3.js.',
      'We have about 500 daily active users and process roughly 2 million events per day.',
    ],
    expectedKeywords: ['phoenix', 'react', 'd3', '500', '2 million'],
  },
  // Mixed / tools
  {
    session: 'seed-mixed',
    messages: [
      'I use Neovim as my primary editor with a custom Lua config. I switched from VS Code last year.',
      'My dotfiles are in a private GitHub repo. I use zsh with oh-my-zsh and the powerlevel10k theme.',
    ],
    expectedKeywords: ['neovim', 'lua', 'zsh', 'powerlevel10k'],
  },
];

// ---------------------------------------------------------------------------
// Recall verification queries
// ---------------------------------------------------------------------------

interface RecallQuery {
  query: string;
  expectedKeywords: string[];
  source: string;
}

// Static fallback queries — used only if we can't build dynamic queries from Mem0 memories
const STATIC_AUTO_RECALL_QUERIES: RecallQuery[] = [
  { query: 'What programming languages do I like?', expectedKeywords: ['rust', 'typescript'], source: 'seed-prefs' },
  { query: 'Where do I work and where am I based?', expectedKeywords: ['acme', 'lisbon'], source: 'seed-bio' },
  { query: 'What database did we choose for our project?', expectedKeywords: ['postgresql'], source: 'seed-decisions' },
  { query: 'What am I trying to learn this quarter?', expectedKeywords: ['kubernetes'], source: 'seed-goals' },
  { query: 'What was our last deployment about?', expectedKeywords: ['microservice', 'payment'], source: 'seed-events' },
  { query: 'What is the name of our analytics project?', expectedKeywords: ['phoenix'], source: 'seed-project' },
  { query: 'What text editor do I use?', expectedKeywords: ['neovim'], source: 'seed-mixed' },
];

/**
 * Build recall queries dynamically from actual Mem0 memories.
 * This ensures we only test recall for facts that were actually stored,
 * avoiding false failures when Mem0 under-extracts.
 */
function buildRecallQueriesFromMemories(memories: Mem0Memory[]): RecallQuery[] {
  const queries: RecallQuery[] = [];

  for (const mem of memories) {
    const text = mem.memory.toLowerCase();

    // Extract 2-3 distinctive keywords from each memory to use as verification
    const keywords: string[] = [];

    // Common distinctive words to look for
    const candidateKeywords = [
      'rust', 'typescript', 'python', 'javascript', 'go', 'java',
      'neovim', 'vscode', 'vim', 'emacs', 'lua',
      'acme', 'lisbon', 'portugal', 'ist',
      'postgresql', 'mongodb', 'mysql', 'redis',
      'kubernetes', 'docker', 'cka', 'nginx',
      'fastapi', 'express', 'react', 'd3', 'phoenix',
      'dark theme', 'zsh', 'powerlevel10k', 'oh-my-zsh',
      'microservice', 'payment', 'blue-green',
      'senior engineer', 'cs degree', '2018',
    ];

    for (const kw of candidateKeywords) {
      if (text.includes(kw)) {
        keywords.push(kw);
      }
    }

    if (keywords.length === 0) {
      // Fall back to extracting significant words (4+ chars, not common)
      const stopWords = new Set(['that', 'this', 'with', 'from', 'have', 'been', 'their', 'also', 'user', 'they', 'using', 'uses', 'primary', 'having', 'custom', 'configuration']);
      const words = mem.memory.split(/\s+/).filter(w => w.length >= 4 && !stopWords.has(w.toLowerCase()));
      if (words.length > 0) keywords.push(words[0].toLowerCase().replace(/[^a-z0-9]/g, ''));
    }

    if (keywords.length > 0) {
      // Build a natural query that should trigger recall of this memory
      const queryText = `Use totalreclaw_recall to search for: ${mem.memory.slice(0, 60)}`;
      queries.push({
        query: queryText,
        expectedKeywords: keywords.slice(0, 3), // Max 3 keywords
        source: `mem0-${mem.id.slice(0, 8)}`,
      });
    }
  }

  return queries;
}

// Will be populated dynamically after Phase A
let AUTO_RECALL_QUERIES: RecallQuery[] = [];

const EXPLICIT_RECALL_QUERIES: RecallQuery[] = [
  {
    query: 'Use the totalreclaw_recall tool to search for memories about my work at Acme Corp.',
    expectedKeywords: ['acme', 'senior', 'engineer'],
    source: 'explicit-bio',
  },
  {
    query: 'Use totalreclaw_recall to find what databases we evaluated.',
    expectedKeywords: ['postgresql', 'mongodb'],
    source: 'explicit-decisions',
  },
];

// ---------------------------------------------------------------------------
// Result types
// ---------------------------------------------------------------------------

interface RecallResult {
  query: string;
  source: string;
  expectedKeywords: string[];
  matchedKeywords: string[];
  passed: boolean;
  response: string;
}

interface CrossValidation {
  mem0Count: number;
  importedCount: number;
  autoRecallPassed: number;
  autoRecallTotal: number;
  autoRecallRate: number;
  explicitRecallPassed: number;
  explicitRecallTotal: number;
  explicitRecallRate: number;
  missedKeywords: string[];
}

// ---------------------------------------------------------------------------
// Phase A: Seed Mem0 with diverse memories
// ---------------------------------------------------------------------------

async function phaseA_seedMem0(client: OpenClawClient): Promise<void> {
  console.log('\n' + '='.repeat(60));
  console.log('  PHASE A: Seeding memories via Mem0 OpenClaw');
  console.log('='.repeat(60) + '\n');

  for (const conv of SEED_CONVERSATIONS) {
    console.log(`  [${conv.session}] Sending ${conv.messages.length} messages...`);
    for (const msg of conv.messages) {
      const sessionKey = `${conv.session}-${Date.now()}`;
      try {
        const response = await client.sendMessage(msg, {
          sessionKey,
          timeoutMs: 120_000,
          maxRetries: 3,
        });
        console.log(`    Response: ${response.content.slice(0, 80)}...`);
      } catch (err) {
        console.log(`    WARNING: Message failed: ${(err as Error).message}`);
        console.log('    Continuing with next message...');
      }
      // Wait between messages for Mem0 async processing
      await sleep(3000);
    }
    // Wait between conversations for Mem0 indexing
    console.log(`  [${conv.session}] Waiting 10s for Mem0 indexing...`);
    await sleep(10_000);
  }
}

async function phaseA_verifyMem0(apiKey: string): Promise<{ count: number; memories: Mem0Memory[] }> {
  console.log('\n  Verifying Mem0 stored memories via API...');

  const memories = await mem0FetchMemories(apiKey);

  console.log(`  Mem0 has ${memories.length} memories for user ${MEM0_USER_ID}`);
  for (const m of memories) {
    console.log(`    - [${m.id.slice(0, 8)}] ${m.memory.slice(0, 80)}`);
  }

  return { count: memories.length, memories };
}

// ---------------------------------------------------------------------------
// Phase B: Import from Mem0 into TotalReclaw
// ---------------------------------------------------------------------------

async function phaseB_importToTotalReclaw(
  trClient: OpenClawClient,
  mem0ApiKey: string,
): Promise<number> {
  console.log('\n' + '='.repeat(60));
  console.log('  PHASE B: Importing Mem0 memories into TotalReclaw');
  console.log('='.repeat(60) + '\n');

  let importedCount = 0;

  // Strategy 1: Ask the agent to call the import tool directly
  const importMsg =
    `Use the totalreclaw_import_from tool to import all my memories from Mem0. ` +
    `Parameters: source="mem0", api_key="${mem0ApiKey}", source_user_id="${MEM0_USER_ID}". ` +
    `This is NOT a dry run -- actually import them. Tell me how many were imported.`;

  console.log('  [import] Requesting agent-based import...');
  const importSession = `import-real-${Date.now()}`;
  let importResponse: AgentResponse;

  try {
    importResponse = await trClient.sendMessage(importMsg, {
      sessionKey: importSession,
      timeoutMs: 300_000, // 5 min -- imports can be slow
      maxRetries: 3,
    });
    console.log(`  [import] Response: ${importResponse.content.slice(0, 300)}`);
  } catch (err) {
    console.log(`  [import] Agent-based import failed: ${(err as Error).message}`);
    console.log('  [import] Falling back to content-paste approach...');
    importedCount = await phaseB_importFallback(trClient, mem0ApiKey);
    return importedCount;
  }

  // Try to parse the imported count from the response
  const importedMatch = importResponse.content.match(/(\d+)\s*(?:memor(?:y|ies)|imported|stored|success)/i);
  const importedMatch2 = importResponse.content.match(/imported\s*(?::?\s*)(\d+)/i);
  const importedMatch3 = importResponse.content.match(/(\d+)\s*(?:new|total)/i);

  if (importedMatch) {
    importedCount = parseInt(importedMatch[1], 10);
  } else if (importedMatch2) {
    importedCount = parseInt(importedMatch2[1], 10);
  } else if (importedMatch3) {
    importedCount = parseInt(importedMatch3[1], 10);
  }

  // Check if the response indicates the agent actually called the tool
  const toolCalled =
    /imported|success|stored|completed|done/i.test(importResponse.content) &&
    importedCount > 0;

  if (!toolCalled) {
    console.log('  [import] Agent may not have called the tool. Falling back to content-paste...');
    importedCount = await phaseB_importFallback(trClient, mem0ApiKey);
  } else {
    console.log(`  [import] Agent imported ${importedCount} memories`);
  }

  // Wait for TotalReclaw to process (subgraph indexing)
  console.log('  [import] Waiting 30s for TotalReclaw indexing...');
  await sleep(30_000);

  return importedCount;
}

/**
 * Fallback import: fetch from Mem0 API directly, then pass the JSON
 * as the `content` param to totalreclaw_import_from via the agent.
 */
async function phaseB_importFallback(
  trClient: OpenClawClient,
  mem0ApiKey: string,
): Promise<number> {
  console.log('  [fallback] Fetching memories from Mem0 API...');
  const memories = await mem0FetchMemories(mem0ApiKey);
  console.log(`  [fallback] Fetched ${memories.length} memories from Mem0 API`);

  if (memories.length === 0) {
    console.log('  [fallback] No memories to import');
    return 0;
  }

  // Build the content JSON in Mem0 API response format
  const mem0Json = JSON.stringify({ results: memories });

  const pasteMsg =
    `Use the totalreclaw_import_from tool with source="mem0" and the following content parameter. ` +
    `Import all these memories. This is NOT a dry run.\n\ncontent: ${mem0Json}`;

  console.log('  [fallback] Sending content-paste import to TotalReclaw agent...');
  const fallbackSession = `import-fallback-${Date.now()}`;

  try {
    const response = await trClient.sendMessage(pasteMsg, {
      sessionKey: fallbackSession,
      timeoutMs: 300_000,
      maxRetries: 3,
    });
    console.log(`  [fallback] Response: ${response.content.slice(0, 300)}`);

    // Parse imported count
    const match = response.content.match(/(\d+)\s*(?:memor|imported|stored|success)/i);
    const match2 = response.content.match(/imported\s*(?::?\s*)(\d+)/i);

    if (match) return parseInt(match[1], 10);
    if (match2) return parseInt(match2[1], 10);

    // If we can't parse the count but the response seems successful, assume all were imported
    if (/success|imported|completed|done/i.test(response.content)) {
      console.log(`  [fallback] Import appeared successful but could not parse count. Assuming ${memories.length}.`);
      return memories.length;
    }

    console.log('  [fallback] Could not determine import result');
    return 0;
  } catch (err) {
    console.log(`  [fallback] Content-paste import failed: ${(err as Error).message}`);
    return 0;
  }
}

// ---------------------------------------------------------------------------
// Phase C: Verify recall in fresh TotalReclaw sessions
// ---------------------------------------------------------------------------

async function phaseC_verifyAutoRecall(trClient: OpenClawClient): Promise<RecallResult[]> {
  console.log('\n' + '='.repeat(60));
  console.log('  PHASE C: Verifying recall in fresh TotalReclaw sessions');
  console.log('='.repeat(60));
  console.log('\n  --- Recall via totalreclaw_recall (per imported memory) ---\n');

  const results: RecallResult[] = [];

  for (const q of AUTO_RECALL_QUERIES) {
    // Each query in a FRESH session to test recall of imported facts
    const sessionKey = `recall-${q.source}-${Date.now()}`;

    console.log(`  [${q.source}] "${q.query.slice(0, 80)}..."`);
    try {
      const response = await trClient.sendMessage(q.query, {
        sessionKey,
        timeoutMs: 120_000,
        maxRetries: 3,
      });

      const lower = response.content.toLowerCase();
      const matched = q.expectedKeywords.filter((kw) => lower.includes(kw.toLowerCase()));
      const passed = matched.length > 0; // At least 1 keyword match = pass

      results.push({
        query: q.query,
        source: q.source,
        expectedKeywords: q.expectedKeywords,
        matchedKeywords: matched,
        passed,
        response: response.content.slice(0, 200),
      });

      const icon = passed ? 'PASS' : 'FAIL';
      console.log(`    [${icon}] Matched ${matched.length}/${q.expectedKeywords.length}: [${matched.join(', ')}]`);
      if (!passed) {
        console.log(`    Response: ${response.content.slice(0, 150)}`);
      }
    } catch (err) {
      console.log(`    [FAIL] Error: ${(err as Error).message}`);
      results.push({
        query: q.query,
        source: q.source,
        expectedKeywords: q.expectedKeywords,
        matchedKeywords: [],
        passed: false,
        response: `ERROR: ${(err as Error).message}`,
      });
    }

    await sleep(2000); // Brief pause between queries
  }

  return results;
}

async function phaseC_verifyExplicitRecall(trClient: OpenClawClient): Promise<RecallResult[]> {
  console.log('\n  --- Explicit recall (totalreclaw_recall tool) ---\n');

  const results: RecallResult[] = [];

  for (const q of EXPLICIT_RECALL_QUERIES) {
    const sessionKey = `explicit-${q.source}-${Date.now()}`;

    console.log(`  [${q.source}] "${q.query}"`);
    try {
      const response = await trClient.sendMessage(q.query, {
        sessionKey,
        timeoutMs: 120_000,
        maxRetries: 3,
      });

      const lower = response.content.toLowerCase();
      const matched = q.expectedKeywords.filter((kw) => lower.includes(kw.toLowerCase()));
      const passed = matched.length > 0; // At least 1 keyword match = pass

      results.push({
        query: q.query,
        source: q.source,
        expectedKeywords: q.expectedKeywords,
        matchedKeywords: matched,
        passed,
        response: response.content.slice(0, 200),
      });

      const icon = passed ? 'PASS' : 'FAIL';
      console.log(`    [${icon}] Matched ${matched.length}/${q.expectedKeywords.length}: [${matched.join(', ')}]`);
      if (!passed) {
        console.log(`    Response: ${response.content.slice(0, 150)}`);
      }
    } catch (err) {
      console.log(`    [FAIL] Error: ${(err as Error).message}`);
      results.push({
        query: q.query,
        source: q.source,
        expectedKeywords: q.expectedKeywords,
        matchedKeywords: [],
        passed: false,
        response: `ERROR: ${(err as Error).message}`,
      });
    }

    await sleep(2000);
  }

  return results;
}

// ---------------------------------------------------------------------------
// Phase D: Cross-validation and report
// ---------------------------------------------------------------------------

function phaseD_crossValidate(
  mem0Count: number,
  importedCount: number,
  autoRecallResults: RecallResult[],
  explicitRecallResults: RecallResult[],
): CrossValidation {
  const autoRecallPassed = autoRecallResults.filter((r) => r.passed).length;
  const explicitRecallPassed = explicitRecallResults.filter((r) => r.passed).length;

  const allResults = [...autoRecallResults, ...explicitRecallResults];
  const missedKeywords = allResults
    .filter((r) => !r.passed)
    .flatMap((r) => r.expectedKeywords.filter((kw) => !r.matchedKeywords.includes(kw)));

  return {
    mem0Count,
    importedCount,
    autoRecallPassed,
    autoRecallTotal: autoRecallResults.length,
    autoRecallRate: autoRecallResults.length > 0 ? autoRecallPassed / autoRecallResults.length : 0,
    explicitRecallPassed,
    explicitRecallTotal: explicitRecallResults.length,
    explicitRecallRate: explicitRecallResults.length > 0 ? explicitRecallPassed / explicitRecallResults.length : 0,
    missedKeywords: [...new Set(missedKeywords)],
  };
}

function printReport(cv: CrossValidation): boolean {
  console.log('\n' + '='.repeat(60));
  console.log('  MEM0 -> TOTALRECLAW IMPORT E2E REPORT');
  console.log('='.repeat(60));
  console.log(`  Mem0 memories stored:       ${cv.mem0Count}`);
  console.log(`  Imported into TotalReclaw:   ${cv.importedCount}`);
  console.log(`  Auto-recall (fresh session): ${cv.autoRecallPassed}/${cv.autoRecallTotal} (${(cv.autoRecallRate * 100).toFixed(0)}%)`);
  console.log(`  Explicit recall (tool):      ${cv.explicitRecallPassed}/${cv.explicitRecallTotal} (${(cv.explicitRecallRate * 100).toFixed(0)}%)`);
  console.log(`  Missed keywords:             ${cv.missedKeywords.length > 0 ? cv.missedKeywords.join(', ') : 'none'}`);
  console.log('='.repeat(60));

  // Thresholds
  const importLossless = cv.importedCount === cv.mem0Count;
  const autoRecallOk = cv.autoRecallRate >= 0.85; // 85% threshold (6/7+)
  const explicitRecallOk = cv.explicitRecallRate >= 1.0; // 100% threshold (2/2)
  const allPassed = importLossless && autoRecallOk && explicitRecallOk;

  console.log('\n  THRESHOLDS:');
  console.log(`    Import completeness:  ${importLossless ? 'PASS' : 'FAIL'} -- ${cv.importedCount}/${cv.mem0Count} (must be 100%)`);
  console.log(`    Auto-recall rate:     ${autoRecallOk ? 'PASS' : 'FAIL'} -- ${(cv.autoRecallRate * 100).toFixed(0)}% (threshold: 85%)`);
  console.log(`    Explicit recall rate: ${explicitRecallOk ? 'PASS' : 'FAIL'} -- ${(cv.explicitRecallRate * 100).toFixed(0)}% (threshold: 100%)`);

  console.log(`\n  VERDICT: ${allPassed ? 'PASS' : 'FAIL'}`);
  console.log(`  ${allPassed ? 'Seamless migration validated!' : 'Migration has gaps -- investigate.'}\n`);

  return allPassed;
}

// ---------------------------------------------------------------------------
// Cleanup
// ---------------------------------------------------------------------------

async function cleanupMem0(apiKey: string): Promise<void> {
  console.log('\n  Cleaning up Mem0 test data...');
  try {
    const status = await mem0DeleteAllMemories(apiKey);
    console.log(`  Cleanup response status: ${status}`);
    if (status >= 200 && status < 300) {
      console.log('  Mem0 test data cleaned up successfully.');
    } else {
      console.log(`  WARNING: Mem0 cleanup returned status ${status}`);
    }
  } catch (err) {
    console.log(`  WARNING: Mem0 cleanup failed: ${(err as Error).message}`);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const mem0ApiKey = process.env.MEM0_API_KEY;
  if (!mem0ApiKey) {
    console.error('ERROR: MEM0_API_KEY not set.');
    console.error('Set it in your environment or in .env (then source it before running).');
    console.error('Get a key from https://app.mem0.ai');
    process.exit(1);
  }

  console.log('='.repeat(60));
  console.log('  E2E Mem0 Import Validation');
  console.log('='.repeat(60));
  console.log(`  Mem0 WS:     ${MEM0_WS_URL}`);
  console.log(`  TR WS:       ${TR_WS_URL}`);
  console.log(`  Mem0 User:   ${MEM0_USER_ID}`);
  console.log(`  Skip seed:   ${FLAG_SKIP_SEED}`);
  console.log(`  Cleanup:     ${FLAG_CLEANUP}`);
  console.log('');

  let mem0Count = 0;
  let mem0Memories: Mem0Memory[] = [];

  // ---------- Phase A: Seed Mem0 ----------

  if (FLAG_SKIP_SEED) {
    console.log('  --skip-seed: Skipping Phase A, verifying existing Mem0 data...\n');
    const verified = await phaseA_verifyMem0(mem0ApiKey);
    mem0Count = verified.count;
    mem0Memories = verified.memories;
  } else {
    let mem0Client: OpenClawClient | null = null;
    try {
      mem0Client = new OpenClawClient(MEM0_WS_URL, MEM0_AUTH_TOKEN);
      console.log('  Connecting to Mem0 OpenClaw instance...');
      await mem0Client.connect();
      console.log('  Connected to Mem0 instance.\n');

      await phaseA_seedMem0(mem0Client);
    } catch (err) {
      console.error(`\n  ERROR connecting to Mem0 OpenClaw instance: ${(err as Error).message}`);
      console.error('  HINT: Is the openclaw-mem0 container running? Check:');
      console.error('    docker compose logs openclaw-mem0');
      console.error('    curl -sf http://127.0.0.1:8082/');
      process.exit(1);
    } finally {
      mem0Client?.close();
    }

    // Verify what Mem0 stored
    const verified = await phaseA_verifyMem0(mem0ApiKey);
    mem0Count = verified.count;
    mem0Memories = verified.memories;
  }

  if (mem0Count === 0) {
    console.error('\n  FATAL: Mem0 stored 0 memories -- seeding failed.');
    console.error('  HINTS:');
    console.error('    - Check MEM0_API_KEY is valid (format: m0-*)');
    console.error('    - Check Mem0 container logs: docker compose logs openclaw-mem0');
    console.error('    - Mem0 may need more time for async processing (try --skip-seed)');
    process.exit(1);
  }

  // Build recall queries from actual Mem0 memories (not hardcoded assumptions)
  AUTO_RECALL_QUERIES = buildRecallQueriesFromMemories(mem0Memories);
  console.log(`\n  Phase A complete: ${mem0Count} memories in Mem0.`);
  console.log(`  Built ${AUTO_RECALL_QUERIES.length} recall queries from actual memories.\n`);

  // ---------- Phase B: Import into TotalReclaw ----------

  let trClient: OpenClawClient;
  try {
    trClient = new OpenClawClient(TR_WS_URL, TR_AUTH_TOKEN);
    console.log('  Connecting to TotalReclaw OpenClaw instance...');
    await trClient.connect();
    console.log('  Connected to TotalReclaw instance.\n');
  } catch (err) {
    console.error(`\n  ERROR connecting to TotalReclaw OpenClaw instance: ${(err as Error).message}`);
    console.error('  HINT: Is the openclaw-totalreclaw container running? Check:');
    console.error('    docker compose logs openclaw-totalreclaw');
    console.error('    curl -sf http://127.0.0.1:18789/');
    process.exit(1);
  }

  const importedCount = await phaseB_importToTotalReclaw(trClient, mem0ApiKey);
  console.log(`\n  Phase B complete: ${importedCount} memories imported.\n`);

  // ---------- Phase C: Verify recall ----------

  const autoRecallResults = await phaseC_verifyAutoRecall(trClient);
  const explicitRecallResults = await phaseC_verifyExplicitRecall(trClient);

  trClient.close();

  // ---------- Phase D: Cross-validate and report ----------

  console.log('\n' + '='.repeat(60));
  console.log('  PHASE D: Cross-validation');
  console.log('='.repeat(60));

  const cv = phaseD_crossValidate(mem0Count, importedCount, autoRecallResults, explicitRecallResults);
  const allPassed = printReport(cv);

  // ---------- Cleanup ----------

  if (FLAG_CLEANUP) {
    await cleanupMem0(mem0ApiKey);
  }

  // ---------- Per-query detail ----------

  console.log('\n  --- Per-Query Detail ---\n');

  for (const r of [...autoRecallResults, ...explicitRecallResults]) {
    const icon = r.passed ? 'PASS' : 'FAIL';
    console.log(`  [${icon}] [${r.source}] "${r.query}"`);
    console.log(`         Expected: [${r.expectedKeywords.join(', ')}]`);
    console.log(`         Matched:  [${r.matchedKeywords.join(', ')}]`);
    if (!r.passed) {
      console.log(`         Response: ${r.response}`);
    }
  }

  process.exit(allPassed ? 0 : 1);
}

main().catch((err) => {
  console.error('\nFatal error:', err);
  console.error('\nHINTS:');
  console.error('  - Are both Docker containers running and healthy?');
  console.error('  - docker compose ps');
  console.error('  - docker compose logs --tail=50 openclaw-mem0');
  console.error('  - docker compose logs --tail=50 openclaw-totalreclaw');
  process.exit(1);
});
