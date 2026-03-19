#!/usr/bin/env node
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  GetPromptRequestSchema,
  ListPromptsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';

import { TotalReclaw } from '@totalreclaw/client';
import {
  rememberToolDefinition,
  recallToolDefinition,
  forgetToolDefinition,
  exportToolDefinition,
  importToolDefinition,
  importFromToolDefinition,
  consolidateToolDefinition,
  handleRemember,
  handleRecall,
  handleForget,
  handleExport,
  handleImport,
  handleImportFrom,
  handleConsolidate,
  statusToolDefinition,
  upgradeToolDefinition,
  handleStatus,
  handleUpgrade,
} from './tools/index.js';
import { setOnRememberCallback } from './tools/remember.js';
import {
  findNearDuplicate,
  shouldSupersede,
  clusterFacts,
  getStoreDedupThreshold,
  getConsolidationThreshold,
  STORE_DEDUP_MAX_CANDIDATES,
  type DecryptedCandidate,
} from './consolidation.js';
import {
  SERVER_INSTRUCTIONS,
  PROMPT_DEFINITIONS,
  getPromptMessages,
} from './prompts.js';
import {
  memoryContextResource,
  readMemoryContext,
  invalidateMemoryContextCache,
} from './resources/index.js';

// Subgraph imports (lazy usage -- only when managed service is active)
import {
  deriveKeys,
  deriveLshSeed,
  computeAuthKeyHash,
  encrypt,
  decrypt,
  generateBlindIndices,
  generateContentFingerprint,
} from './subgraph/crypto.js';
import { LSHHasher } from './subgraph/lsh.js';
import { generateEmbedding, getEmbeddingDims } from './subgraph/embedding.js';
import {
  rerank,
  detectQueryIntent,
  INTENT_WEIGHTS,
} from './subgraph/reranker.js';
import {
  submitFactOnChain,
  encodeFactProtobuf,
  getSubgraphConfig,
  isSubgraphMode,
  type FactPayload,
  type SubgraphStoreConfig,
} from './subgraph/store.js';
import { searchSubgraph } from './subgraph/search.js';

import { validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import { mnemonicToAccount } from 'viem/accounts';
import { createPublicClient, http, type Address } from 'viem';
import { gnosis, gnosisChiado } from 'viem/chains';
import { toSimpleSmartAccount } from 'permissionless/accounts';
import { entryPoint07Address } from 'viem/account-abstraction';
import crypto from 'node:crypto';

// ── Configuration ───────────────────────────────────────────────────────────

const SERVER_URL = process.env.TOTALRECLAW_SERVER_URL || 'http://127.0.0.1:8080';
const MASTER_PASSWORD = process.env.TOTALRECLAW_MASTER_PASSWORD;

// Store-time near-duplicate detection (consolidation module)
const STORE_DEDUP_ENABLED = process.env.TOTALRECLAW_STORE_DEDUP !== 'false';

// ── Server mode detection ───────────────────────────────────────────────────

type ServerMode = 'http' | 'subgraph';

interface SubgraphState {
  mode: 'subgraph';
  mnemonic: string;
  authKey: Buffer;
  encryptionKey: Buffer;
  dedupKey: Buffer;
  salt: Buffer;
  lshHasher: LSHHasher;
  serverUrl: string;
  smartAccountAddress: string;
}

let subgraphState: SubgraphState | null = null;

/**
 * Detect server mode based on environment configuration.
 *
 * Managed service (subgraph) mode is the default when a valid BIP-39 mnemonic
 * is provided as TOTALRECLAW_MASTER_PASSWORD.
 *
 * Self-hosted mode (HTTP) requires TOTALRECLAW_SELF_HOSTED=true.
 * Defaults to managed service otherwise.
 */
function detectServerMode(): ServerMode {
  if (!MASTER_PASSWORD) return 'http';
  // Self-hosted mode is opt-in: requires explicit TOTALRECLAW_SELF_HOSTED=true
  if (process.env.TOTALRECLAW_SELF_HOSTED === 'true') return 'http';

  const words = MASTER_PASSWORD.trim().split(/\s+/);
  if (words.length !== 12 && words.length !== 24) return 'http';
  if (!validateMnemonic(MASTER_PASSWORD.trim(), wordlist)) return 'http';

  return 'subgraph';
}

/**
 * Initialize subgraph state from the BIP-39 mnemonic.
 * Derives all cryptographic keys and creates the LSH hasher.
 */
async function initSubgraphState(): Promise<SubgraphState> {
  const mnemonic = MASTER_PASSWORD!.trim();
  const { authKey, encryptionKey, dedupKey, salt } = deriveKeys(mnemonic);

  // Derive LSH seed and create hasher
  const lshSeed = deriveLshSeed(mnemonic, salt);
  const dims = getEmbeddingDims(); // 384 for bge-small-en-v1.5
  const lshHasher = new LSHHasher(lshSeed, dims);

  // Derive Smart Account address via relay bundler proxy (same chain view as Pimlico).
  // This does an eth_call to the EntryPoint to compute the counterfactual CREATE2 address.
  const chainId = parseInt(process.env.TOTALRECLAW_CHAIN_ID || '10200');
  const chain = chainId === 100 ? gnosis : gnosisChiado;
  const bundlerRpcUrl = `${SERVER_URL}/v1/bundler`;
  const ownerAccount = mnemonicToAccount(mnemonic);
  const entryPointAddr = (process.env.TOTALRECLAW_ENTRYPOINT_ADDRESS || entryPoint07Address) as Address;
  const authKeyHex = Buffer.from(authKey).toString('hex');

  // Use public RPC for the eth_call (Pimlico bundler doesn't support eth_call).
  // This is only used to derive the counterfactual Smart Account address via CREATE2 —
  // the result is deterministic and doesn't depend on chain head position.
  const publicClient = createPublicClient({
    chain,
    transport: http(),
  });

  const smartAccount = await toSimpleSmartAccount({
    // @ts-ignore - viem/permissionless type intersection conflict
    client: publicClient,
    owner: ownerAccount,
    entryPoint: {
      address: entryPointAddr,
      version: '0.7',
    },
  });

  const smartAccountAddress = smartAccount.address.toLowerCase();

  return {
    mode: 'subgraph',
    mnemonic,
    authKey,
    encryptionKey,
    dedupKey,
    salt,
    lshHasher,
    serverUrl: SERVER_URL,
    smartAccountAddress,
  };
}

// ── HTTP mode client state ──────────────────────────────────────────────────

interface ClientState {
  client: TotalReclaw | null;
  userId: string | null;
  salt: Buffer | null;
}

const clientState: ClientState = {
  client: null,
  userId: null,
  salt: null,
};

async function getClient(): Promise<TotalReclaw> {
  if (clientState.client && clientState.client.isReady()) {
    return clientState.client;
  }

  const client = new TotalReclaw({ serverUrl: SERVER_URL });
  await client.init();

  const credentialsPath = process.env.TOTALRECLAW_CREDENTIALS_PATH || '/workspace/.totalreclaw/credentials.json';

  if (await credentialsExist(credentialsPath)) {
    const credentials = await loadCredentials(credentialsPath);
    await client.login(credentials.userId, MASTER_PASSWORD || 'default-password', credentials.salt);
    clientState.userId = credentials.userId;
    clientState.salt = credentials.salt;
  } else {
    const userId = await client.register(MASTER_PASSWORD || 'default-password');
    clientState.userId = userId;
    clientState.salt = client.getSalt();
    await saveCredentials(credentialsPath, {
      userId: clientState.userId!,
      salt: clientState.salt!,
    });
  }

  clientState.client = client;
  return client;
}

async function credentialsExist(path: string): Promise<boolean> {
  try {
    const fs = await import('fs/promises');
    await fs.access(path);
    return true;
  } catch {
    return false;
  }
}

interface StoredCredentials {
  userId: string;
  salt: string;
}

async function loadCredentials(path: string): Promise<{ userId: string; salt: Buffer }> {
  const fs = await import('fs/promises');
  const data = await fs.readFile(path, 'utf-8');
  const parsed = JSON.parse(data) as StoredCredentials;
  return {
    userId: parsed.userId,
    salt: Buffer.from(parsed.salt, 'hex'),
  };
}

async function saveCredentials(path: string, credentials: { userId: string; salt: Buffer }): Promise<void> {
  const fs = await import('fs/promises');
  const dir = path.substring(0, path.lastIndexOf('/'));
  await fs.mkdir(dir, { recursive: true }).catch(() => {});
  const data: StoredCredentials = {
    userId: credentials.userId,
    salt: credentials.salt.toString('hex'),
  };
  await fs.writeFile(path, JSON.stringify(data, null, 2), 'utf-8');
}

// ── Subgraph handlers ───────────────────────────────────────────────────────

/**
 * Encrypt an embedding vector for on-chain storage.
 *
 * Concatenates the float array into a Buffer, encrypts with AES-256-GCM,
 * and returns base64. The subgraph stores this as a string field.
 */
function encryptEmbedding(embedding: number[], encryptionKey: Buffer): string {
  const buf = Buffer.alloc(embedding.length * 4);
  for (let i = 0; i < embedding.length; i++) {
    buf.writeFloatLE(embedding[i], i * 4);
  }
  return encrypt(buf.toString('base64'), encryptionKey);
}

/**
 * Decrypt an encrypted embedding back to a number array.
 */
function decryptEmbedding(encryptedEmbedding: string, encryptionKey: Buffer): number[] {
  const decryptedBase64 = decrypt(encryptedEmbedding, encryptionKey);
  const buf = Buffer.from(decryptedBase64, 'base64');
  const floats: number[] = [];
  for (let i = 0; i < buf.length; i += 4) {
    floats.push(buf.readFloatLE(i));
  }
  return floats;
}

/**
 * Handle remember in subgraph mode: encrypt, generate indices, submit on-chain.
 */
async function handleRememberSubgraph(
  state: SubgraphState,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as Record<string, unknown>;

  // Support both single fact and batch mode (single only for subgraph for now)
  const factText = (input?.fact as string) || '';
  const factsArray = input?.facts as Array<{ text: string; importance?: number; type?: string }> | undefined;

  const textsToStore: Array<{ text: string; importance: number }> = [];

  if (factsArray && Array.isArray(factsArray) && factsArray.length > 0) {
    for (const f of factsArray) {
      if (f.text && typeof f.text === 'string' && f.text.trim().length > 0) {
        textsToStore.push({ text: f.text.trim(), importance: f.importance ?? 5 });
      }
    }
  } else if (factText && typeof factText === 'string' && factText.trim().length > 0) {
    textsToStore.push({
      text: factText.trim(),
      importance: (input?.importance as number) ?? 5,
    });
  }

  if (textsToStore.length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          error: 'Invalid input: provide either a "fact" string or a "facts" array',
        }),
      }],
    };
  }

  // Determine if this is an explicit remember (user-initiated) — always supersede.
  // For batch mode (multiple facts from auto-extraction), apply full skip/supersede logic.
  const isExplicitRemember = !!(input?.fact as string);

  const results: Array<{ success: boolean; fact_id: string; tx_hash?: string; action?: string }> = [];
  let dedupSkipped = 0;
  let dedupSuperseded = 0;

  for (const item of textsToStore) {
    try {
      // 1. Generate blind indices (word-based)
      const wordIndices = generateBlindIndices(item.text);

      // 2. Generate embedding and LSH indices
      const embedding = await generateEmbedding(item.text);
      const lshIndices = state.lshHasher.hash(embedding);

      // 3. Combine word + LSH indices
      const allIndices = [...wordIndices, ...lshIndices];

      // Store-time dedup: search for near-duplicates before storing
      let effectiveImportance = item.importance;
      if (STORE_DEDUP_ENABLED) {
        try {
          const maxCandidates = STORE_DEDUP_MAX_CANDIDATES;
          const candidates = await searchSubgraph(
            state.smartAccountAddress,
            allIndices,
            maxCandidates,
            state.serverUrl,
            Buffer.from(state.authKey).toString('hex'),
          );

          if (candidates.length > 0) {
            // Decrypt candidates and extract embeddings
            const decryptedCandidates: DecryptedCandidate[] = [];
            for (const c of candidates) {
              try {
                const blobBase64 = Buffer.from(c.encryptedBlob, 'hex').toString('base64');
                const text = decrypt(blobBase64, state.encryptionKey);

                let candEmbedding: number[] | null = null;
                if (c.encryptedEmbedding) {
                  try {
                    candEmbedding = decryptEmbedding(c.encryptedEmbedding, state.encryptionKey);
                  } catch { /* skip */ }
                }

                decryptedCandidates.push({
                  id: c.id,
                  text,
                  embedding: candEmbedding,
                  importance: Math.round(parseFloat(c.decayScore) * 10) || 5,
                  decayScore: parseFloat(c.decayScore) || 0.5,
                  createdAt: parseInt(c.timestamp) || 0,
                  version: 1,
                });
              } catch { /* skip undecryptable */ }
            }

            const dupMatch = findNearDuplicate(embedding, decryptedCandidates, getStoreDedupThreshold());
            if (dupMatch) {
              if (isExplicitRemember) {
                // Explicit remember: always supersede — submit tombstone for old fact
                effectiveImportance = Math.max(item.importance, dupMatch.existingFact.importance);
                try {
                  const tombstonePayload: FactPayload = {
                    id: dupMatch.existingFact.id,
                    timestamp: new Date().toISOString(),
                    owner: state.smartAccountAddress,
                    encryptedBlob: Buffer.from('tombstone').toString('hex'),
                    blindIndices: [],
                    decayScore: 0,
                    source: 'mcp_dedup',
                    contentFp: '',
                    agentId: 'mcp-server',
                  };
                  const tombProtobuf = encodeFactProtobuf(tombstonePayload);
                  const tombConfig = getSubgraphConfig({
                    relayUrl: state.serverUrl,
                    mnemonic: state.mnemonic,
                    authKeyHex: Buffer.from(state.authKey).toString('hex'),
                    walletAddress: state.smartAccountAddress,
                  });
                  await submitFactOnChain(tombProtobuf, tombConfig);
                  console.error(`Store-time dedup: superseded ${dupMatch.existingFact.id} (sim=${dupMatch.similarity.toFixed(3)})`);
                } catch {
                  console.error(`Store-time dedup: failed to tombstone superseded fact ${dupMatch.existingFact.id}`);
                }
                dedupSuperseded++;
              } else {
                // Batch mode: apply shouldSupersede logic
                const action = shouldSupersede(item.importance, dupMatch.existingFact);
                if (action === 'skip') {
                  console.error(`Store-time dedup: skipping "${item.text.slice(0, 60)}..." (sim=${dupMatch.similarity.toFixed(3)})`);
                  results.push({ success: true, fact_id: '', action: 'skipped_dedup' });
                  dedupSkipped++;
                  continue;
                }
                // action === 'supersede'
                effectiveImportance = Math.max(item.importance, dupMatch.existingFact.importance);
                try {
                  const tombstonePayload: FactPayload = {
                    id: dupMatch.existingFact.id,
                    timestamp: new Date().toISOString(),
                    owner: state.smartAccountAddress,
                    encryptedBlob: Buffer.from('tombstone').toString('hex'),
                    blindIndices: [],
                    decayScore: 0,
                    source: 'mcp_dedup',
                    contentFp: '',
                    agentId: 'mcp-server',
                  };
                  const tombProtobuf = encodeFactProtobuf(tombstonePayload);
                  const tombConfig = getSubgraphConfig({
                    relayUrl: state.serverUrl,
                    mnemonic: state.mnemonic,
                    authKeyHex: Buffer.from(state.authKey).toString('hex'),
                    walletAddress: state.smartAccountAddress,
                  });
                  await submitFactOnChain(tombProtobuf, tombConfig);
                  console.error(`Store-time dedup: superseded ${dupMatch.existingFact.id} (sim=${dupMatch.similarity.toFixed(3)})`);
                } catch {
                  console.error(`Store-time dedup: failed to tombstone superseded fact ${dupMatch.existingFact.id}`);
                }
                dedupSuperseded++;
              }
            }
          }
        } catch (dedupErr) {
          // Fail-open: dedup failure should not prevent storing the fact
          console.error(`Store-time dedup search failed: ${dedupErr instanceof Error ? dedupErr.message : String(dedupErr)}`);
        }
      }

      // 4. Encrypt the fact text
      const encryptedBlob = encrypt(item.text, state.encryptionKey);

      // 5. Generate content fingerprint for dedup
      const contentFp = generateContentFingerprint(item.text, state.dedupKey);

      // 6. Encrypt the embedding for on-chain storage
      const encryptedEmb = encryptEmbedding(embedding, state.encryptionKey);

      // 7. Build fact payload
      const factId = crypto.randomUUID();
      const factPayload: FactPayload = {
        id: factId,
        timestamp: new Date().toISOString(),
        owner: state.smartAccountAddress,
        encryptedBlob: Buffer.from(encryptedBlob, 'base64').toString('hex'),
        blindIndices: allIndices,
        decayScore: effectiveImportance / 10,
        source: 'mcp_remember',
        contentFp,
        agentId: 'mcp-server',
        encryptedEmbedding: encryptedEmb,
      };

      // 8. Encode as protobuf and submit on-chain
      const protobuf = encodeFactProtobuf(factPayload);
      const config = getSubgraphConfig({
        relayUrl: state.serverUrl,
        mnemonic: state.mnemonic,
        authKeyHex: Buffer.from(state.authKey).toString('hex'),
        walletAddress: state.smartAccountAddress,
      });

      const { txHash, success } = await submitFactOnChain(protobuf, config);

      results.push({ success, fact_id: factId, tx_hash: txHash, action: dedupSuperseded > 0 ? 'superseded' : 'created' });
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error';
      results.push({ success: false, fact_id: '', tx_hash: undefined });
      console.error(`Failed to store fact on-chain: ${message}`);
    }
  }

  const created = results.filter(r => r.success && r.action !== 'skipped_dedup').length;

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        success: created > 0,
        results,
        total: textsToStore.length,
        created,
        skipped: textsToStore.length - created,
        dedup_skipped: dedupSkipped,
        dedup_superseded: dedupSuperseded,
        mode: 'subgraph',
      }),
    }],
  };
}

