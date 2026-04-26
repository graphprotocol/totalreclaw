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
  importBatchToolDefinition,
  consolidateToolDefinition,
  handleRemember,
  handleRecall,
  handleForget,
  handleExport,
  handleImport,
  handleImportFrom,
  handleImportBatch,
  handleConsolidate,
  statusToolDefinition,
  upgradeToolDefinition,
  migrateToolDefinition,
  handleStatus,
  handleUpgrade,
  fetchAllFactsFromSubgraph,
  fetchMainnetContentFps,
  fetchBlindIndicesForFacts,
  checkBillingTier,
  type SubgraphFactFull,
  type MigrationResult,
  debriefToolDefinition,
  handleDebrief,
  parseDebriefResponse,
  supportToolDefinition,
  handleSupport,
  accountToolDefinition,
  handleAccount,
} from './tools/index.js';
import { getLastBillingResponse } from './tools/status.js';
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
  submitFactBatchOnChain,
  encodeFactProtobuf,
  getSubgraphConfig,
  isSubgraphMode,
  type FactPayload,
  type SubgraphStoreConfig,
} from './subgraph/store.js';
import { searchSubgraph, searchSubgraphBroadened, getOwnerFactCount, fetchFactById } from './subgraph/search.js';
import { confirmIndexed } from './subgraph/confirm-indexed.js';
import {
  detectAndResolveContradictions,
  type ResolutionDecision,
} from './contradiction-sync.js';
import {
  pinToolDefinition,
  unpinToolDefinition,
  handlePin,
  handleUnpin,
  handlePinSubgraphWithDeps,
  handleUnpinSubgraphWithDeps,
  type PinOpDeps,
} from './tools/pin.js';
import {
  retypeToolDefinition,
  handleRetype,
  handleRetypeWithDeps,
  type MetadataOpDeps,
} from './tools/retype.js';
import {
  setScopeToolDefinition,
  handleSetScope,
  handleSetScopeWithDeps,
} from './tools/set-scope.js';
import {
  buildV1ClaimBlob,
  computeEntityTrapdoor,
  isDigestBlob,
  readBlobUnified,
  readClaimFromBlob,
} from './claims-helper.js';
import { isValidMemoryType, type MemoryType } from './memory-types.js';
import {
  normalizeTypeToV1,
  normalizeScope,
} from './tools/remember.js';
import { type MemoryTypeV1, type MemoryScope } from './v1-types.js';

