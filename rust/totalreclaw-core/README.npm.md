# @totalreclaw/core

The shared TotalReclaw core library, compiled from Rust to WebAssembly. It holds the encryption, blind indexing, reranking, key and wallet derivation, and on-chain encoding code that every TotalReclaw client uses, so the OpenClaw plugin, the MCP server, the NanoClaw skill and the Python client all produce byte-identical results.

The same crate also ships as [`totalreclaw-core`](https://crates.io/crates/totalreclaw-core) on crates.io (Rust) and [`totalreclaw-core`](https://pypi.org/project/totalreclaw-core/) on PyPI (Python bindings).

## Should you install this?

Probably not directly. If you want encrypted memory for an AI agent, install the integration for your agent instead. Each one pulls in `@totalreclaw/core` for you:

| Agent | Package | Guide |
|-------|---------|-------|
| OpenClaw | [`@totalreclaw/totalreclaw`](https://www.npmjs.com/package/@totalreclaw/totalreclaw) | [OpenClaw setup](https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/openclaw-setup.md) |
| Claude Code, Claude Desktop, Cursor, other MCP hosts | [`@totalreclaw/mcp-server`](https://www.npmjs.com/package/@totalreclaw/mcp-server) | [Claude Code setup](https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/claude-code-setup.md) |
| Hermes | [`totalreclaw`](https://pypi.org/project/totalreclaw/) on PyPI | [Hermes setup](https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/hermes-setup.md) |
| NanoClaw | [`@totalreclaw/skill-nanoclaw`](https://www.npmjs.com/package/@totalreclaw/skill-nanoclaw) | [NanoClaw guide](https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/nanoclaw-getting-started.md) |

Install `@totalreclaw/core` yourself only if you are building a TotalReclaw client or tool and need the same primitives.

## Install

```bash
npm install @totalreclaw/core
```

## Entry points

| Import | Build | Use it from |
|--------|-------|-------------|
| `@totalreclaw/core` | wasm-bindgen `nodejs` target (CommonJS; loads the `.wasm` file synchronously) | Node.js |
| `@totalreclaw/core/web` | wasm-bindgen `web` target (ES module; call the default `init()` export once before anything else) | Browsers and bundlers |

Both builds come from the same crate and version and expose the same functions. TypeScript declarations ship with each build.

## Usage

Node.js:

```js
const { randomBytes } = require('node:crypto');
const core = require('@totalreclaw/core');

// XChaCha20-Poly1305. Keys are 32 bytes, passed as 64 hex characters.
const key = randomBytes(32).toString('hex');
const blob = core.encrypt('prefers dark mode', key); // base64: nonce || tag || ciphertext
console.log(core.decrypt(blob, key));                 // 'prefers dark mode'

// Blind indices: hex SHA-256 hashes of the normalized tokens and their stems.
// The server matches on these and never sees the words.
console.log(core.generateBlindIndices('prefers dark mode'));
```

Browser:

```js
import init, { encrypt, decrypt } from '@totalreclaw/core/web';

await init(); // fetches totalreclaw_core_bg.wasm from next to the module
```

## What's included

Function names below are the JavaScript exports. Most functions take and return plain strings (hex keys, base64 blobs, JSON); see `totalreclaw_core.d.ts` for exact signatures.

- **Encryption and indexing**: `encrypt`, `decrypt`, `generateBlindIndices`, `generateContentFingerprint`, `normalizeText`, `computeAuthKeyHash`.
- **Key derivation**: `deriveKeysFromMnemonic`, `deriveLshSeed`, `deriveBundleFromMnemonic`, plus `parseBundleV1` / `validateBundleV1` for credential bundles.
- **Store and search**: `prepareFact`, `prepareTombstone`, `encodeFactProtobuf`, `canonicalizeClaim`, `generateSearchTrapdoors`, `parseSearchResponse`, `rerank`, `rerankWithConfig`, `decryptAndRerank`, `cosineSimilarity`.
- **Wallet and ERC-4337**: `deriveEoa`, `deriveEoaAddress`, `encodeSingleCall`, `encodeBatchCall`, `hashUserOp`, `signUserOp`.
- **Dedup, contradictions and pins**: `detectContradictions`, `resolvePair`, `resolveWithCandidates`, `classifyPinIntent`, `isPinnedClaim`, decision-log helpers.
- **Extraction prompts**: `getExtractionSystemPrompt`, `getCompactionSystemPrompt`, `getDebriefSystemPrompt`.

Apart from the web build's `init()` loading the `.wasm` file, the library does no network I/O. All key material stays in your process. Functions that take a recovery phrase exist for client implementations that already hold the user's phrase; never log a phrase, put it in a prompt, or send it to an LLM.

## Links

- Repository: <https://github.com/graphprotocol/totalreclaw> (source in `rust/totalreclaw-core`)
- Website: <https://totalreclaw.xyz>
- Issues: <https://github.com/graphprotocol/totalreclaw/issues>

## License

MIT