/**
 * Handle recall in subgraph mode: search subgraph, decrypt, rerank.
 */
async function handleRecallSubgraph(
  state: SubgraphState,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as Record<string, unknown>;
  const query = (input?.query as string) || '';
  const startTime = Date.now();

  if (!query || typeof query !== 'string' || query.trim().length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          memories: [],
          latency_ms: 0,
          error: 'Invalid input: query is required',
        }),
      }],
    };
  }

  let k = (input?.k as number) ?? 8;
  if (k < 1) k = 8;
  if (k > 50) k = 50;

  try {
    // 1. Generate blind indices for the query (word + LSH trapdoors)
    const wordTrapdoors = generateBlindIndices(query.trim());

    // 2. Generate query embedding for LSH trapdoors and cosine reranking
    const queryEmbedding = await generateEmbedding(query.trim(), { isQuery: true });
    const lshTrapdoors = state.lshHasher.hash(queryEmbedding);

    // 3. Combine trapdoors
    const allTrapdoors = [...wordTrapdoors, ...lshTrapdoors];

    // 4. Search the subgraph
    const maxCandidates = Math.max(k * 50, 400); // Fetch more candidates for reranking
    const candidates = await searchSubgraph(
      state.smartAccountAddress,
      allTrapdoors,
      maxCandidates,
      state.serverUrl,
      Buffer.from(state.authKey).toString('hex'),
    );

    if (candidates.length === 0) {
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            memories: [],
            latency_ms: Date.now() - startTime,
            mode: 'subgraph',
          }),
        }],
      };
    }

    // 5. Decrypt candidates
    const decryptedCandidates = [];
    for (const c of candidates) {
      try {
        // Decrypt the fact text (encryptedBlob is 0x-prefixed hex from subgraph)
        const blobHex = c.encryptedBlob.startsWith('0x') ? c.encryptedBlob.slice(2) : c.encryptedBlob;
        const blobBase64 = Buffer.from(blobHex, 'hex').toString('base64');
        const text = decrypt(blobBase64, state.encryptionKey);

        // Decrypt embedding if available
        let embedding: number[] | undefined;
        if (c.encryptedEmbedding) {
          try {
            embedding = decryptEmbedding(c.encryptedEmbedding, state.encryptionKey);
          } catch {
            // Skip embedding if decryption fails
          }
        }

        decryptedCandidates.push({
          id: c.id,
          text,
          embedding,
          importance: parseFloat(c.decayScore) || 0.5,
          createdAt: parseInt(c.timestamp) || undefined,
        });
      } catch {
        // Skip candidates that fail to decrypt (e.g., wrong key, corrupt data)
      }
    }

    // 6. Detect query intent for dynamic weight selection
    const intent = detectQueryIntent(query.trim());
    const weights = INTENT_WEIGHTS[intent];

    // 7. Rerank with BM25 + cosine + importance + recency via weighted RRF
    const reranked = rerank(query.trim(), queryEmbedding, decryptedCandidates, k, weights);

    // 8. Format results
    const memories = reranked.map(r => ({
      fact_id: r.id,
      fact_text: r.text,
      score: r.rrfScore,
      cosine_similarity: r.cosineSimilarity ?? 0,
      importance: Math.round((r.importance ?? 0.5) * 10),
      age_days: r.createdAt
        ? Math.floor((Date.now() / 1000 - r.createdAt) / 86400)
        : 0,
    }));

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          memories,
          latency_ms: Date.now() - startTime,
          candidates_searched: candidates.length,
          mode: 'subgraph',
          query_intent: intent,
        }),
      }],
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error';
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          memories: [],
          latency_ms: Date.now() - startTime,
          error: `Failed to recall memories: ${message}`,
          mode: 'subgraph',
        }),
      }],
    };
  }
}