import { validateMnemonic, generateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import { mnemonicToAccount } from 'viem/accounts';
import { createPublicClient, http, type Address } from 'viem';
import { gnosis, baseSepolia } from 'viem/chains';
import { toSimpleSmartAccount } from 'permissionless/accounts';
import { entryPoint07Address } from 'viem/account-abstraction';
import crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

import {
  registerWithServer,
  CREDENTIALS_PATH,
  deriveAuthKey,
  computeAuthKeyHash as computeSetupAuthKeyHash,
} from './cli/setup.js';
import type { SavedCredentials } from './cli/setup.js';

// ── Configuration ───────────────────────────────────────────────────────────

const SERVER_URL = process.env.TOTALRECLAW_SERVER_URL || 'https://api.totalreclaw.xyz';
const MASTER_PASSWORD = process.env.TOTALRECLAW_RECOVERY_PHRASE;

// v1 env var cleanup — warn once if any removed env var is still set.
// See docs/guides/env-vars-reference.md for the canonical list.
//
// NOTE: TOTALRECLAW_SESSION_ID was previously in this list and silently
// rejected with a warning, breaking Axiom traceability for QA runs that rely
// on the X-TotalReclaw-Session header for log filtering. Restored as
// SUPPORTED — see `./session-id.ts` + internal#127. Do NOT add it back.
(() => {
  const removed = [
    'TOTALRECLAW_CHAIN_ID',
    'TOTALRECLAW_EMBEDDING_MODEL',
    'TOTALRECLAW_STORE_DEDUP',
    'TOTALRECLAW_LLM_MODEL',
    'TOTALRECLAW_TAXONOMY_VERSION',
    'TOTALRECLAW_CLAIM_FORMAT',
    'TOTALRECLAW_DIGEST_MODE',
  ].filter((name) => process.env[name] !== undefined);
  if (removed.length > 0) {
    // rc.22 finding #4 — full URL beats relative repo path on stderr.
    console.error(
      `TotalReclaw MCP: ignoring removed env var(s): ${removed.join(', ')}. ` +
        `Migration guide: https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/env-vars-reference.md`,
    );
  }
})();

// QA / observability session-tag reader. See `./session-id.ts`.
// Re-exported so the existing wide imports of `mcp/src/index.ts` keep working.
import { getSessionId } from './session-id.js';
export { getSessionId };

// ── Client identification ──────────────────────────────────────────────────
import { setClientId, getClientId } from './client-id.js';
let clientIdentifierResolved = false;

/**
 * Build outbound relay headers including the optional session tag.
 * Uses the late-resolved client identifier — falls back to the raw
 * `getClientId()` value if `getClientIdentifier()` hasn't run yet (which
 * can happen for fetches kicked off before the MCP `initialize` handshake
 * completes, e.g. proactive billing fetch).
 */
function buildRelayHeaders(
  overrides: Record<string, string> = {},
): Record<string, string> {
  const clientId = clientIdentifierResolved ? getClientIdentifier() : getClientId();
  const headers: Record<string, string> = {
    'X-TotalReclaw-Client': clientId,
  };
  const sid = getSessionId();
  if (sid) headers['X-TotalReclaw-Session'] = sid;
  return { ...headers, ...overrides };
}

function resolveMnemonic(): string | undefined {
  // Priority 1: env var
  if (MASTER_PASSWORD) return MASTER_PASSWORD.trim();

  // Priority 2: credentials.json mnemonic field
  try {
    const data = fs.readFileSync(CREDENTIALS_PATH, 'utf-8');
    const parsed = JSON.parse(data) as { mnemonic?: string };
    if (parsed.mnemonic && typeof parsed.mnemonic === 'string') {
      const trimmed = parsed.mnemonic.trim();
      const words = trimmed.split(/\s+/);
      const allWordsValid = words.length === 12 && words.every((w: string) => wordlist.includes(w));
      if (validateMnemonic(trimmed, wordlist) || allWordsValid) {
        return trimmed;
      }
    }
  } catch {
    // credentials.json doesn't exist — fall through to unconfigured
  }

  return undefined;
}

let currentMode: ServerMode = 'unconfigured';

// Store-time near-duplicate detection is always ON in v1.
// The TOTALRECLAW_STORE_DEDUP env var was removed.
const STORE_DEDUP_ENABLED = true;

// ── Billing cache (in-memory, for server-side candidate pool) ───────────────

interface BillingCacheEntry {
  max_candidate_pool?: number;
  checked_at: number;
}

const BILLING_CACHE_TTL = 2 * 60 * 60 * 1000; // 2 hours
let billingCache: BillingCacheEntry | null = null;

/** Whether a background billing fetch is already in progress. */
let billingFetchInProgress = false;

/**
 * Proactively fetch billing status to populate the candidate pool cache.
 * Fire-and-forget — does not block the caller.
 */
function proactiveBillingFetch(state: SubgraphState): void {
  if (billingFetchInProgress) return;
  billingFetchInProgress = true;
  const authKeyHex = Buffer.from(state.authKey).toString('hex');
  const url = `${state.serverUrl.replace(/\/+$/, '')}/v1/billing/status?wallet_address=${encodeURIComponent(state.smartAccountAddress)}`;
  fetch(url, {
    method: 'GET',
    headers: buildRelayHeaders({
      'Authorization': `Bearer ${authKeyHex}`,
    }),
  })
    .then(async (resp) => {
      if (resp.ok) {
        const raw = (await resp.json()) as Record<string, unknown>;
        const features = raw.features as Record<string, unknown> | undefined;
        billingCache = {
          max_candidate_pool: features?.max_candidate_pool as number | undefined,
          checked_at: Date.now(),
        };
      }
    })
    .catch(() => { /* best-effort */ })
    .finally(() => { billingFetchInProgress = false; });
}

/**
 * Get the server-configured max candidate pool size.
 * Falls back to a local formula if no cached billing response.
 */
function getMaxCandidatePool(k: number): number {
  if (billingCache && Date.now() - billingCache.checked_at < BILLING_CACHE_TTL) {
    if (billingCache.max_candidate_pool != null) return billingCache.max_candidate_pool;
  }
  // Trigger a background fetch if we have subgraph state but no cache
  if (!billingCache && subgraphState) {
    proactiveBillingFetch(subgraphState);
  }
  // Fallback to local formula
  return Math.max(k * 50, 400);
}

// ── Server mode detection ───────────────────────────────────────────────────

type ServerMode = 'http' | 'subgraph' | 'unconfigured';

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
 * is provided as TOTALRECLAW_RECOVERY_PHRASE.
 *
 * Self-hosted mode (HTTP) requires TOTALRECLAW_SELF_HOSTED=true.
 * Defaults to managed service otherwise.
 */
function detectServerMode(mnemonic: string | undefined): ServerMode {
  if (!mnemonic) return 'unconfigured';
  if (process.env.TOTALRECLAW_SELF_HOSTED === 'true') return 'http';
  const words = mnemonic.split(/\s+/);
  if (words.length !== 12 && words.length !== 24) return 'unconfigured';
  const allWordsValid = words.every((w: string) => wordlist.includes(w));
  if (!validateMnemonic(mnemonic, wordlist) && !allWordsValid) return 'unconfigured';
  return 'subgraph';
}

/**
 * Initialize subgraph state from the BIP-39 mnemonic.
 * Derives all cryptographic keys and creates the LSH hasher.
 */
async function initSubgraphState(mnemonic: string): Promise<SubgraphState> {
  const { authKey, encryptionKey, dedupKey, salt } = deriveKeys(mnemonic);

  // Derive LSH seed and create hasher
  const lshSeed = deriveLshSeed(mnemonic, salt);
  const dims = getEmbeddingDims(); // 640 for Harrier-OSS-v1-270M
  const lshHasher = new LSHHasher(lshSeed, dims);

  // Derive Smart Account address via CREATE2 (deterministic, same on all chains).
  // First derive on Base Sepolia (free tier default), then query billing to detect
  // Pro tier and switch to Gnosis if needed.
  const entryPointAddr = (process.env.TOTALRECLAW_ENTRYPOINT_ADDRESS || entryPoint07Address) as Address;
  const authKeyHex = Buffer.from(authKey).toString('hex');
  const ownerAccount = mnemonicToAccount(mnemonic);

  // Smart Account address is deterministic via CREATE2 — use Base Sepolia for derivation
  // (same address regardless of chain).
  const publicClient = createPublicClient({
    chain: baseSepolia,
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

  // Determine chain ID: always auto-detect from billing tier.
  // Free tier → Base Sepolia (84532, testnet — Pimlico sponsors gas for free).
  // Pro tier  → Gnosis mainnet (100, permanent on-chain storage).
  // TOTALRECLAW_CHAIN_ID user-facing override was removed in v1.
  let chainId = 84532; // default free tier

  // Auto-detect Pro tier from billing endpoint.
  {
    try {
      const billingUrl = `${SERVER_URL.replace(/\/+$/, '')}/v1/billing/status?wallet_address=${encodeURIComponent(smartAccountAddress)}`;
      const resp = await fetch(billingUrl, {
        method: 'GET',
        headers: buildRelayHeaders({
          'Authorization': `Bearer ${authKeyHex}`,
        }),
        signal: AbortSignal.timeout(5000),
      });
      if (resp.ok) {
        const billing = (await resp.json()) as Record<string, unknown>;
        if (billing.tier === 'pro') {
          chainId = 100;
          console.error('TotalReclaw: Pro tier detected — using Gnosis mainnet (chain 100).');
        }
      }
    } catch {
      // Best-effort — default to free tier chain if billing is unreachable.
    }
  }

  // Register auth key with relay (idempotent — relay returns 200 for existing users).
  // Without this, all relay queries return 401 when using env var mnemonic path
  // (the setup wizard handles registration, but the env var path previously skipped it).
  try {
    const authKeyHashForReg = computeAuthKeyHash(authKey);
    const saltHexForReg = Buffer.from(salt).toString('hex');
    await registerWithServer(SERVER_URL, authKeyHashForReg, saltHexForReg);
  } catch {
    // Best-effort — if relay is unreachable, billing check above may have also failed.
    // The server returns 200 for already-registered users, so this only fails on network errors.
    console.error('TotalReclaw: Registration with relay failed (best-effort, will retry on next start).');
  }

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
 * Concatenates the float array into a Buffer, encrypts with XChaCha20-Poly1305,
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
  const factsArray = input?.facts as Array<{
    text: string;
    importance?: number;
    type?: string;
    scope?: string;
    reasoning?: string;
  }> | undefined;

  // Phase 2.2.6 + v1: accept both legacy 8-type and v1 6-type inputs. The
  // managed-service path always writes v1 inner blobs (protobuf wrapper v4).
  // The legacy `MemoryType` is still tracked for backward compat with any
  // downstream reader that consults the v0 short-key value.
  const textsToStore: Array<{
    text: string;
    importance: number;
    type: MemoryType;         // v0 short-key (for legacy fallback)
    typeV1: MemoryTypeV1;     // v1 canonical type
    scope: MemoryScope;
    reasoning?: string;
  }> = [];

  const resolveType = (raw: unknown): MemoryType =>
    isValidMemoryType(raw) ? raw : 'fact';

  if (factsArray && Array.isArray(factsArray) && factsArray.length > 0) {
    for (const f of factsArray) {
      if (f.text && typeof f.text === 'string' && f.text.trim().length > 0) {
        textsToStore.push({
          text: f.text.trim(),
          importance: f.importance ?? 5,
          type: resolveType(f.type),
          typeV1: normalizeTypeToV1(f.type),
          scope: normalizeScope(f.scope),
          reasoning: typeof f.reasoning === 'string' && f.reasoning.trim().length > 0
            ? f.reasoning.trim()
            : undefined,
        });
      }
    }
  } else if (factText && typeof factText === 'string' && factText.trim().length > 0) {
    const singleMetadata = input?.metadata as {
      type?: unknown;
      scope?: unknown;
      reasoning?: unknown;
    } | undefined;
    const topLevelScope = typeof (input as Record<string, unknown>)?.scope === 'string'
      ? (input as Record<string, unknown>).scope
      : undefined;
    const topLevelReasoning = typeof (input as Record<string, unknown>)?.reasoning === 'string'
      ? (input as Record<string, unknown>).reasoning as string
      : undefined;
    const resolvedScope = normalizeScope(topLevelScope ?? singleMetadata?.scope);
    const resolvedReasoning =
      topLevelReasoning ?? (typeof singleMetadata?.reasoning === 'string' ? singleMetadata.reasoning : undefined);
    textsToStore.push({
      text: factText.trim(),
      importance: (input?.importance as number) ?? 5,
      type: resolveType(singleMetadata?.type),
      typeV1: normalizeTypeToV1(singleMetadata?.type),
      scope: resolvedScope,
      reasoning: resolvedReasoning && resolvedReasoning.trim().length > 0
        ? resolvedReasoning.trim()
        : undefined,
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

  // Collect protobuf payloads for batch submission (tombstones + facts)
  const pendingPayloads: Buffer[] = [];
  const pendingFactMeta: Array<{ factId: string; action: string }> = [];

  for (const item of textsToStore) {
    let itemSuperseded = false; // Per-fact flag for action tracking
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
                const blobHex = c.encryptedBlob.startsWith('0x') ? c.encryptedBlob.slice(2) : c.encryptedBlob;
                const blobBase64 = Buffer.from(blobHex, 'hex').toString('base64');
                const decryptedBlob = decrypt(blobBase64, state.encryptionKey);
                if (isDigestBlob(decryptedBlob)) continue;
                const doc = readClaimFromBlob(decryptedBlob);
                const text = doc.text;

                let candEmbedding: number[] | null = null;
                if (c.encryptedEmbedding) {
                  try {
                    const embHex = c.encryptedEmbedding.startsWith('0x') ? c.encryptedEmbedding.slice(2) : c.encryptedEmbedding;
                    candEmbedding = decryptEmbedding(embHex, state.encryptionKey);
                  } catch { /* skip */ }
                }

                if (candEmbedding && candEmbedding.length !== getEmbeddingDims()) {
                  try {
                    candEmbedding = await generateEmbedding(text);
                  } catch { candEmbedding = null; }
                }

                decryptedCandidates.push({
                  id: c.id,
                  text,
                  embedding: candEmbedding,
                  importance: doc.importance,
                  decayScore: parseFloat(c.decayScore) || 0.5,
                  createdAt: parseInt(c.timestamp) || 0,
                  version: 1,
                });
              } catch { /* skip undecryptable */ }
            }

            const dupMatch = findNearDuplicate(embedding, decryptedCandidates, getStoreDedupThreshold());
            if (dupMatch) {
              if (isExplicitRemember) {
                // Explicit remember: always supersede — queue tombstone for old fact
                effectiveImportance = Math.max(item.importance, dupMatch.existingFact.importance);
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
                pendingPayloads.push(encodeFactProtobuf(tombstonePayload));
                console.error(`Store-time dedup: queued supersede for ${dupMatch.existingFact.id} (sim=${dupMatch.similarity.toFixed(3)})`);
                dedupSuperseded++;
                itemSuperseded = true;
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
                pendingPayloads.push(encodeFactProtobuf(tombstonePayload));
                console.error(`Store-time dedup: queued supersede for ${dupMatch.existingFact.id} (sim=${dupMatch.similarity.toFixed(3)})`);
                dedupSuperseded++;
                itemSuperseded = true;
              }
            }
          }
        } catch (dedupErr) {
          // Fail-open: dedup failure should not prevent storing the fact
          console.error(`Store-time dedup search failed: ${dedupErr instanceof Error ? dedupErr.message : String(dedupErr)}`);
        }
      }

      // 4. Build the blob.
      //
      // Memory Taxonomy v1 (MCP server v3.0.0+): the managed-service write
      // path unconditionally emits v1 inner blobs. Outer protobuf wrapper is
      // v4 so readers know the inner payload is v1 JSON (see
      // protobuf-v4-design.md). The v0 `{t,c,i,...}` canonical and raw-text
      // legacy blobs are no longer produced — `TOTALRECLAW_CLAIM_FORMAT`
      // was removed in v1.
      //
      // `source` defaults to:
      //   - `'user'` when the tool was called in single-fact (explicit) mode
      //   - `'user-inferred'` when called via the batch path (extraction)
      // Host agents typically have the best provenance signal; this is a
      // conservative default the extraction pipeline can override later.
      const source = isExplicitRemember ? 'user' : 'user-inferred';
      const blobPlaintext = buildV1ClaimBlob({
        text: item.text,
        type: item.typeV1,
        source,
        scope: item.scope,
        reasoning: item.reasoning,
        importance: effectiveImportance,
      });

      // Build the fact id up-front so contradiction detection can reference it.
      const factId = crypto.randomUUID();

      // Phase 2 Slice 2d: contradiction detection + auto-resolution.
      //
      // Mirrors the plugin's pattern at `skill/plugin/index.ts:1661-1754`. Runs
      // only when the new claim has entities (the core contradiction primitive
      // is entity-anchored), and only emits decisions when Rust core's
      // `resolve_with_candidates` identifies a contradiction in the
      // [threshold_lower, threshold_upper) similarity band.
      //
      // Pin respect is handled inside the Rust core via
      // `respect_pin_in_resolution` — when an existing claim's status is
      // pinned, the action emitted is `skip_new` with reason `existing_pinned`
      // and the new write is dropped.
      //
      // Returns one decision per candidate contradicting claim:
      //   - supersede_existing → queue a tombstone + proceed with the new write
      //   - skip_new → do not write the new fact; record the skip reason
      //   - empty list → no contradiction, proceed unchanged
      //
      // Never throws: on any error (subgraph, decrypt, WASM), returns [] and
      // falls back to pre-Phase-2 behaviour.
      let contradictionSkipNew = false;
      try {
        const newClaimObj = JSON.parse(blobPlaintext) as Record<string, unknown>;
        const authKeyHex = Buffer.from(state.authKey).toString('hex');
        const decisions: ResolutionDecision[] = await detectAndResolveContradictions({
          newClaim: newClaimObj,
          newClaimId: factId,
          newEmbedding: embedding,
          subgraphOwner: state.smartAccountAddress,
          authKeyHex,
          encryptionKey: state.encryptionKey,
          deps: {
            searchSubgraph: (owner, trapdoors, maxCandidates, authKey) =>
              searchSubgraph(owner, trapdoors, maxCandidates, state.serverUrl, authKey).then((rows) =>
                rows.map((r) => ({
                  id: r.id,
                  encryptedBlob: r.encryptedBlob,
                  encryptedEmbedding: r.encryptedEmbedding ?? null,
                  timestamp: r.timestamp,
                  isActive: r.isActive,
                })),
              ),
            decryptFromHex: (hex, key) => {
              const clean = hex.startsWith('0x') ? hex.slice(2) : hex;
              const base64 = Buffer.from(clean, 'hex').toString('base64');
              return decrypt(base64, key);
            },
          },
          logger: {
            info: (m) => console.error(m),
            warn: (m) => console.error(m),
          },
        });

        for (const decision of decisions) {
          if (decision.action === 'supersede_existing') {
            const tombstonePayload: FactPayload = {
              id: decision.existingFactId,
              timestamp: new Date().toISOString(),
              owner: state.smartAccountAddress,
              encryptedBlob: Buffer.from('tombstone').toString('hex'),
              blindIndices: [],
              decayScore: 0,
              source: 'mcp_contradiction',
              contentFp: '',
              agentId: 'mcp-server',
            };
            pendingPayloads.push(encodeFactProtobuf(tombstonePayload));
            console.error(
              `Auto-resolve: queued supersede for ${decision.existingFactId.slice(0, 10)}… ` +
                `(sim=${decision.similarity.toFixed(3)}, entity=${decision.entityId})`,
            );
          } else if (decision.action === 'skip_new') {
            if (decision.reason === 'existing_pinned') {
              console.error(
                `Auto-resolve: skipped new write — existing claim ${decision.existingFactId.slice(0, 10)}… is pinned ` +
                  `(sim=${decision.similarity.toFixed(3)}, entity=${decision.entityId})`,
              );
            } else {
              console.error(
                `Auto-resolve: skipped new write — existing ${decision.existingFactId.slice(0, 10)}… wins ` +
                  `(sim=${decision.similarity.toFixed(3)}, entity=${decision.entityId})`,
              );
            }
            contradictionSkipNew = true;
          }
        }
      } catch (crErr) {
        // detectAndResolveContradictions is supposed to never throw — if it
        // does, we log and continue with pre-Phase-2 behaviour.
        const msg = crErr instanceof Error ? crErr.message : String(crErr);
        console.error(`Contradiction detection failed (proceeding with store): ${msg}`);
      }

      if (contradictionSkipNew) {
        results.push({ success: true, fact_id: factId, action: 'skipped_contradiction' });
        continue;
      }

      const encryptedBlob = encrypt(blobPlaintext, state.encryptionKey);

      // 5. Generate content fingerprint for dedup (over plaintext text, not blob)
      const contentFp = generateContentFingerprint(item.text, state.dedupKey);

      // 6. Encrypt the embedding for on-chain storage
      const encryptedEmb = encryptEmbedding(embedding, state.encryptionKey);

      // 7. Build fact payload
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

      // 8. Encode as protobuf and queue for batch submission
      const protobuf = encodeFactProtobuf(factPayload);
      pendingPayloads.push(protobuf);
      pendingFactMeta.push({ factId, action: itemSuperseded ? 'superseded' : 'created' });
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error';
      results.push({ success: false, fact_id: '', tx_hash: undefined });
      console.error(`Failed to prepare fact for on-chain storage: ${message}`);
    }
  }

  // Batch-submit all payloads (tombstones + facts) in a single UserOp
  if (pendingPayloads.length > 0) {
    try {
      const batchConfig = getSubgraphConfig({
        relayUrl: state.serverUrl,
        mnemonic: state.mnemonic,
        authKeyHex: Buffer.from(state.authKey).toString('hex'),
        walletAddress: state.smartAccountAddress,
      });
      const batchResult = await submitFactBatchOnChain(pendingPayloads, batchConfig);
      for (const meta of pendingFactMeta) {
        results.push({
          success: batchResult.success,
          fact_id: meta.factId,
          tx_hash: batchResult.txHash,
          action: meta.action,
        });
      }
      console.error(`Batch submitted ${batchResult.batchSize} payloads in 1 UserOp (tx=${batchResult.txHash.slice(0, 10)}...)`);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error';
      for (const meta of pendingFactMeta) {
        results.push({ success: false, fact_id: meta.factId, tx_hash: undefined });
      }
      console.error(`Batch submission failed: ${message}`);
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
 * Handle debrief in subgraph mode: validate, encrypt, submit on-chain.
 *
 * Follows the same pipeline as handleRememberSubgraph but simpler:
 * no dedup (debrief items are high-level summaries), source 'mcp_debrief'.
 */
async function handleDebriefSubgraph(
  state: SubgraphState,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as Record<string, unknown>;
  const factsInput = input?.facts;

  if (!Array.isArray(factsInput) || factsInput.length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          error: 'Invalid input: "facts" array is required and must not be empty',
        }),
      }],
    };
  }

  // Validate through the canonical parser
  const validated = parseDebriefResponse(JSON.stringify(factsInput));

  if (validated.length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: true,
          stored: 0,
          message: 'No valid debrief items to store (filtered by validation)',
        }),
      }],
    };
  }

  const pendingPayloads: Buffer[] = [];
  const pendingFactMeta: Array<{ factId: string }> = [];

  for (const item of validated) {
    try {
      const wordIndices = generateBlindIndices(item.text);
      const embedding = await generateEmbedding(item.text);
      const lshIndices = state.lshHasher.hash(embedding);
      const allIndices = [...wordIndices, ...lshIndices];

      // Memory Taxonomy v1: emit v1 inner blob with `type: 'summary'` (both
      // tool-level 'summary' and 'context' map here — per spec §type-semantics,
      // summary absorbs session-level synthesis regardless of whether the tool
      // called it "summary" or "context"). `source: 'derived'` marks this as a
      // derived-from-conversation claim (spec requires summary ∈ {derived,
      // assistant}; derived is the better fit here).
      //
      // TOTALRECLAW_CLAIM_FORMAT=legacy was removed in v1; raw-text blobs are
      // no longer produced on the write path.
      const blobPlaintext = buildV1ClaimBlob({
        text: item.text,
        type: 'summary',
        source: 'derived',
        importance: item.importance,
      });
      const encryptedBlob = encrypt(blobPlaintext, state.encryptionKey);
      const contentFp = generateContentFingerprint(item.text, state.dedupKey);
      const encryptedEmb = encryptEmbedding(embedding, state.encryptionKey);

      const factId = crypto.randomUUID();
      const factPayload: FactPayload = {
        id: factId,
        timestamp: new Date().toISOString(),
        owner: state.smartAccountAddress,
        encryptedBlob: Buffer.from(encryptedBlob, 'base64').toString('hex'),
        blindIndices: allIndices,
        decayScore: item.importance / 10,
        source: 'mcp_debrief',
        contentFp,
        agentId: 'mcp-server',
        encryptedEmbedding: encryptedEmb,
      };

      pendingPayloads.push(encodeFactProtobuf(factPayload));
      pendingFactMeta.push({ factId });
    } catch (error) {
      console.error(`Failed to prepare debrief item: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  const results: Array<{ success: boolean; fact_id: string; tx_hash?: string }> = [];

  if (pendingPayloads.length > 0) {
    try {
      const batchConfig = getSubgraphConfig({
        relayUrl: state.serverUrl,
        mnemonic: state.mnemonic,
        authKeyHex: Buffer.from(state.authKey).toString('hex'),
        walletAddress: state.smartAccountAddress,
      });
      const batchResult = await submitFactBatchOnChain(pendingPayloads, batchConfig);
      for (const meta of pendingFactMeta) {
        results.push({
          success: batchResult.success,
          fact_id: meta.factId,
          tx_hash: batchResult.txHash,
        });
      }
      console.error(`Debrief: submitted ${batchResult.batchSize} items (tx=${batchResult.txHash.slice(0, 10)}...)`);
    } catch (error) {
      for (const meta of pendingFactMeta) {
        results.push({ success: false, fact_id: meta.factId });
      }
      console.error(`Debrief batch submission failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  const stored = results.filter((r) => r.success).length;

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        success: stored > 0,
        stored,
        total: validated.length,
        results,
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
    const maxCandidates = getMaxCandidatePool(k);
    let candidates = await searchSubgraph(
      state.smartAccountAddress,
      allTrapdoors,
      maxCandidates,
      state.serverUrl,
      Buffer.from(state.authKey).toString('hex'),
    );

    // Always run broadened search and merge — ensures vocabulary mismatches
    // (e.g., "preferences" vs "prefer") don't cause recall failures.
    // The reranker handles scoring; extra cost is ~1 GraphQL query per recall.
    try {
      const fallbackCandidates = await searchSubgraphBroadened(
        state.smartAccountAddress,
        maxCandidates,
        state.serverUrl,
        Buffer.from(state.authKey).toString('hex'),
      );
      // Merge broadened results with existing candidates (deduplicate by ID)
      const existingIds = new Set(candidates.map(c => c.id));
      for (const fc of fallbackCandidates) {
        if (!existingIds.has(fc.id)) {
          candidates.push(fc);
        }
      }
    } catch {
      // Non-fatal — trapdoor results still work
    }

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

    // 5. Decrypt candidates. Surfaces v1 provenance (`source`) when the blob
    //    carries it so Retrieval v2 Tier 1 can apply the source-weighted
    //    multiplier in core's `rerankWithConfig`.
    const decryptedCandidates = [];
    const categoryMap = new Map<string, string>();
    const sourceMap = new Map<string, string>(); // v1 source, when available
    const scopeMap = new Map<string, string>();
    const importanceRaw = new Map<string, number>();
    for (const c of candidates) {
      try {
        const blobHex = c.encryptedBlob.startsWith('0x') ? c.encryptedBlob.slice(2) : c.encryptedBlob;
        const blobBase64 = Buffer.from(blobHex, 'hex').toString('base64');
        const decryptedBlob = decrypt(blobBase64, state.encryptionKey);
        if (isDigestBlob(decryptedBlob)) continue;
        const doc = readBlobUnified(decryptedBlob);
        const text = doc.text;

        let embedding: number[] | undefined;
        if (c.encryptedEmbedding) {
          try {
            embedding = decryptEmbedding(c.encryptedEmbedding, state.encryptionKey);
          } catch {
            // Skip embedding if decryption fails
          }
        }

        if (embedding && embedding.length !== getEmbeddingDims()) {
          try {
            embedding = await generateEmbedding(text);
          } catch {
            embedding = undefined;
          }
        }

        if (doc.category) {
          categoryMap.set(c.id, doc.category);
        }
        if (doc.v1) {
          sourceMap.set(c.id, doc.v1.source);
          if (doc.v1.scope) scopeMap.set(c.id, doc.v1.scope);
        }
        importanceRaw.set(c.id, doc.importance);

        decryptedCandidates.push({
          id: c.id,
          text,
          embedding,
          importance: parseFloat(c.decayScore) || 0.5,
          createdAt: parseInt(c.timestamp) || undefined,
          source: sourceMap.get(c.id),
        });
      } catch {
        // Skip candidates that fail to decrypt (e.g., wrong key, corrupt data)
      }
    }

    // 6. Detect query intent for dynamic weight selection
    const intent = detectQueryIntent(query.trim());
    const weights = INTENT_WEIGHTS[intent];

    // 7. Rerank via core's `rerankWithConfig` (BM25 + cosine + intent-weighted
    //    RRF + Tier 1 source weighting in one shot). The legacy `weights`
    //    argument is accepted for API stability but ignored -- core handles
    //    intent-weighting internally based on per-candidate cosine scores.
    const reranked = rerank(
      query.trim(),
      queryEmbedding,
      decryptedCandidates,
      k,
      weights,
      /* applySourceWeights (Retrieval v2 Tier 1) */ true,
    );

    // 8. Format results
    const memories = reranked.map((r) => ({
      fact_id: r.id,
      fact_text: r.text,
      type: categoryMap.get(r.id) ?? 'fact',
      source: sourceMap.get(r.id),
      scope: scopeMap.get(r.id),
      score: r.rrfScore,
      rrf_score: r.rrfScore,
      source_weight: r.sourceWeight ?? 1.0,
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
          broadened_search: true,
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

    // Read-after-write: poll the subgraph until the fact is no longer
    // active (forget flips isActive=false). On timeout we still report
    // deleted_count: 1 — the chain write IS acknowledged — but flag
    // `partial: true` so callers know the indexer is still propagating.
    let indexed = true;
    if (success) {
      const subgraphUrl = process.env.TOTALRECLAW_SUBGRAPH_URL || `${state.serverUrl}/v1/subgraph`;
      try {
        const r = await confirmIndexed(factId, {
          subgraphUrl,
          authKeyHex: Buffer.from(state.authKey).toString('hex'),
          expect: 'inactive',
        });
        indexed = r.indexed;
      } catch {
        indexed = false;
      }
    }

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          deleted_count: success ? 1 : 0,
          fact_ids: success ? [factId] : [],
          tx_hash: txHash,
          mode: 'subgraph',
          ...(success && !indexed ? { partial: true } : {}),
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

/** Build PinOpDeps bound to the live subgraph state + relay transport. */
function buildPinDepsFromState(state: SubgraphState): PinOpDeps {
  return {
    owner: state.smartAccountAddress,
    sourceAgent: 'mcp-server',
    fetchFactById: (factId: string) => fetchFactById(
      state.smartAccountAddress,
      factId,
      state.serverUrl,
      Buffer.from(state.authKey).toString('hex'),
    ),
    decryptBlob: (hexEncryptedBlob: string) => {
      const base64 = Buffer.from(hexEncryptedBlob, 'hex').toString('base64');
      return decrypt(base64, state.encryptionKey);
    },
    encryptBlob: (plaintext: string) => {
      const base64 = encrypt(plaintext, state.encryptionKey);
      return Buffer.from(base64, 'base64').toString('hex');
    },
    submitBatch: async (payloads: Buffer[]) => {
      const config = getSubgraphConfig({
        relayUrl: state.serverUrl,
        mnemonic: state.mnemonic,
        authKeyHex: Buffer.from(state.authKey).toString('hex'),
        walletAddress: state.smartAccountAddress,
      });
      const result = await submitFactBatchOnChain(payloads, config);
      return { txHash: result.txHash, success: result.success };
    },
    generateIndices: async (text: string, entityNames: string[]) => {
      if (!text) return { blindIndices: [] };
      const wordIndices = generateBlindIndices(text);
      let lshIndices: string[] = [];
      let encryptedEmbedding: string | undefined;
      try {
        const embedding = await generateEmbedding(text);
        lshIndices = state.lshHasher.hash(embedding);
        encryptedEmbedding = encryptEmbedding(embedding, state.encryptionKey);
      } catch {
        // Best-effort: if embedding fails, word trapdoors alone still surface the claim.
      }
      const entityTrapdoors = entityNames.map((n) => computeEntityTrapdoor(n));
      return {
        blindIndices: [...wordIndices, ...lshIndices, ...entityTrapdoors],
        encryptedEmbedding,
      };
    },
    confirmIndexed: async (factId: string, expect?: 'active' | 'inactive') => {
      const subgraphUrl = process.env.TOTALRECLAW_SUBGRAPH_URL || `${state.serverUrl}/v1/subgraph`;
      const result = await confirmIndexed(factId, {
        subgraphUrl,
        authKeyHex: Buffer.from(state.authKey).toString('hex'),
        expect,
      });
      return result.indexed;
    },
  };
}

/** Build MetadataOpDeps (retype + set_scope) bound to the live subgraph state. */
function buildMetadataOpDepsFromState(state: SubgraphState): MetadataOpDeps {
  // MetadataOpDeps is structurally compatible with PinOpDeps (minus the
  // status-flip specifics). Reuse the pin deps factory and narrow to the
  // fields `executeMetadataOp` needs.
  const pinDeps = buildPinDepsFromState(state);
  return {
    owner: pinDeps.owner,
    sourceAgent: pinDeps.sourceAgent,
    fetchFactById: pinDeps.fetchFactById,
    decryptBlob: pinDeps.decryptBlob,
    encryptBlob: pinDeps.encryptBlob,
    submitBatch: pinDeps.submitBatch,
    generateIndices: pinDeps.generateIndices,
    confirmIndexed: pinDeps.confirmIndexed,
  };
}

/** Handle pin/unpin in subgraph mode. */
async function handlePinSubgraph(
  state: SubgraphState,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const deps = buildPinDepsFromState(state);
  return await handlePinSubgraphWithDeps(args, deps);
}

async function handleUnpinSubgraph(
  state: SubgraphState,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const deps = buildPinDepsFromState(state);
  return await handleUnpinSubgraphWithDeps(args, deps);
}

// ── Migration handler ─────────────────────────────────────────────────────────

/** Maximum facts per UserOp batch during migration */
const MIGRATION_BATCH_SIZE = 15;

/**
 * Handle the testnet-to-mainnet migration tool call.
 *
 * This function:
 *   1. Validates the user is on subgraph mode with Pro tier
 *   2. Fetches all active facts from the testnet (Base Sepolia) subgraph
 *   3. Fetches existing mainnet content fingerprints for idempotency
 *   4. In dry-run mode: returns a preview of what would be migrated
 *   5. In confirm mode: re-encodes and batch-submits facts to mainnet
 */
async function handleMigrateFromIndex(
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as Record<string, unknown>;
  const confirm = input?.confirm === true;

  // Must be in subgraph mode
  if (!subgraphState) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          error: 'Migration is only available with the managed service (subgraph mode). Self-hosted mode does not use chain-based storage.',
        }),
      }],
    };
  }

  const authKeyHex = Buffer.from(subgraphState.authKey).toString('hex');

  // 1. Check billing tier — must be Pro
  const billing = await checkBillingTier(SERVER_URL, subgraphState.smartAccountAddress, authKeyHex);
  if (billing.error) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          error: `Failed to verify billing tier: ${billing.error}`,
        }),
      }],
    };
  }
  if (billing.tier !== 'pro') {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          error: 'Migration requires Pro tier. Use totalreclaw_upgrade to upgrade first, then run migration.',
          current_tier: billing.tier,
        }),
      }],
    };
  }

  // 2. Fetch all active facts from the TESTNET subgraph
  //    The relay routes Pro users to the mainnet subgraph, so we need to
  //    explicitly request the testnet subgraph via the ?chain=testnet query param.
  const testnetSubgraphUrl = `${SERVER_URL.replace(/\/+$/, '')}/v1/subgraph?chain=testnet`;
  const mainnetSubgraphUrl = `${SERVER_URL.replace(/\/+$/, '')}/v1/subgraph`;

  console.error(`[migrate] Fetching testnet facts for owner ${subgraphState.smartAccountAddress}...`);
  const testnetFacts = await fetchAllFactsFromSubgraph(
    testnetSubgraphUrl,
    subgraphState.smartAccountAddress,
    authKeyHex,
  );

  if (testnetFacts.length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: true,
          mode: 'dry_run',
          testnet_facts: 0,
          already_on_mainnet: 0,
          to_migrate: 0,
          migrated: 0,
          failed_batches: 0,
          batch_results: [],
          message: 'No facts found on testnet. Nothing to migrate.',
        } satisfies MigrationResult),
      }],
    };
  }

  // 3. Check which facts already exist on mainnet (by contentFp)
  console.error(`[migrate] Checking mainnet for existing facts...`);
  const mainnetFps = await fetchMainnetContentFps(
    mainnetSubgraphUrl,
    subgraphState.smartAccountAddress,
    authKeyHex,
  );

  // Filter to facts that need migration
  const factsToMigrate = testnetFacts.filter(f => {
    if (!f.contentFp) return true; // No fingerprint — migrate it
    return !mainnetFps.has(f.contentFp);
  });

  const alreadyOnMainnet = testnetFacts.length - factsToMigrate.length;

  console.error(`[migrate] Testnet: ${testnetFacts.length} facts, already on mainnet: ${alreadyOnMainnet}, to migrate: ${factsToMigrate.length}`);

  // 4. Dry-run mode: just report
  if (!confirm) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: true,
          mode: 'dry_run',
          testnet_facts: testnetFacts.length,
          already_on_mainnet: alreadyOnMainnet,
          to_migrate: factsToMigrate.length,
          migrated: 0,
          failed_batches: 0,
          batch_results: [],
          message: factsToMigrate.length === 0
            ? `All ${testnetFacts.length} testnet facts already exist on mainnet. Nothing to migrate.`
            : `Found ${factsToMigrate.length} facts to migrate from testnet to Gnosis mainnet (${alreadyOnMainnet} already on mainnet). Call with confirm=true to proceed.`,
        } satisfies MigrationResult),
      }],
    };
  }

  // 5. Execute migration: re-encode and batch-submit
  if (factsToMigrate.length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: true,
          mode: 'executed',
          testnet_facts: testnetFacts.length,
          already_on_mainnet: alreadyOnMainnet,
          to_migrate: 0,
          migrated: 0,
          failed_batches: 0,
          batch_results: [],
          message: `All ${testnetFacts.length} testnet facts already exist on mainnet. Nothing to migrate.`,
        } satisfies MigrationResult),
      }],
    };
  }

  // Fetch blind indices from testnet for all facts to migrate.
  // The protobuf payload must include blind indices for the subgraph to create
  // BlindIndex entities on mainnet (required for search to work).
  console.error(`[migrate] Fetching blind indices for ${factsToMigrate.length} facts...`);
  const factIdsToMigrate = factsToMigrate.map(f => f.id);
  const blindIndicesMap = await fetchBlindIndicesForFacts(
    testnetSubgraphUrl,
    factIdsToMigrate,
    authKeyHex,
  );

  // Build protobuf payloads with blind indices
  const finalPayloads: Buffer[] = [];
  for (const fact of factsToMigrate) {
    const blobHex = fact.encryptedBlob.startsWith('0x')
      ? fact.encryptedBlob.slice(2)
      : fact.encryptedBlob;

    const indices = blindIndicesMap.get(fact.id) || [];

    const factPayload: FactPayload = {
      id: fact.id,
      timestamp: new Date().toISOString(),
      owner: subgraphState.smartAccountAddress,
      encryptedBlob: blobHex,
      blindIndices: indices,
      decayScore: parseFloat(fact.decayScore) || 0.5,
      source: fact.source || 'migration',
      contentFp: fact.contentFp || '',
      agentId: fact.agentId || 'mcp-server',
      encryptedEmbedding: fact.encryptedEmbedding || undefined,
    };

    finalPayloads.push(encodeFactProtobuf(factPayload));
  }

  // Batch into groups of MIGRATION_BATCH_SIZE
  const batches: Buffer[][] = [];
  for (let i = 0; i < finalPayloads.length; i += MIGRATION_BATCH_SIZE) {
    batches.push(finalPayloads.slice(i, i + MIGRATION_BATCH_SIZE));
  }

  console.error(`[migrate] Submitting ${finalPayloads.length} facts in ${batches.length} batch(es)...`);

  const batchConfig = getSubgraphConfig({
    relayUrl: subgraphState.serverUrl,
    mnemonic: subgraphState.mnemonic,
    authKeyHex,
    walletAddress: subgraphState.smartAccountAddress,
  });

  let migrated = 0;
  let failedBatches = 0;
  const batchResults: MigrationResult['batch_results'] = [];

  for (let i = 0; i < batches.length; i++) {
    const batch = batches[i];
    const batchNum = i + 1;
    console.error(`[migrate] Batch ${batchNum}/${batches.length} (${batch.length} facts)...`);

    try {
      const result = await submitFactBatchOnChain(batch, batchConfig);
      if (result.success) {
        migrated += batch.length;
        batchResults.push({
          batch_number: batchNum,
          size: batch.length,
          success: true,
          tx_hash: result.txHash,
        });
        console.error(`[migrate] Batch ${batchNum} succeeded (tx=${result.txHash.slice(0, 10)}...)`);
      } else {
        failedBatches++;
        batchResults.push({
          batch_number: batchNum,
          size: batch.length,
          success: false,
          tx_hash: result.txHash,
          error: 'UserOp included but marked as failed',
        });
        console.error(`[migrate] Batch ${batchNum} included but failed (tx=${result.txHash.slice(0, 10)}...)`);
      }
    } catch (err) {
      failedBatches++;
      const errMsg = err instanceof Error ? err.message : String(err);
      batchResults.push({
        batch_number: batchNum,
        size: batch.length,
        success: false,
        error: errMsg,
      });
      console.error(`[migrate] Batch ${batchNum} error: ${errMsg}`);
    }
  }

  const resultMessage = failedBatches === 0
    ? `Successfully migrated ${migrated} memories from testnet to Gnosis mainnet in ${batches.length} batch(es).`
    : `Migrated ${migrated}/${factsToMigrate.length} memories. ${failedBatches} batch(es) failed — re-run to retry (idempotent).`;

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        success: failedBatches === 0,
        mode: 'executed',
        testnet_facts: testnetFacts.length,
        already_on_mainnet: alreadyOnMainnet,
        to_migrate: factsToMigrate.length,
        migrated,
        failed_batches: failedBatches,
        batch_results: batchResults,
        message: resultMessage,
      } satisfies MigrationResult),
    }],
  };
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

