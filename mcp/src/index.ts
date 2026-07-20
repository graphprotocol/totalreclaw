#!/usr/bin/env node
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

import { TotalReclaw } from '@totalreclaw/client';
import {
  handleRemember,
  handleRecall,
  handleForget,
  handleExport,
  handleImport,
  handleImportFrom,
  handleImportBatch,
  handleConsolidate,
  handleStatus,
  handleUpgrade,
  handleDebrief,
  parseDebriefResponse,
  handleSupport,
  handleAccount,
  handlePair,
} from './tools/index.js';
import { getLastBillingResponse } from './tools/status.js';
import type { ToolContext, ToolResponse } from './tools/types.js';
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
  memoryContextResource,
  invalidateMemoryContextCache,
} from './resources/index.js';
import { createTotalReclawServer } from './server-setup.js';
import {
  createCallToolHandler,
  type HandlerBundle,
  type ServerMode,
} from './dispatch.js';

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
import { resolveChainConfig, buildSubgraphOverrides } from './subgraph/chain-config.js';
import { searchSubgraph, searchSubgraphBroadened, getOwnerFactCount, fetchFactById } from './subgraph/search.js';
import { confirmIndexed } from './subgraph/confirm-indexed.js';
import {
  decodeEmbeddingUniversal,
  encodeEmbeddingPayload,
  LEGACY_F32_MARKER,
} from './embedding-codec.js';
import {
  detectAndResolveContradictions,
  type ResolutionDecision,
} from './contradiction-sync.js';
import {
  handlePin,
  handleUnpin,
  handlePinSubgraphWithDeps,
  handleUnpinSubgraphWithDeps,
  type PinOpDeps,
} from './tools/pin.js';
import {
  handleRetype,
  handleRetypeWithDeps,
  type MetadataOpDeps,
} from './tools/retype.js';
import {
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
    const credentialsRaw = fs.readFileSync(CREDENTIALS_PATH, 'utf-8');
    const parsed = JSON.parse(credentialsRaw) as { mnemonic?: string };
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
// `ServerMode` is defined alongside the dispatch table in `./dispatch.js`.

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
  /** Chain to write to — resolved verbatim from billing chain_id (#439). */
  chainId: number;
  /** DataEdge contract — resolved verbatim from billing data_edge_address (#439). Undefined ⇒ env/default. */
  dataEdgeAddress?: string;
}

/**
 * Build the getSubgraphConfig override for a managed-service write, threading
 * the billing-resolved chain + DataEdge onto every on-chain write site (#439).
 */
function stateWriteOverrides(state: SubgraphState): Partial<SubgraphStoreConfig> {
  return buildSubgraphOverrides({
    relayUrl: state.serverUrl,
    mnemonic: state.mnemonic,
    authKeyHex: Buffer.from(state.authKey).toString('hex'),
    walletAddress: state.smartAccountAddress,
    chainId: state.chainId,
    dataEdgeAddress: state.dataEdgeAddress,
  });
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

  // Resolve chain + DataEdge from the relay's billing response, verbatim (#439).
  // The relay is authoritative: it returns `chain_id` + `data_edge_address` in
  // GET /v1/billing/status. After ops-1 both tiers run on Gnosis (chain 100);
  // the legacy Free → Base Sepolia (84532) mapping was retired. Consuming the
  // relay's values verbatim means a future chain move needs zero client release.
  // (The client `TOTALRECLAW_CHAIN_ID` override was removed in v1.)
  let chainId = 100; // Gnosis default when billing is unreachable
  let dataEdgeAddress: string | undefined; // undefined ⇒ env/store default
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
        const resolved = resolveChainConfig(billing);
        chainId = resolved.chainId;
        dataEdgeAddress = resolved.dataEdgeAddress;
        console.error(
          `TotalReclaw: tier=${billing.tier ?? 'unknown'}, chain=${chainId}` +
            (dataEdgeAddress ? `, dataEdge=${dataEdgeAddress}` : '') + '.',
        );
      }
    } catch {
      // Best-effort — fall back to the Gnosis default if billing is unreachable.
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
    chainId,
    dataEdgeAddress,
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
  const fileContents = await fs.readFile(path, 'utf-8');
  const parsed = JSON.parse(fileContents) as StoredCredentials;
  return {
    userId: parsed.userId,
    salt: Buffer.from(parsed.salt, 'hex'),
  };
}