/**
 * Handle forget in subgraph mode: submit a tombstone fact with isActive=false.
 *
 * On-chain facts are immutable, so we submit a new record that marks the
 * original fact as inactive. The subgraph mapping updates isActive accordingly.
 */
async function handleForgetSubgraph(
  state: SubgraphState,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as Record<string, unknown>;
  const factId = input?.fact_id as string;

  if (!factId || typeof factId !== 'string') {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          deleted_count: 0,
          fact_ids: [],
          error: 'fact_id is required for forget (query-based forget is not supported with the managed service)',
        }),
      }],
    };
  }

  try {
    // Build a tombstone payload: same fact ID with isActive=false
    // The protobuf encoder always sets isActive=true, so we build a minimal
    // tombstone payload manually. For now, we re-encode the fact with
    // a special "tombstone" marker that the subgraph mapping recognizes.
    const tombstonePayload: FactPayload = {
      id: factId,
      timestamp: new Date().toISOString(),
      owner: state.smartAccountAddress,
      encryptedBlob: Buffer.from('tombstone').toString('hex'),
      blindIndices: [],
      decayScore: 0,
      source: 'mcp_forget',
      contentFp: '',
      agentId: 'mcp-server',
    };

    const protobuf = encodeFactProtobuf(tombstonePayload);
    const config = getSubgraphConfig({
      relayUrl: state.serverUrl,
      mnemonic: state.mnemonic,
      authKeyHex: Buffer.from(state.authKey).toString('hex'),
      walletAddress: state.smartAccountAddress,
    });

    const { txHash, success } = await submitFactOnChain(protobuf, config);

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          deleted_count: success ? 1 : 0,
          fact_ids: success ? [factId] : [],
          tx_hash: txHash,
          mode: 'subgraph',
        }),
      }],
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error';
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          deleted_count: 0,
          fact_ids: [],
          error: `Failed to forget memory: ${message}`,
          mode: 'subgraph',
        }),
      }],
    };
  }
}