// ── Setup tool — REMOVED in 3.2.1 (security: phrase-safety) ─────────────────
// The `totalreclaw_setup` MCP tool was removed because its response payload
// returned the BIP-39 mnemonic via the `recovery_phrase` field, which crossed
// the LLM's context window every time an agent invoked the tool. That violated
// the phrase-safety invariant ("the recovery phrase MUST NEVER cross the LLM
// context"). MCP onboarding now follows the URL-driven flow documented at
// `docs/guides/claude-code-setup.md` — agents direct users to source their
// phrase from the OpenClaw / Hermes browser pair flow (or an offline BIP-39
// generator) and paste it into the MCP host config themselves. No tool path
// exists for the agent to ever see the phrase.

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

// ── Client identification (resolved after initialize handshake) ──────────────

function getClientIdentifier(): string {
  if (!clientIdentifierResolved) {
    const clientInfo = server.getClientVersion();
    if (clientInfo?.name) {
      const name = clientInfo.name.toLowerCase().replace(/\s+/g, '-');
      setClientId(`mcp-server:${name}`);
    }
    clientIdentifierResolved = true;
    console.error(`Client identified as: ${getClientId()}`);
  }
  return getClientId();
}

// ── Layer 2 + 3: Tool handlers ───────────────────────────────────────────────

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    rememberToolDefinition,
    recallToolDefinition,
    forgetToolDefinition,
    exportToolDefinition,
    importToolDefinition,
    importFromToolDefinition,
    importBatchToolDefinition,
    consolidateToolDefinition,
    statusToolDefinition,
    upgradeToolDefinition,
    migrateToolDefinition,
    debriefToolDefinition,
    supportToolDefinition,
    accountToolDefinition,
    pinToolDefinition,
    unpinToolDefinition,
    retypeToolDefinition,
    setScopeToolDefinition,
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  // `totalreclaw_setup` was removed in 3.2.1 (security: phrase-safety).
  // Onboarding now follows the URL-driven flow at
  // `docs/guides/claude-code-setup.md`. If a stale agent / host catalog
  // still calls the old tool name, return a structured error pointing at
  // the canonical install guide — without surfacing any phrase in payload.
  if (name === 'totalreclaw_setup') {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          error: 'tool_removed',
          message:
            'The totalreclaw_setup tool was removed in @totalreclaw/mcp-server@3.2.1 ' +
            'for phrase-safety. Follow the URL-driven install flow at ' +
            'https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/claude-code-setup.md. ' +
            'The user sources their recovery phrase out-of-band (OpenClaw or Hermes ' +
            'browser pair flow, or an offline BIP-39 generator) and pastes it directly ' +
            'into TOTALRECLAW_RECOVERY_PHRASE in the MCP host config — never into chat.',
        }),
      }],
      isError: true,
    };
  }

  // Handle support tool (available in all modes, including unconfigured)
  if (name === 'totalreclaw_support') {
    const walletAddress = subgraphState?.smartAccountAddress ?? null;
    return handleSupport(walletAddress);
  }

  // In unconfigured mode, all other tools return setup guidance pointing at
  // the URL-driven install flow (NOT the deleted totalreclaw_setup tool).
  if (currentMode === 'unconfigured') {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          error: 'not_configured',
          message:
            'TotalReclaw is not configured yet. Follow the URL-driven install flow ' +
            'at https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/claude-code-setup.md — ' +
            'the user pastes their recovery phrase directly into the MCP host config (TOTALRECLAW_RECOVERY_PHRASE), ' +
            'never into chat.',
        }),
      }],
      isError: true,
    };
  }

  const mode = subgraphState ? 'subgraph' : 'http';

  try {
    // ── Billing tools (mode-independent, always use HTTP relay) ────────────
    if (name === 'totalreclaw_status') {
      const authKeyHex = subgraphState
        ? Buffer.from(subgraphState.authKey).toString('hex')
        : '';
      const enrichedArgs = {
        ...(args as Record<string, unknown>),
        wallet_address:
          (args as Record<string, unknown>)?.wallet_address ||
          subgraphState?.smartAccountAddress,
      };
      const statusResult = await handleStatus(SERVER_URL, authKeyHex, enrichedArgs);

      // Cache billing features for candidate pool sizing.
      // handleStatus stores the raw response; extract max_candidate_pool from it.
      try {
        const raw = getLastBillingResponse();
        if (raw?.features) {
          billingCache = {
            max_candidate_pool: raw.features.max_candidate_pool,
            checked_at: Date.now(),
          };
        }
      } catch {
        // Best-effort cache — don't fail the status call
      }

      return statusResult;
    }

    if (name === 'totalreclaw_upgrade') {
      const authKeyHex = subgraphState
        ? Buffer.from(subgraphState.authKey).toString('hex')
        : '';
      return await handleUpgrade(SERVER_URL, authKeyHex, args);
    }

    if (name === 'totalreclaw_account') {
      if (!subgraphState) {
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              error: 'Account details require managed service mode (subgraph). Self-hosted mode does not track billing.',
            }),
          }],
          isError: true,
        };
      }
      const authKeyHex = Buffer.from(subgraphState.authKey).toString('hex');
      const words = subgraphState.mnemonic.split(/\s+/);
      const mnemonicHint = `${words[0]} ... ${words[words.length - 1]}`;
      const getFactCount = () => getOwnerFactCount(
        subgraphState!.smartAccountAddress,
        subgraphState!.serverUrl,
        authKeyHex,
      );
      return await handleAccount(
        SERVER_URL,
        authKeyHex,
        subgraphState.smartAccountAddress,
        mnemonicHint,
        getFactCount,
      );
    }

    if (name === 'totalreclaw_migrate') {
      return await handleMigrateFromIndex(args);
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

        case 'totalreclaw_debrief': {
          try {
            const result = await handleDebriefSubgraph(subgraphState, args);
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

        case 'totalreclaw_pin': {
          try {
            const result = await handlePinSubgraph(subgraphState, args);
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

        case 'totalreclaw_unpin': {
          try {
            const result = await handleUnpinSubgraph(subgraphState, args);
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

        case 'totalreclaw_retype': {
          try {
            const deps = buildMetadataOpDepsFromState(subgraphState);
            const result = await handleRetypeWithDeps(args, deps);
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

        case 'totalreclaw_set_scope': {
          try {
            const deps = buildMetadataOpDepsFromState(subgraphState);
            const result = await handleSetScopeWithDeps(args, deps);
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

      case 'totalreclaw_import_batch':
        return await handleImportBatch(args);

      case 'totalreclaw_consolidate': {
        const result = await handleConsolidate(client, args);
        // Invalidate cache after consolidation (facts may have been deleted)
        invalidateMemoryContextCache();
        server.sendResourceUpdated({ uri: memoryContextResource.uri }).catch((err) => console.error('Failed to send resource update:', err));
        return result;
      }

      case 'totalreclaw_debrief': {
        try {
          const result = await handleDebrief(client, args);
          return result;
        } catch (error) {
          if (isQuotaExceededError(error)) {
            return quotaExceededResponse();
          }
          throw error;
        }
      }

      case 'totalreclaw_pin':
        return await handlePin(args);

      case 'totalreclaw_unpin':
        return await handleUnpin(args);

      case 'totalreclaw_retype':
        return await handleRetype(args);

      case 'totalreclaw_set_scope':
        return await handleSetScope(args);

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

// ── Show-phrase CLI subcommand ────────────────────────────────────────────────

async function showPhrase(): Promise<void> {
  try {
    const data = fs.readFileSync(CREDENTIALS_PATH, 'utf-8');
    const parsed = JSON.parse(data) as { mnemonic?: string };
    if (parsed.mnemonic) {
      console.log(parsed.mnemonic);
    } else {
      console.error('No recovery phrase found in credentials. Re-run setup.');
      process.exit(1);
    }
  } catch {
    console.error(`No credentials found at ${CREDENTIALS_PATH}. Run setup first.`);
    process.exit(1);
  }
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  if (process.argv[2] === 'setup') {
    const { runSetup } = await import('./cli/setup.js');
    await runSetup();
    return;
  }

  if (process.argv[2] === 'show-phrase') {
    await showPhrase();
    return;
  }

  // Resolve mnemonic from env var or credentials.json
  const mnemonic = resolveMnemonic();
  currentMode = detectServerMode(mnemonic);

  if (currentMode === 'subgraph' && mnemonic) {
    subgraphState = await initSubgraphState(mnemonic);
    console.error(`TotalReclaw MCP server started (managed service, owner: ${subgraphState.smartAccountAddress})`);
  } else if (currentMode === 'http') {
    console.error('TotalReclaw MCP server started (self-hosted mode)');
  } else {
    console.error('TotalReclaw MCP server started (unconfigured — set TOTALRECLAW_RECOVERY_PHRASE in the MCP host config; see docs/guides/claude-code-setup.md)');
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((error) => {
  console.error('Server error:', error);
  process.exit(1);
});