async function saveCredentials(path: string, credentials: { userId: string; salt: Buffer }): Promise<void> {
  const fs = await import('fs/promises');
  const dir = path.substring(0, path.lastIndexOf('/'));
  await fs.mkdir(dir, { recursive: true }).catch(() => {});
  const storedCredentials: StoredCredentials = {
    userId: credentials.userId,
    salt: credentials.salt.toString('hex'),
  };
  await fs.writeFile(path, JSON.stringify(storedCredentials, null, 2), 'utf-8');
}

// ── Subgraph handlers ───────────────────────────────────────────────────────

/**
 * Encrypt an embedding vector for on-chain storage.
 *
 * The pre-encryption payload is canonical f16 when @totalreclaw/core exposes
 * the codec (encodeEmbeddingPayload returns the canonical base64), else the
 * legacy little-endian f32 packing the MCP server has always used (signaled by
 * the LEGACY_F32_MARKER — the f32→f16 rounding stays in core, never here; see
 * embedding-codec.ts, #479 Part B). Either payload is then XChaCha20-Poly1305
 * encrypted; the subgraph stores the ciphertext as a string field.
 */
function encryptEmbedding(embedding: number[], encryptionKey: Buffer): string {
  const payload = encodeEmbeddingPayload(embedding);
  const preEncryption =
    payload === LEGACY_F32_MARKER ? packLegacyF32(embedding) : payload;
  return encrypt(preEncryption, encryptionKey);
}

/** Legacy f32-binary packing (the historical MCP/Python write). */
function packLegacyF32(embedding: number[]): string {
  const buf = Buffer.alloc(embedding.length * 4);
  for (let i = 0; i < embedding.length; i++) {
    buf.writeFloatLE(embedding[i], i * 4);
  }
  return buf.toString('base64');
}

/**
 * Decrypt an encrypted embedding back to a number array.
 *
 * Universal decode (#479 Part B): reads canonical f16, legacy JSON array (TS
 * plugin), and legacy f32 binary (old Python / MCP) — so a mixed-client vault
 * no longer silently degrades foreign-format facts to word-index matching.
 */