// ── Auth error helper ────────────────────────────────────────────────────────

const AUTH_HINT_MESSAGE =
  'Authentication failed. If using a recovery phrase, check that all 12 words are in the correct order and spelled correctly.';

/**
 * Check if an error is a 401 authentication error and return a helpful message.
 */
function isAuthError(error: unknown): boolean {
  if (error instanceof Error) {
    const msg = error.message;
    return (
      msg.includes('401') ||
      msg.includes('UNAUTHORIZED') ||
      msg.includes('Not authenticated') ||
      msg.includes('AUTH_FAILED') ||
      msg.includes('Invalid credentials')
    );
  }
  return false;
}

// ── Quota error helper ──────────────────────────────────────────────────────

/**
 * Check if an error is a quota exceeded error (HTTP 403 with error_code: QUOTA_EXCEEDED).
 */
function isQuotaExceededError(error: unknown): boolean {
  if (error instanceof Error) {
    const msg = error.message.toLowerCase();
    return msg.includes('quota_exceeded') || msg.includes('quota exceeded');
  }
  return false;
}

function quotaExceededResponse(): { content: Array<{ type: string; text: string }>; isError: true } {
  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        error: 'quota_exceeded',
        message: 'Free tier limit reached. Upgrade to Pro for unlimited memories.',
        upgrade_url: 'Use totalreclaw_upgrade tool to get a checkout link',
      }),
    }],
    isError: true,
  };
}

