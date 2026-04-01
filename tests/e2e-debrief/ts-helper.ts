/**
 * TypeScript helper for cross-client debrief E2E tests.
 *
 * Self-contained — does NOT import from MCP source (avoids tsx resolution issues).
 * Accepts commands via argv and executes them using the on-chain pipeline.
 *
 * Usage:
 *   npx tsx ts-helper.ts store <wallet> <text> <importance> <source>
 *   npx tsx ts-helper.ts recall <wallet> <query>
 *
 * Environment:
 *   TEST_MNEMONIC           - BIP-39 mnemonic (required)
 *   TOTALRECLAW_SERVER_URL  - Relay URL (default: api-staging)
 *   TOTALRECLAW_TEST        - Test flag (default: true)
 */

import { createHash, randomBytes, createCipheriv, createDecipheriv, createHmac } from 'crypto';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { mnemonicToSeedSync } from '@scure/bip39';
import { mnemonicToAccount } from 'viem/accounts';
import { createPublicClient, http } from 'viem';
import { baseSepolia } from 'viem/chains';
import { toSimpleSmartAccount } from 'permissionless/accounts';
import { createSmartAccountClient } from 'permissionless';
import { createPimlicoClient } from 'permissionless/clients/pimlico';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const RELAY_URL = process.env.TOTALRECLAW_SERVER_URL || 'https://api-staging.totalreclaw.xyz';
const DATA_EDGE_ADDRESS = '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca' as const;
const ENTRYPOINT_ADDRESS = '0x0000000071727De22E5E9d8BAf0edAc6f37da032' as const;
const mnemonic = process.env.TEST_MNEMONIC;

if (!mnemonic) {
  console.error('ERROR: TEST_MNEMONIC env var is required');
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Key derivation (self-contained, matches mcp/src/subgraph/crypto.ts)
// ---------------------------------------------------------------------------

const AUTH_KEY_INFO = 'totalreclaw-auth-key-v1';
const ENCRYPTION_KEY_INFO = 'totalreclaw-encryption-key-v1';
const DEDUP_KEY_INFO = 'openmemory-dedup-v1';

function deriveKeysFromMnemonic(m: string) {
  const seed = mnemonicToSeedSync(m.trim());
  const salt = Buffer.from(seed.slice(0, 32));
  const enc = (s: string) => Buffer.from(s, 'utf8');
  const seedBuf = Buffer.from(seed);
  return {
    authKey: Buffer.from(hkdf(sha256, seedBuf, salt, enc(AUTH_KEY_INFO), 32)),
    encryptionKey: Buffer.from(hkdf(sha256, seedBuf, salt, enc(ENCRYPTION_KEY_INFO), 32)),
    dedupKey: Buffer.from(hkdf(sha256, seedBuf, salt, enc(DEDUP_KEY_INFO), 32)),
    salt,
  };
}

const keys = deriveKeysFromMnemonic(mnemonic);
const authKeyHex = Buffer.from(keys.authKey).toString('hex');

// ---------------------------------------------------------------------------
// Crypto helpers
// ---------------------------------------------------------------------------

function encrypt(plaintext: string, key: Buffer): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv, { authTagLength: 16 });
  const encrypted = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, encrypted]).toString('base64');
}

function decrypt(encryptedBase64: string, key: Buffer): string {
  const combined = Buffer.from(encryptedBase64, 'base64');
  if (combined.length < 28) throw new Error('Encrypted data too short');
  const iv = combined.subarray(0, 12);
  const tag = combined.subarray(12, 28);
  const ciphertext = combined.subarray(28);
  const decipher = createDecipheriv('aes-256-gcm', key, iv, { authTagLength: 16 });
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
}

function generateBlindIndices(text: string): string[] {
  const tokens = text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .split(/\s+/)
    .filter(t => t.length >= 2);
  const seen = new Set<string>();
  const indices: string[] = [];
  for (const token of tokens) {
    const hash = createHash('sha256').update(Buffer.from(token, 'utf8')).digest('hex');
    if (!seen.has(hash)) {
      seen.add(hash);
      indices.push(hash);
    }
  }
  return indices;
}

