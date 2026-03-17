# @totalreclaw/client

TypeScript client library for TotalReclaw — E2EE encryption, LSH blind indexing, embedding search, and BM25+cosine reranking for AI agent memory.

Build encrypted memory into any AI agent. All encryption happens client-side — the server never sees plaintext.

**[totalreclaw.xyz](https://totalreclaw.xyz)**

## Features

- **Zero-Knowledge Encryption**: All memories and embeddings are encrypted client-side using AES-256-GCM
- **Blind Index Search**: Search your encrypted memories using LSH-based blind indices - the server never sees your query content
- **Semantic Search**: Uses all-MiniLM-L6-v2 embeddings for semantic similarity
- **Client-Side Reranking**: Combines BM25 text search with vector similarity using Reciprocal Rank Fusion (RRF)
- **Memory Lifecycle**: Built-in decay scoring for importance-based memory management

## Installation

```bash
npm install @totalreclaw/client
```

## Quick Start

```typescript
import { TotalReclaw } from '@totalreclaw/client';

// Create client instance
const client = new TotalReclaw({
  serverUrl: 'http://127.0.0.1:8080',
});

// Initialize the client
await client.init();

// Register a new user
const userId = await client.register('your-secure-master-password');
console.log('Registered with user ID:', userId);

// Store a memory
const factId = await client.remember('I prefer coffee over tea in the morning');
console.log('Stored memory:', factId);

// Search memories
const results = await client.recall('what do I like to drink?');
for (const result of results) {
  console.log(`Score: ${result.score.toFixed(3)} - ${result.fact.text}`);
}
```

## Configuration

```typescript
interface TotalReclawConfig {
  // Server URL (required)
  serverUrl: string;

  // Path to ONNX model (optional, uses default if not provided)
  modelPath?: string;

  // LSH configuration (optional)
  lshConfig?: {
    n_bits_per_table?: number;  // Default: 64
    n_tables?: number;          // Default: 12
    candidate_pool?: number;    // Default: 3000
  };

  // Request timeout in milliseconds (optional, default: 30000)
  timeout?: number;
}
```

## API Reference

### TotalReclaw Class

#### `init(): Promise<void>`

Initialize the client. Must be called before any other operations.

```typescript
await client.init();
```

#### `register(masterPassword: string): Promise<string>`

Register a new user with the server. Returns the user ID.

```typescript
const userId = await client.register('my-secure-password');
```

#### `login(userId: string, masterPassword: string, salt: Buffer): Promise<void>`

Login with existing credentials.

```typescript
await client.login(userId, 'my-secure-password', salt);
```

#### `remember(text: string, metadata?: FactMetadata): Promise<string>`

Store a new memory. Returns the fact ID.

```typescript
const factId = await client.remember('I work remotely from home', {
  importance: 0.8,
  tags: ['work', 'lifestyle'],
});
```

#### `recall(query: string, k?: number): Promise<RerankedResult[]>`

Search memories. Returns the top k results (default: 8).

```typescript
const results = await client.recall('where do I work?', 5);
```

#### `forget(factId: string): Promise<void>`

Delete a memory.

```typescript
await client.forget(factId);
```

#### `export(): Promise<ExportedData>`

Export data for portability (requires server support for fetching all facts).

```typescript
const exported = await client.export();
```

## Cryptographic Operations

The library uses industry-standard cryptographic primitives:

- **Key Derivation**: Argon2id for memory-hard password hashing
- **Encryption**: AES-256-GCM for authenticated encryption
- **Blind Indices**: SHA-256 for searchable encryption
- **Authentication**: HKDF-SHA256 for server auth

### Low-Level Crypto API

```typescript
import {
  deriveKeys,
  generateSalt,
  encrypt,
  decrypt,
  generateBlindIndices,
  generateTrapdoors,
} from '@totalreclaw/client';

// Generate salt
const salt = generateSalt(32);

// Derive keys from password
const { authKey, encryptionKey } = await deriveKeys('password', salt);

// Encrypt data
const encrypted = encrypt(Buffer.from('secret'), encryptionKey);

// Decrypt data
const decrypted = decrypt(encrypted.ciphertext, encryptionKey, encrypted.iv, encrypted.tag);

// Generate blind indices
const indices = generateBlindIndices('text content', ['lsh-bucket-1', 'lsh-bucket-2']);
```

## LSH (Locality-Sensitive Hashing)

The library implements Random Hyperplane LSH for approximate nearest neighbor search:

```typescript
import { LSHIndex, createHashBasedEmbedding } from '@totalreclaw/client';

const index = new LSHIndex({
  n_bits_per_table: 64,
  n_tables: 12,
  candidate_pool: 3000,
});

// Initialize with embedding dimension
index.initialize(384);

// Hash a vector
const embedding = createHashBasedEmbedding('some text');
const buckets = index.hashVector(embedding);
```

### LSH Configuration

Default configuration based on validation results (TS v0.3):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `n_bits_per_table` | 64 | Number of bits per hash table |
| `n_tables` | 12 | Number of independent hash tables |
| `candidate_pool` | 3000 | Candidates to retrieve for re-ranking |

For larger corpora, the candidate pool scales logarithmically:

```typescript
import { calculateCandidatePool } from '@totalreclaw/client';

const pool = calculateCandidatePool(50000); // Returns ~5000
```

## Search Reranking

The library combines multiple ranking signals:

1. **Vector Similarity**: Cosine similarity between query and document embeddings
2. **Text Similarity**: BM25 scoring on decrypted document text
3. **Decay Score**: Time-based importance decay

```typescript
import { cosineSimilarity, BM25Scorer, rrfFusion, calculateDecayScore } from '@totalreclaw/client';

// Cosine similarity
const sim = cosineSimilarity(queryEmbedding, docEmbedding);

// BM25 scoring
const scorer = new BM25Scorer();
scorer.indexDocuments([{ id: '1', text: 'hello world' }]);
const bm25Score = scorer.score('hello', '1', 'hello world');

// Decay score
const decay = calculateDecayScore(0.8, 30, 5); // importance, days since access, access count

// RRF fusion
const rankings = [
  [{ id: 'a' }, { id: 'b' }],
  [{ id: 'b' }, { id: 'a' }],
];
const fusedScores = rrfFusion(rankings);
```

## Development

### Prerequisites

- Node.js 18+
- npm 9+

### Setup

```bash
# Clone the repository
cd client

# Install dependencies
npm install

# Run tests
npm test

# Build
npm run build
```

### Running Tests

```bash
# Run all tests
npm test

# Run with coverage
npm test -- --coverage

# Run specific test file
npm test -- crypto.test.ts
```

## Security Considerations

1. **Master Password**: Choose a strong, unique password. The security of your memories depends on it.

2. **Key Storage**: The library does not persist keys. You are responsible for secure storage of:
   - User ID
   - Salt (from registration)
   - Master password (or derive keys and store them securely)

3. **Memory Tradeoffs**: Argon2id uses significant memory by design (64MB default). Adjust parameters for constrained environments.

4. **Network Security**: Always use HTTPS in production. The PoC uses HTTP for development only.

5. **Trust Model**: The server never sees:
   - Your master password
   - Your encryption keys
   - Plaintext memories
   - Plaintext embeddings
   - Query content

## License

MIT

## Contributing

See the main [TotalReclaw repository](https://github.com/p-diogo/totalreclaw) for contribution guidelines.