// ── Layer 1: Server with instructions ────────────────────────────────────────

const server = new Server(
  { name: 'totalreclaw', version: '1.0.0' },
  {
    capabilities: {
      tools: {},
      prompts: {},
      resources: { subscribe: true, listChanged: true },
    },
    instructions: SERVER_INSTRUCTIONS,
  }
);

// ── Wire up cache invalidation ───────────────────────────────────────────────
// When facts are stored, invalidate the memory context resource cache

setOnRememberCallback(() => {
  invalidateMemoryContextCache();
  // Notify subscribed clients that the resource has been updated
  server.sendResourceUpdated({ uri: memoryContextResource.uri }).catch((err) => console.error('Failed to send resource update:', err));
});

// ── Layer 2 + 3: Tool handlers ───────────────────────────────────────────────

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    rememberToolDefinition,
    recallToolDefinition,
    forgetToolDefinition,
    exportToolDefinition,
    importToolDefinition,
    importFromToolDefinition,
    consolidateToolDefinition,
    statusToolDefinition,
    upgradeToolDefinition,
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const mode = subgraphState ? 'subgraph' : 'http';

  try {
    // ── Billing tools (mode-independent, always use HTTP relay) ────────────
    if (name === 'totalreclaw_status') {
      const authKeyHex = subgraphState
        ? Buffer.from(subgraphState.authKey).toString('hex')
        : '';
      return await handleStatus(SERVER_URL, authKeyHex, args);
    }

    if (name === 'totalreclaw_upgrade') {
      const authKeyHex = subgraphState
        ? Buffer.from(subgraphState.authKey).toString('hex')
        : '';
      return await handleUpgrade(SERVER_URL, authKeyHex, args);
    }

    // ── Subgraph mode ─────────────────────────────────────────────────────
    if (subgraphState) {
      switch (name) {
        case 'totalreclaw_remember': {
          try {
            const result = await handleRememberSubgraph(subgraphState, args);
            // Invalidate cache after successful store
            invalidateMemoryContextCache();
            server.sendResourceUpdated({ uri: memoryContextResource.uri }).catch((err) => console.error('Failed to send resource update:', err));
            return result;
          } catch (error) {
            if (isQuotaExceededError(error)) {
              return quotaExceededResponse();
            }
            throw error;
          }
        }

        case 'totalreclaw_recall':
          return await handleRecallSubgraph(subgraphState, args);

        case 'totalreclaw_forget': {
          const result = await handleForgetSubgraph(subgraphState, args);
          invalidateMemoryContextCache();
          server.sendResourceUpdated({ uri: memoryContextResource.uri }).catch((err) => console.error('Failed to send resource update:', err));
          return result;
        }

        case 'totalreclaw_export':
          // Export not yet implemented for the managed service -- fall through to error
          return {
            content: [{
              type: 'text',
              text: JSON.stringify({
                error: 'Export is not yet supported with the managed service. Use self-hosted mode for export.',
              }),
            }],
            isError: true,
          };

        case 'totalreclaw_import':
          return {
            content: [{
              type: 'text',
              text: JSON.stringify({
                error: 'Import is not yet supported with the managed service. Use self-hosted mode for import.',
              }),
            }],
            isError: true,
          };

        case 'totalreclaw_import_from':
          return {
            content: [{
              type: 'text',
              text: JSON.stringify({
                error: 'Import from external sources is not yet supported with the managed service. Use self-hosted mode for import.',
              }),
            }],
            isError: true,
          };

        case 'totalreclaw_consolidate':
          return {
            content: [{
              type: 'text',
              text: JSON.stringify({
                error: 'Consolidation is not supported with the managed service. On-chain facts require tombstone-based dedup which is handled automatically at store time.',
              }),
            }],
            isError: true,
          };

        default:
          return {
            content: [{
              type: 'text',
              text: JSON.stringify({ error: `Unknown tool: ${name}` }),
            }],
            isError: true,
          };
      }
    }

    // ── HTTP mode (existing behavior) ─────────────────────────────────────
    const client = await getClient();

    switch (name) {
      case 'totalreclaw_remember': {
        try {
          const result = await handleRemember(client, args);
          return result;
        } catch (error) {
          if (isQuotaExceededError(error)) {
            return quotaExceededResponse();
          }
          throw error;
        }
      }

      case 'totalreclaw_recall':
        return await handleRecall(client, args);

      case 'totalreclaw_forget': {
        const result = await handleForget(client, args);
        // Invalidate cache on forget too
        invalidateMemoryContextCache();
        server.sendResourceUpdated({ uri: memoryContextResource.uri }).catch((err) => console.error('Failed to send resource update:', err));
        return result;
      }

      case 'totalreclaw_export':
        return await handleExport(client, args);

      case 'totalreclaw_import':
        return await handleImport(client, args);

      case 'totalreclaw_import_from':
        return await handleImportFrom(client, args);

      case 'totalreclaw_consolidate': {
        const result = await handleConsolidate(client, args);
        // Invalidate cache after consolidation (facts may have been deleted)
        invalidateMemoryContextCache();
        server.sendResourceUpdated({ uri: memoryContextResource.uri }).catch((err) => console.error('Failed to send resource update:', err));
        return result;
      }

      default:
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({ error: `Unknown tool: ${name}` }),
          }],
          isError: true,
        };
    }
  } catch (error) {
    // Final catch-all: check for quota error at the top level too
    if (isQuotaExceededError(error)) {
      return quotaExceededResponse();
    }

    // Provide a helpful hint for authentication failures
    if (isAuthError(error)) {
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            error: AUTH_HINT_MESSAGE,
          }),
        }],
        isError: true,
      };
    }

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          error: error instanceof Error ? error.message : 'Unknown error',
        }),
      }],
      isError: true,
    };
  }
});