function decryptEmbedding(encryptedEmbedding: string, encryptionKey: Buffer): number[] {
  const decryptedPayload = decrypt(encryptedEmbedding, encryptionKey);
  return decodeEmbeddingUniversal(decryptedPayload);
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
      const batchConfig = getSubgraphConfig(stateWriteOverrides(state));
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

  // ── Crystal path (am-1, preferred) ──────────────────────────────────────
  if (input?.crystal !== undefined) {
    const raw = input.crystal as Record<string, unknown>;
    const narrative = typeof raw?.narrative === 'string' ? raw.narrative.trim().slice(0, 512) : '';
    if (narrative.length < 10) {
      return { content: [{ type: 'text', text: JSON.stringify({ success: false, error: 'crystal.narrative (10+ chars) is required' }) }] };
    }

    const strList = (key: string): string[] => {
      const v = (raw as Record<string, unknown>)[key];
      if (!Array.isArray(v)) return [];
      return v.map((x) => String(x).trim()).filter((x) => x.length > 0).slice(0, 10);
    };
    let importance = typeof raw?.importance === 'number' ? raw.importance : 8;
    importance = Math.max(1, Math.min(10, Math.round(importance)));

    const crystalMetadata: Record<string, unknown> = {
      subtype: 'session_crystal',
      key_outcomes: strList('key_outcomes'),
      open_threads: strList('open_threads'),
      lessons: strList('lessons'),
    };
    const filesAffected = strList('files_affected');
    if (filesAffected.length > 0) crystalMetadata.files_affected = filesAffected;
    if (typeof raw?.session_id === 'string' && raw.session_id) crystalMetadata.session_id = raw.session_id;

    try {
      const wordIndices = generateBlindIndices(narrative);
      const embedding = await generateEmbedding(narrative);
      const lshIndices = state.lshHasher.hash(embedding);
      const allIndices = [...wordIndices, ...lshIndices];

      const blobPlaintext = buildV1ClaimBlob({
        text: narrative,
        type: 'summary',
        source: 'derived',
        importance,
        metadata: crystalMetadata,
      });
      const encryptedBlob = encrypt(blobPlaintext, state.encryptionKey);
      const contentFp = generateContentFingerprint(narrative, state.dedupKey);
      const encryptedEmb = encryptEmbedding(embedding, state.encryptionKey);

      const factId = crypto.randomUUID();
      const factPayload: FactPayload = {
        id: factId,
        timestamp: new Date().toISOString(),
        owner: state.smartAccountAddress,
        encryptedBlob: Buffer.from(encryptedBlob, 'base64').toString('hex'),
        blindIndices: allIndices,
        decayScore: importance / 10,
        source: 'mcp_debrief',
        contentFp,
        agentId: 'mcp-server',
        encryptedEmbedding: encryptedEmb,
      };

      const batchConfig = getSubgraphConfig(stateWriteOverrides(state));
      const batchResult = await submitFactBatchOnChain([encodeFactProtobuf(factPayload)], batchConfig);
      console.error(`Crystal debrief stored (tx=${batchResult.txHash.slice(0, 10)}...)`);
      return {
        content: [{ type: 'text', text: JSON.stringify({ success: batchResult.success, stored: batchResult.success ? 1 : 0, fact_id: factId, tx_hash: batchResult.txHash, crystal: true }) }],
      };
    } catch (error) {
      console.error(`Crystal debrief failed: ${error instanceof Error ? error.message : String(error)}`);
      return { content: [{ type: 'text', text: JSON.stringify({ success: false, error: String(error) }) }] };
    }
  }

  // ── Legacy facts array path (backward compat) ────────────────────────────
  const factsInput = input?.facts;
  if (!Array.isArray(factsInput) || factsInput.length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({ success: false, error: 'Provide either "crystal" (preferred) or "facts" array' }),
      }],
    };
  }

  const validated = parseDebriefResponse(JSON.stringify(factsInput));
  if (validated.length === 0) {
    return {
      content: [{ type: 'text', text: JSON.stringify({ success: true, stored: 0, message: 'No valid debrief items to store (filtered by validation)' }) }],
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
      const batchConfig = getSubgraphConfig(stateWriteOverrides(state));
      const batchResult = await submitFactBatchOnChain(pendingPayloads, batchConfig);
      for (const meta of pendingFactMeta) {
        results.push({ success: batchResult.success, fact_id: meta.factId, tx_hash: batchResult.txHash });
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
    content: [{ type: 'text', text: JSON.stringify({ success: stored > 0, stored, total: validated.length, results }) }],
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
      /* applySourceWeights (Retrieval v2 Tier 1) */ false,
    );

    // 8. Format results
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const subgraphCore = require('@totalreclaw/core') as typeof import('@totalreclaw/core');
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
      date: r.createdAt != null
        ? subgraphCore.formatMemoryDate(BigInt(r.createdAt))
        : '',
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
    const config = getSubgraphConfig(stateWriteOverrides(state));

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
      const config = getSubgraphConfig(stateWriteOverrides(state));
      const batchResult = await submitFactBatchOnChain(payloads, config);
      return { txHash: batchResult.txHash, success: batchResult.success };
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
      const confirmation = await confirmIndexed(factId, {
        subgraphUrl,
        authKeyHex: Buffer.from(state.authKey).toString('hex'),
        expect,
      });
      return confirmation.indexed;
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
        message: 'Free tier monthly limit reached (250 memories). Upgrade to Pro for 1,500 memories/month on Gnosis mainnet.',
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

// ── Server wiring ─────────────────────────────────────────────────────────
// The MCP `Server` is constructed in `main()` via `createTotalReclawServer`
// (see `./server-setup.js`) and its request routing is delegated to the single
// dispatch table in `./dispatch.js`. This section builds the mode-appropriate
// handler bundles + cross-cutting deps the dispatcher needs.

let server: ReturnType<typeof createTotalReclawServer> | undefined;

// ── Client identification (resolved after initialize handshake) ──────────────

function getClientIdentifier(): string {
  if (!clientIdentifierResolved) {
    const clientInfo = server?.getClientVersion();
    if (clientInfo?.name) {
      const name = clientInfo.name.toLowerCase().replace(/\s+/g, '-');
      setClientId(`mcp-server:${name}`);
    }
    clientIdentifierResolved = true;
    console.error(`Client identified as: ${getClientId()}`);
  }
  return getClientId();
}

// ── Cross-cutting dispatch deps ──────────────────────────────────────────────

/** Invalidate the memory-context resource cache + notify subscribed clients. */
function onMutate(): void {
  invalidateMemoryContextCache();
  server
    ?.sendResourceUpdated({ uri: memoryContextResource.uri })
    .catch((err) => console.error('Failed to send resource update:', err));
}

/** Standard authentication-failure envelope (shared quota → auth funnel). */
function authHintResponse(): ToolResponse {
  return {
    content: [{ type: 'text', text: JSON.stringify({ error: AUTH_HINT_MESSAGE }) }],
    isError: true,
  };
}

// ── Mode-independent tool handlers (status / upgrade / account) ───────────────
// These talk to the relay directly and behave identically in both storage
// modes. `subgraphState` supplies the auth key + wallet when present.

async function statusHandler(args: unknown): Promise<ToolResponse> {
  const authKeyHex = subgraphState
    ? Buffer.from(subgraphState.authKey).toString('hex')
    : '';
  const enrichedArgs = {
    ...(args as Record<string, unknown>),
    wallet_address:
      (args as Record<string, unknown>)?.wallet_address ||
      subgraphState?.smartAccountAddress,
  };
  const statusResult = await handleStatus({ serverUrl: SERVER_URL, authKeyHex }, enrichedArgs);

  // Cache billing features for candidate-pool sizing. handleStatus stores the
  // raw response; extract max_candidate_pool from it.
  try {
    const raw = getLastBillingResponse();
    if (raw?.features) {
      billingCache = {
        max_candidate_pool: raw.features.max_candidate_pool,
        checked_at: Date.now(),
      };
    }
  } catch {
    // Best-effort cache — don't fail the status call.
  }

  return statusResult;
}

async function upgradeHandler(args: unknown): Promise<ToolResponse> {
  const authKeyHex = subgraphState
    ? Buffer.from(subgraphState.authKey).toString('hex')
    : '';
  return handleUpgrade({ serverUrl: SERVER_URL, authKeyHex }, args);
}

async function accountHandler(args: unknown): Promise<ToolResponse> {
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
  return handleAccount({
    serverUrl: SERVER_URL,
    authKeyHex,
    walletAddress: subgraphState.smartAccountAddress,
    mnemonicHint,
    getFactCount,
  }, args);
}

const commonHandlers: HandlerBundle = {
  totalreclaw_status: statusHandler,
  totalreclaw_upgrade: upgradeHandler,
  totalreclaw_account: accountHandler,
};

// ── Managed-service (subgraph) tool bundle ───────────────────────────────────
// Tools with no managed-service equivalent return an explicit "use self-hosted
// mode" envelope; `totalreclaw_import_batch` is intentionally absent so it
// falls through to the standard unknown-tool error, matching pre-refactor
// behaviour.

function unsupportedInSubgraph(message: string): ToolResponse {
  return { content: [{ type: 'text', text: JSON.stringify({ error: message }) }], isError: true };
}

function subgraphBundle(state: SubgraphState): HandlerBundle {
  return {
    totalreclaw_remember: (args) => handleRememberSubgraph(state, args),
    totalreclaw_recall: (args) => handleRecallSubgraph(state, args),
    totalreclaw_forget: (args) => handleForgetSubgraph(state, args),
    totalreclaw_debrief: (args) => handleDebriefSubgraph(state, args),
    totalreclaw_pin: (args) => handlePinSubgraph(state, args),
    totalreclaw_unpin: (args) => handleUnpinSubgraph(state, args),
    totalreclaw_retype: (args) => handleRetypeWithDeps(args, buildMetadataOpDepsFromState(state)),
    totalreclaw_set_scope: (args) => handleSetScopeWithDeps(args, buildMetadataOpDepsFromState(state)),
    totalreclaw_export: async () =>
      unsupportedInSubgraph('Export is not yet supported with the managed service. Use self-hosted mode for export.'),
    totalreclaw_import: async () =>
      unsupportedInSubgraph('Import is not yet supported with the managed service. Use self-hosted mode for import.'),
    totalreclaw_import_from: async () =>
      unsupportedInSubgraph('Import from external sources is not yet supported with the managed service. Use self-hosted mode for import.'),
    totalreclaw_consolidate: async () =>
      unsupportedInSubgraph('Consolidation is not supported with the managed service. On-chain facts require tombstone-based dedup which is handled automatically at store time.'),
  };
}

// ── Self-hosted (HTTP) tool bundle ───────────────────────────────────────────

function httpBundle(ctx: ToolContext): HandlerBundle {
  return {
    totalreclaw_remember: (args) => handleRemember(ctx, args),
    totalreclaw_recall: (args) => handleRecall(ctx, args),
    totalreclaw_forget: (args) => handleForget(ctx, args),
    totalreclaw_export: (args) => handleExport(ctx, args),
    totalreclaw_import: (args) => handleImport(ctx, args),
    totalreclaw_import_from: (args) => handleImportFrom(ctx, args),
    totalreclaw_import_batch: (args) => handleImportBatch(ctx, args),
    totalreclaw_consolidate: (args) => handleConsolidate(ctx, args),
    totalreclaw_debrief: (args) => handleDebrief(ctx, args),
    totalreclaw_pin: (args) => handlePin(ctx, args),
    totalreclaw_unpin: (args) => handleUnpin(ctx, args),
    totalreclaw_retype: (args) => handleRetype(ctx, args),
    totalreclaw_set_scope: (args) => handleSetScope(ctx, args),
  };
}

/** Resolve the handler bundle for the current storage mode. */
async function resolveBundle(mode: 'http' | 'subgraph'): Promise<HandlerBundle> {
  if (mode === 'subgraph') return subgraphBundle(subgraphState!);
  const client = await getClient();
  return httpBundle({ client });
}

// ── The single CallTool router ───────────────────────────────────────────────

const callToolHandler = createCallToolHandler({
  getMode: () => currentMode,
  handleSupport: (args) =>
    handleSupport({ walletAddress: subgraphState?.smartAccountAddress ?? null }, args),
  handlePair: (args) => handlePair({}, args as Record<string, unknown> | undefined),
  common: commonHandlers,
  resolveBundle,
  isQuotaExceededError,
  quotaExceededResponse,
  isAuthError,
  authHintResponse,
  onMutate,
});

// ── Show-phrase CLI subcommand ────────────────────────────────────────────────

async function showPhrase(): Promise<void> {
  try {
    const credentialsRaw = fs.readFileSync(CREDENTIALS_PATH, 'utf-8');
    const parsed = JSON.parse(credentialsRaw) as { mnemonic?: string };
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

  // ── Trajectory poller (mcp-server 3.3.0-rc.3) ─────────────────────────
  // Auto-extraction without relying on OpenClaw plugin's `agent_end` hook
  // (which 2026.5.4+ silently blocks for non-bundled plugins). The poller
  // scans `~/.openclaw/agents/<agent>/sessions/*.trajectory.jsonl` every
  // 60s, parses new prompt.submitted + model.completed events into
  // {role, content}[] turns, and calls runExtraction() when the turn-
  // accumulator threshold is met.
  //
  // 3.3.0-rc.3: real LLM-driven extraction wired in. The plugin's
  // extractor + llm-client + auth-profile reader were ported into
  // `src/extraction/` and feed the poller's `runExtraction` dep.
  // Persistence routes through `handleRememberSubgraph` so the on-chain
  // pipeline (protobuf v4 + store-time dedup + batch UserOp) is shared
  // with the explicit `totalreclaw_remember` tool. Phrase-safety: the
  // recovery phrase never crosses into the extraction LLM — see the
  // top-of-file rationale in `extraction/extractor.ts`.
  startPollerSafely();

  // Construct the MCP server and wire its request handlers to the single
  // dispatch router. Created here (not at module load) so importing this file
  // in tests does not stand up a server or connect a transport.
  server = createTotalReclawServer({
    callTool: callToolHandler,
    isManagedMode: () => !!subgraphState,
    getClient,
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

function startPollerSafely(): void {
  try {
    // Lazy-require so test environments that import this file (without
    // calling main()) don't pay the import cost.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { startTrajectoryPoller } = require('./trajectory-poller.js') as typeof import('./trajectory-poller.js');
    // 3.3.0-rc.3: live LLM-driven extraction wired in. The plugin's
    // extractor + llm-client + auth-profile reader were ported into
    // `mcp/src/extraction/` and are imported here.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const extraction = require('./extraction/extractor.js') as typeof import('./extraction/extractor.js');
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const llmClient = require('./extraction/llm-client.js') as typeof import('./extraction/llm-client.js');
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const profileReader = require('./extraction/llm-profile-reader.js') as typeof import('./extraction/llm-profile-reader.js');

    const logger = {
      info: (m: string) => console.error(`[trajectory-poller] ${m}`),
      warn: (m: string) => console.error(`[trajectory-poller] WARN ${m}`),
      error: (m: string) => console.error(`[trajectory-poller] ERROR ${m}`),
    };

    // ── Initialize the extraction LLM client ────────────────────────────────
    // Resolution cascade (highest priority first):
    //   1. Plugin-config override — N/A in MCP-only mode (no plugin manifest).
    //   2. SDK-passed openclawProviders — N/A in MCP-only mode.
    //   3. ~/.openclaw/agents/<agent>/agent/auth-profiles.json (or models.json
    //      legacy fallback). This is the primary source for MCP-only users
    //      because it's where their OpenClaw install already stores the keys.
    //   4. Env-var fallback (ZAI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY,
    //      ...) for self-hosted / dev setups.
    //
    // The `primaryModel` hint is left undefined — MCP-only mode has no
    // single primary chat model concept (the user could be running
    // OpenClaw with Claude Sonnet AND z.ai GLM-4.6 across different
    // sessions). The cascade picks the first available cheap model from
    // the priority list (zai > openai > anthropic > ...).
    const authProfileKeys = profileReader.readAllProfileKeys({
      root: profileReader.defaultAuthProfilesRoot(os.homedir()),
    });
    llmClient.initLLMClient({
      authProfileKeys,
      logger,
    });

    startTrajectoryPoller({
      logger,
      ensureInitialized: async () => {
        // No-op: subgraphState is already resolved before main() finishes.
      },
      // Block extraction until the user has paired (no credentials.json).
      isPairingPending: () => !subgraphState,
      // No import-flow tracking in mcp-server today.
      isImportActive: () => false,
      getExtractInterval: () => {
        const raw = process.env.TOTALRECLAW_EXTRACT_INTERVAL;
        const parsed = raw ? parseInt(raw, 10) : NaN;
        return Number.isFinite(parsed) && parsed > 0 ? parsed : 3;
      },
      getMaxFactsPerExtraction: () => {
        const raw = process.env.TOTALRECLAW_MAX_FACTS_PER_EXTRACTION;
        const parsed = raw ? parseInt(raw, 10) : NaN;
        return Number.isFinite(parsed) && parsed > 0 ? parsed : 6;
      },
      // Dedup-via-existing-memories is OFF for rc.3. Turn-extraction always
      // sees the full conversation + the importance bumps, so we drop a
      // few "did we already store this?" UPDATE/NOOP signals; downstream
      // store-time dedup (in handleRememberSubgraph) catches the actual
      // duplicates via cosine-similarity. Wiring an MCP-side dedup pass is
      // a follow-up — would call handleRecallSubgraph(state, {query: convText, k: 20})
      // and pass the decrypted candidates here.
      isDedupEnabled: () => false,
      getDedupCandidates: async () => [],
      // ── runExtraction: the real LLM-driven extractor ──
      // Phrase-safety: `messages` is `{role, content}[]` parsed by the
      // poller from OpenClaw's trajectory JSONL — user prompts +
      // assistant replies only. The recovery phrase NEVER crosses this
      // boundary; it lives in `subgraphState.mnemonic` (different call
      // stack, used only for HD wallet derivation in subgraph/store.ts).
      runExtraction: async (messages, mode) => {
        const facts = await extraction.extractFacts(
          messages,
          mode,
          undefined, // existingMemories — wired via getDedupCandidates path when isDedupEnabled() flips to true
          undefined, // profileContext (no plugin profile in MCP-only mode)
          logger,
        );
        return facts as unknown as Array<{ text: string; importance?: number; [k: string]: unknown }>;
      },
      filterByImportance: (facts) => {
        // The extractor already filters at parse-time (importance >= 6 unless
        // DELETE). Trust that floor here — only drop the obvious noise that
        // slipped through (e.g. 0/NaN sentinels). Same shape as the plugin's
        // hook-side filter.
        const kept: typeof facts = [];
        let dropped = 0;
        for (const f of facts) {
          const imp = typeof f.importance === 'number' && Number.isFinite(f.importance) ? f.importance : 0;
          if (imp >= 6) kept.push(f);
          else dropped++;
        }
        return { kept, dropped };
      },
      // ── persistFacts: route through handleRememberSubgraph ──
      // Encryption + protobuf v4 + on-chain submit + store-time dedup all
      // live there. Calling it with a `facts` batch (NOT the explicit
      // single-fact form) means batch-mode dedup logic runs (skip vs
      // supersede heuristics). Returns the `created` count for the
      // poller log line.
      persistFacts: async (facts) => {
        if (!subgraphState) {
          logger.warn('persistFacts: subgraphState not yet resolved — skipping persist');
          return 0;
        }
        // Map ExtractedFact → handleRememberSubgraph's batch-fact shape.
        const factsArray = facts.map((f) => {
          const fact = f as Record<string, unknown>;
          return {
            text: String(fact.text),
            importance: typeof fact.importance === 'number' ? fact.importance : 5,
            type: typeof fact.type === 'string' ? fact.type : 'claim',
            scope: typeof fact.scope === 'string' ? fact.scope : 'unspecified',
            reasoning: typeof fact.reasoning === 'string' ? fact.reasoning : undefined,
          };
        });
        try {
          const res = await handleRememberSubgraph(subgraphState, { facts: factsArray });
          // handleRememberSubgraph returns MCP tool-response shape; parse
          // the inner JSON to recover `created`.
          const inner = res.content?.[0]?.text;
          if (typeof inner === 'string') {
            const parsed = JSON.parse(inner) as { created?: number };
            return typeof parsed.created === 'number' ? parsed.created : 0;
          }
          return 0;
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          logger.warn(`persistFacts: handleRememberSubgraph threw: ${msg}`);
          return 0;
        }
      },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`[trajectory-poller] failed to start: ${msg}`);
  }
}

main().catch((error) => {
  console.error('Server error:', error);
  process.exit(1);
});