function generateContentFingerprint(plaintext: string, dedupKey: Buffer): string {
  const normalized = plaintext.normalize('NFC').toLowerCase().replace(/\s+/g, ' ').trim();
  return createHmac('sha256', dedupKey).update(Buffer.from(normalized, 'utf8')).digest('hex');
}

// ---------------------------------------------------------------------------
// Protobuf encoder (self-contained)
// ---------------------------------------------------------------------------

function encodeVarint(value: number): Buffer {
  const bytes: number[] = [];
  let v = value >>> 0;
  while (v > 0x7f) {
    bytes.push((v & 0x7f) | 0x80);
    v >>>= 7;
  }
  bytes.push(v & 0x7f);
  return Buffer.from(bytes);
}

interface FactPayload {
  id: string;
  timestamp: string;
  owner: string;
  encryptedBlob: string; // hex
  blindIndices: string[];
  decayScore: number;
  source: string;
  contentFp: string;
  agentId: string;
}

function encodeFactProtobuf(fact: FactPayload): Buffer {
  const parts: Buffer[] = [];
  const writeString = (fn: number, val: string) => {
    if (!val) return;
    const d = Buffer.from(val, 'utf-8');
    parts.push(encodeVarint((fn << 3) | 2), encodeVarint(d.length), d);
  };
  const writeBytes = (fn: number, val: Buffer) => {
    parts.push(encodeVarint((fn << 3) | 2), encodeVarint(val.length), val);
  };
  const writeDouble = (fn: number, val: number) => {
    parts.push(encodeVarint((fn << 3) | 1));
    const buf = Buffer.alloc(8);
    buf.writeDoubleLE(val);
    parts.push(buf);
  };
  const writeVarintField = (fn: number, val: number) => {
    parts.push(encodeVarint((fn << 3) | 0), encodeVarint(val));
  };

  writeString(1, fact.id);
  writeString(2, fact.timestamp);
  writeString(3, fact.owner);
  writeBytes(4, Buffer.from(fact.encryptedBlob, 'hex'));
  for (const idx of fact.blindIndices) writeString(5, idx);
  writeDouble(6, fact.decayScore);
  writeVarintField(7, 1); // is_active
  writeVarintField(8, 2); // version
  writeString(9, fact.source);
  writeString(10, fact.contentFp);
  writeString(11, fact.agentId);
  return Buffer.concat(parts);
}

// ---------------------------------------------------------------------------
// On-chain submission
// ---------------------------------------------------------------------------

