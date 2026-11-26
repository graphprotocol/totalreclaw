/**
 * Quick local roundtrip test: store 3 facts via MCP crypto → local Anvil → recall via subgraph.
 *
 * Run:
 *   TOTALRECLAW_SUBGRAPH_URL=http://localhost:39000/subgraphs/name/totalreclaw \
 *   TOTALRECLAW_LOCAL_RPC=http://127.0.0.1:39545 \
 *   TOTALRECLAW_DATA_EDGE_ADDRESS=0x5fbdb2315678afecb367f032d93f642f64180aa3 \
 *   TOTALRECLAW_CHAIN_ID=31337 \
 *   TOTALRECLAW_RECOVERY_PHRASE="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about" \
 *   node test-local-roundtrip.mjs
 */

import { submitFactLocal, encodeFactProtobufKeyed, getSubgraphConfig } from './dist/subgraph/store.js';
import { KeyedTagger } from './dist/subgraph/keyed-tags.js';
import { LSHHasher } from './dist/subgraph/lsh.js';
import { deriveKeys, deriveLshSeed } from './dist/subgraph/crypto.js';
import { searchSubgraphBroadened } from './dist/subgraph/search.js';
import { createPublicClient, http } from 'viem';
import { foundry } from 'viem/chains';
import { mnemonicToAccount } from 'viem/accounts';
import crypto from 'crypto';

const mnemonic = process.env.TOTALRECLAW_RECOVERY_PHRASE;
if (!mnemonic) { console.error('Set TOTALRECLAW_RECOVERY_PHRASE'); process.exit(1); }

const config = getSubgraphConfig();
const ownerAccount = mnemonicToAccount(mnemonic);
const ownerAddress = ownerAccount.address.toLowerCase();

console.log('=== Local Roundtrip Test ===');
console.log(`  Owner (EOA): ${ownerAddress}`);
console.log(`  DataEdge: ${config.dataEdgeAddress}`);
console.log(`  Subgraph: ${process.env.TOTALRECLAW_SUBGRAPH_URL}`);
console.log();

// Simple encryption (for test only — real MCP uses HKDF + WASM)
const encKey = crypto.createHash('sha256').update(mnemonic + '-enc').digest();

// Keyed blind indices (internal#573): tags come from the vault keys via core, never hashed here.
const vault = deriveKeys(mnemonic);
const tagger = new KeyedTagger(
  vault.encryptionKey,
  vault.dedupKey,
  new LSHHasher(deriveLshSeed(mnemonic, vault.salt), 640),
);

function encrypt(text) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', encKey, iv);
  const enc = Buffer.concat([cipher.update(Buffer.from(text)), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, enc]);
}

// Store 3 test facts
const testFacts = [
  'User lives in Lisbon, Portugal',
  'User prefers TypeScript for backend development',
  'User chose PostgreSQL because of JSONB support',
];

console.log('Storing 3 test facts...');
for (const text of testFacts) {
  const id = crypto.randomUUID();
  const blob = encrypt(text);
  const contentFp = tagger.contentFp(text);
  const blindIndices = tagger.factTags(text);

  const protobuf = encodeFactProtobufKeyed({
    id,
    timestamp: new Date().toISOString(),
    owner: ownerAddress,
    encryptedBlob: blob.toString('hex'),
    blindIndices,
    decayScore: 0.9,
    source: 'test-roundtrip',
    contentFp,
    agentId: 'test',
  });

  const result = await submitFactLocal(Buffer.from(protobuf), config);
  console.log(`  Stored: "${text.slice(0, 40)}..." tx: ${result.txHash.slice(0, 16)}...`);
}

// Wait for subgraph indexing
console.log('\nWaiting for subgraph to index...');
const publicClient = createPublicClient({ chain: foundry, transport: http('http://127.0.0.1:39545') });
const latestBlock = await publicClient.getBlockNumber();
const subgraphUrl = process.env.TOTALRECLAW_SUBGRAPH_URL;

for (let i = 0; i < 30; i++) {
  const res = await fetch(subgraphUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: `{ _meta { block { number } } }` }),
  });
  const json = await res.json();
  const indexed = json.data?._meta?.block?.number;
  if (indexed && BigInt(indexed) >= latestBlock) {
    console.log(`  Indexed to block ${indexed}`);
    break;
  }
  await new Promise(r => setTimeout(r, 1000));
}

// Search via broadened search
console.log('\nRecalling facts via broadened search...');
const results = await searchSubgraphBroadened(ownerAddress, 10);
console.log(`  Found: ${results.length} facts`);

// Also test blind index search
console.log('\nSearching for "typescript"...');
const tsTags = tagger.factTags('typescript');
const { searchSubgraph } = await import('./dist/subgraph/search.js');
const searchResults = await searchSubgraph(ownerAddress, tsTags, 10);
console.log(`  Blind index search found: ${searchResults.length} facts`);

console.log('\n=== Test Complete ===');