// ── Layer 4: Resources ───────────────────────────────────────────────────────

server.setRequestHandler(ListResourcesRequestSchema, async () => ({
  resources: [memoryContextResource],
}));

server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
  const { uri } = request.params;

  if (uri === memoryContextResource.uri) {
    // With the managed service, resource reading is not supported yet
    if (subgraphState) {
      return {
        contents: [
          {
            uri: memoryContextResource.uri,
            mimeType: 'text/markdown',
            text: '*Memory context resource is not available with the managed service. Use totalreclaw_recall to search memories.*',
          },
        ],
      };
    }

    const client = await getClient();
    const content = await readMemoryContext(client);

    return {
      contents: [
        {
          uri: memoryContextResource.uri,
          mimeType: 'text/markdown',
          text: content,
        },
      ],
    };
  }

  throw new Error(`Unknown resource: ${uri}`);
});

// ── Layer 5: Prompts ─────────────────────────────────────────────────────────

server.setRequestHandler(ListPromptsRequestSchema, async () => ({
  prompts: [
    // Legacy instructions prompt (backward compat)
    {
      name: 'totalreclaw_instructions',
      description: 'Instructions for using TotalReclaw tools',
    },
    // New auto-memory prompt fallbacks
    ...PROMPT_DEFINITIONS,
  ],
}));

server.setRequestHandler(GetPromptRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const messages = getPromptMessages(name, args as Record<string, string> | undefined);
  return { messages };
});

// ── Main ─────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  // CLI subcommand routing
  if (process.argv[2] === 'setup') {
    const { runSetup } = await import('./cli/setup.js');
    await runSetup();
    return;
  }

  // Detect and initialize server mode
  const mode = detectServerMode();
  if (mode === 'subgraph') {
    subgraphState = await initSubgraphState();
    console.error(`TotalReclaw MCP server started (managed service, owner: ${subgraphState.smartAccountAddress})`);
  } else {
    console.error('TotalReclaw MCP server started (self-hosted mode)');
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((error) => {
  console.error('Server error:', error);
  process.exit(1);
});