async function submitOnChain(payloads: Buffer[], walletAddress: string): Promise<string> {
  const bundlerUrl = `${RELAY_URL}/v1/bundler`;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${authKeyHex}`,
    'X-Wallet-Address': walletAddress,
    'X-TotalReclaw-Client': 'cross-client-debrief-e2e',
    'X-TotalReclaw-Test': 'true',
  };
  const authTransport = http(bundlerUrl, { fetchOptions: { headers } });
  const owner = mnemonicToAccount(mnemonic!);
  const publicClient = createPublicClient({ chain: baseSepolia, transport: http() });

  const pimlicoClient = createPimlicoClient({
    chain: baseSepolia,
    transport: authTransport,
    entryPoint: { address: ENTRYPOINT_ADDRESS, version: '0.7' },
  });

  const smartAccount = await toSimpleSmartAccount({
    client: publicClient as any,
    owner,
    entryPoint: { address: ENTRYPOINT_ADDRESS, version: '0.7' },
  });

  const smartAccountClient = createSmartAccountClient({
    account: smartAccount,
    chain: baseSepolia,
    bundlerTransport: authTransport,
    paymaster: pimlicoClient,
    userOperation: {
      estimateFeesPerGas: async () =>
        (await pimlicoClient.getUserOperationGasPrice()).fast,
    },
  });

  // Build calls array
  const calls = payloads.map(payload => ({
    to: DATA_EDGE_ADDRESS,
    value: 0n,
    data: `0x${payload.toString('hex')}` as `0x${string}`,
  }));

  const txHash = await smartAccountClient.sendTransaction(
    calls.length === 1
      ? calls[0]
      : { calls },
  );

  return txHash;
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

async function storeCommand(wallet: string, text: string, importance: number, source: string) {
  // Register first
  const saltHex = keys.salt.toString('hex');
  const authKeyHash = Buffer.from(sha256(keys.authKey)).toString('hex');
  await fetch(`${RELAY_URL}/v1/register`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-TotalReclaw-Test': 'true',
      'X-TotalReclaw-Client': 'cross-client-debrief-e2e',
    },
    body: JSON.stringify({ auth_key_hash: authKeyHash, salt: saltHex }),
  });

  // Encrypt
  const encryptedB64 = encrypt(text, keys.encryptionKey);
  const encryptedHex = Buffer.from(encryptedB64, 'base64').toString('hex');

  // Build payload
  const factId = crypto.randomUUID();
  const payload: FactPayload = {
    id: factId,
    timestamp: new Date().toISOString(),
    owner: wallet,
    encryptedBlob: encryptedHex,
    blindIndices: generateBlindIndices(text),
    decayScore: importance,
    source,
    contentFp: generateContentFingerprint(text, keys.dedupKey),
    agentId: 'cross-client-debrief-e2e',
  };

  const protobuf = encodeFactProtobuf(payload);
  const txHash = await submitOnChain([protobuf], wallet);

  console.log(`STORED: txHash=${txHash} success=true`);
  console.log(`FACT_ID: ${factId}`);
}

async function recallCommand(wallet: string, query: string) {
  const trapdoors = generateBlindIndices(query);

  const resp = await fetch(`${RELAY_URL}/v1/subgraph`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${authKeyHex}`,
      'X-TotalReclaw-Client': 'cross-client-debrief-e2e',
      'X-TotalReclaw-Test': 'true',
    },
    body: JSON.stringify({
      query: `query SearchByBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!) {
        blindIndexes(
          where: { hash_in: $trapdoors, owner: $owner, fact_: { isActive: true } }
          first: $first
          orderBy: id
          orderDirection: desc
        ) {
          id
          fact {
            id
            encryptedBlob
            isActive
          }
        }
      }`,
      variables: {
        trapdoors: trapdoors.slice(0, 20),
        owner: wallet,
        first: 100,
      },
    }),
  });

  if (!resp.ok) {
    console.log(`HTTP_ERROR: ${resp.status} ${await resp.text()}`);
    process.exit(1);
  }

  const json = (await resp.json()) as any;
  const entries = json?.data?.blindIndexes || [];

  // Deduplicate by fact ID
  const seen = new Set<string>();
  const facts: any[] = [];
  for (const entry of entries) {
    const fact = entry.fact;
    if (fact && fact.isActive && !seen.has(fact.id)) {
      seen.add(fact.id);
      facts.push(fact);
    }
  }

  console.log(`FOUND_COUNT: ${facts.length}`);

  for (const fact of facts) {
    try {
      let blob = fact.encryptedBlob;
      if (blob.startsWith('0x')) blob = blob.slice(2);
      const b64 = Buffer.from(blob, 'hex').toString('base64');
      const text = decrypt(b64, keys.encryptionKey);
      console.log(`DECRYPTED: ${text}`);
    } catch (e) {
      console.log(`DECRYPT_ERROR: ${(e as Error).message}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

import crypto from 'node:crypto';

async function main() {
  const [command, ...args] = process.argv.slice(2);

  switch (command) {
    case 'store': {
      const [wallet, text, importanceStr, source] = args;
      if (!wallet || !text || !importanceStr || !source) {
        console.error('Usage: ts-helper.ts store <wallet> <text> <importance> <source>');
        process.exit(1);
      }
      await storeCommand(wallet, text, parseFloat(importanceStr), source);
      break;
    }

    case 'recall': {
      const [wallet, query] = args;
      if (!wallet || !query) {
        console.error('Usage: ts-helper.ts recall <wallet> <query>');
        process.exit(1);
      }
      await recallCommand(wallet, query);
      break;
    }

    default:
      console.error(`Unknown command: ${command}`);
      console.error('Usage: ts-helper.ts <store|recall> [args...]');
      process.exit(1);
  }
}

main().catch(e => {
  console.error(e.message || e);
  process.exit(1);
});
