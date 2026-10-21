/**
 * E2E (PRD-04 F8 / DEP-6): one real MCP `totalreclaw_remember` against
 * STAGING, then prove from chain data that the outer protobuf carries no
 * field 9 (`source`) / field 11 (`agent_id`) and landed on the STAGING
 * DataEdge.
 *
 *   1. A throwaway BIP-39 phrase is generated in memory (never printed,
 *      never written by this script) and a throwaway HOME is used, so no real
 *      ~/.totalreclaw is read or written.
 *   2. The locally built MCP server (mcp/dist/index.js) is spawned over stdio
 *      with TOTALRECLAW_SERVER_URL = the staging relay and
 *      TOTALRECLAW_DATA_EDGE_ADDRESS = the staging DataEdge, so a failed
 *      billing lookup can never fall back to the production DataEdge
 *      (mcp/src/index.ts initSubgraphState fallback).
 *   3. MCP handshake (initialize, notifications/initialized), then
 *      tools/call totalreclaw_remember { fact } -> results[0].{fact_id, tx_hash}.
 *   4. Gnosis RPC: tx receipt -> the Log(bytes) event from the STAGING
 *      DataEdge -> outer protobuf: no field 9/11, field 1 == fact_id,
 *      field 8 == 4, and no log at all from the PRODUCTION DataEdge.
 *   5. Staging subgraph (relay /v1/subgraph): the fact is indexed with the
 *      same txHash.
 *
 * Prereqs:  cd mcp && npm install --legacy-peer-deps && npm run build
 *           cd tests/e2e-batch && npm install
 * Run:      cd tests/e2e-batch && npx tsx e2e-dep6-mcp-outer-fields.ts
 * The first run downloads the embedding model (~344 MB) into
 * mcp/node_modules; allow up to 15 minutes.
 */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
import { createPublicClient, decodeEventLog, http, parseAbi, type Hex } from 'viem';
import { gnosis } from 'viem/chains';

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

// STAGING ONLY — hardcoded on purpose; there is no override.
const RELAY_URL = 'https://api-staging.totalreclaw.xyz';
const STAGING_DATA_EDGE = '0xe7a4d2677b686e13775ba9092631089e35f0bb91';
const PRODUCTION_DATA_EDGE = '0xc445af1d4eb9fce4e1e61fe96ea7b8febf03c5ca';
const GNOSIS_RPC_URL = process.env.GNOSIS_RPC_URL || 'https://rpc.gnosischain.com';
const MCP_ENTRY = join(__dirname, '..', '..', 'mcp', 'dist', 'index.js');
const MCP_CRYPTO = join(__dirname, '..', '..', 'mcp', 'dist', 'subgraph', 'crypto.js');
const FORBIDDEN_FIELDS = [9, 11];
const LOG_ABI = parseAbi(['event Log(bytes data)']);
const TEST_HEADERS = {
  'X-TotalReclaw-Test': 'true',
  'X-TotalReclaw-Client': 'e2e-dep6-mcp-outer-fields',
};

let passed = 0;
let failed = 0;
function check(cond: boolean, name: string): void {
  if (cond) {
    console.log(`ok ${++passed + failed} - ${name}`);
  } else {
    console.error(`not ok ${passed + ++failed} - ${name}`);
    process.exitCode = 1;
  }
}

interface PbField {
  field: number;
  wireType: number;
  varint?: number;
  bytes?: Buffer;
}

function readVarint(buf: Buffer, offset: number): [number, number] {
  let value = 0;
  let shift = 0;
  let i = offset;
  while (i < buf.length) {
    const byte = buf[i];
    value += (byte & 0x7f) * 2 ** shift;
    i += 1;
    if ((byte & 0x80) === 0) return [value, i];
    shift += 7;
  }
  throw new Error('truncated varint');
}

function parseFields(buf: Buffer): PbField[] {
  const out: PbField[] = [];
  let i = 0;
  while (i < buf.length) {
    const [key, afterKey] = readVarint(buf, i);
    i = afterKey;
    const field = Math.floor(key / 8);
    const wireType = key % 8;
    if (wireType === 0) {
      const [value, next] = readVarint(buf, i);
      out.push({ field, wireType, varint: value });
      i = next;
    } else if (wireType === 1) {
      out.push({ field, wireType, bytes: buf.subarray(i, i + 8) });
      i += 8;
    } else if (wireType === 2) {
      const [len, afterLen] = readVarint(buf, i);
      out.push({ field, wireType, bytes: buf.subarray(afterLen, afterLen + len) });
      i = afterLen + len;
    } else if (wireType === 5) {
      out.push({ field, wireType, bytes: buf.subarray(i, i + 4) });
      i += 4;
    } else {
      throw new Error(`unsupported wire type ${wireType}`);
    }
  }
  if (i !== buf.length) throw new Error('protobuf walk overran the buffer');
  return out;
}

/** Minimal newline-delimited JSON-RPC client for an MCP server on stdio. */
class StdioRpc {
  private nextId = 1;
  private readonly pending = new Map<number, (msg: Record<string, unknown>) => void>();
  private readonly child: ChildProcessWithoutNullStreams;

  constructor(child: ChildProcessWithoutNullStreams) {
    this.child = child;
    createInterface({ input: child.stdout }).on('line', (line) => {
      let msg: Record<string, unknown>;
      try {
        msg = JSON.parse(line) as Record<string, unknown>;
      } catch {
        return; // not a JSON-RPC frame
      }
      const id = msg.id;
      if (typeof id === 'number' && this.pending.has(id)) {
        const resolve = this.pending.get(id)!;
        this.pending.delete(id);
        resolve(msg);
      }
    });
  }

  request(method: string, params: unknown, timeoutMs: number): Promise<Record<string, unknown>> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method} timed out after ${timeoutMs} ms`));
      }, timeoutMs);
      this.pending.set(id, (msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
      this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  notify(method: string): void {
    this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method }) + '\n');
  }
}

async function main(): Promise<void> {
  const runId = Date.now().toString(36);
  const { generateMnemonic } = await import('@scure/bip39');
  const { wordlist } = await import('@scure/bip39/wordlists/english');
  const mnemonic = generateMnemonic(wordlist);
  let authKeyHex = '';
  const secrets = (): string[] => [mnemonic, authKeyHex].filter((s) => s.length > 0);
  const redact = (text: string): string =>
    secrets().reduce((acc, s) => acc.split(s).join('[REDACTED]'), text);

  const tmpHome = mkdtempSync(join(tmpdir(), 'dep6-e2e-'));
  const stderrLines: string[] = [];
  const child = spawn(process.execPath, [MCP_ENTRY], {
    env: {
      PATH: process.env.PATH ?? '',
      HOME: tmpHome,
      TOTALRECLAW_SERVER_URL: RELAY_URL,
      TOTALRECLAW_RECOVERY_PHRASE: mnemonic,
      TOTALRECLAW_DATA_EDGE_ADDRESS: STAGING_DATA_EDGE,
      TOTALRECLAW_SESSION_ID: `dep6-e2e-${runId}`,
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  child.stderr.on('data', (chunk) => stderrLines.push(...String(chunk).split('\n')));

  try {
    const rpc = new StdioRpc(child);

    // 1. MCP handshake. protocolVersion 2024-11-05 is accepted by every
    //    stdio-era @modelcontextprotocol/sdk server (DEP-15 keeps this path).
    const init = await rpc.request(
      'initialize',
      {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'e2e-dep6-mcp-outer-fields', version: '1.0.0' },
      },
      180_000,
    );
    check(init.error === undefined && init.result !== undefined, 'MCP initialize succeeded');
    rpc.notify('notifications/initialized');

    // 2. One explicit remember (synthetic, non-sensitive text).
    const factText = `DEP-6 staging E2E canary ${runId}: I prefer oolong tea in the afternoon.`;
    const call = await rpc.request(
      'tools/call',
      { name: 'totalreclaw_remember', arguments: { fact: factText, importance: 6 } },
      900_000,
    );
    const result = call.result as { content?: Array<{ text?: string }> } | undefined;
    const body = JSON.parse(result?.content?.[0]?.text ?? '{}') as {
      success?: boolean;
      mode?: string;
      results?: Array<{ success?: boolean; fact_id?: string; tx_hash?: string }>;
    };
    check(body.success === true && body.mode === 'subgraph', 'totalreclaw_remember succeeded in managed (subgraph) mode');
    const factId = body.results?.[0]?.fact_id ?? '';
    const txHash = (body.results?.[0]?.tx_hash ?? '') as Hex;
    check(factId.length > 0 && /^0x[0-9a-fA-F]{64}$/.test(txHash), `remember returned fact_id + tx_hash (${txHash})`);
    if (!factId || !txHash) throw new Error('no fact_id / tx_hash — cannot continue');

    // 3. Chain: receipt -> DataEdge Log(bytes) -> outer protobuf.
    const publicClient = createPublicClient({ chain: gnosis, transport: http(GNOSIS_RPC_URL) });
    const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash, timeout: 120_000 });
    check(receipt.status === 'success', 'tx receipt status = success');
    check(
      !receipt.logs.some((l) => l.address.toLowerCase() === PRODUCTION_DATA_EDGE),
      'no log emitted by the PRODUCTION DataEdge',
    );
    const edgeLogs = receipt.logs.filter((l) => l.address.toLowerCase() === STAGING_DATA_EDGE);
    check(edgeLogs.length >= 1, `receipt has ${edgeLogs.length} Log(bytes) event(s) from the STAGING DataEdge`);

    let found: PbField[] | null = null;
    for (const log of edgeLogs) {
      const decoded = decodeEventLog({ abi: LOG_ABI, data: log.data, topics: log.topics });
      const payload = Buffer.from((decoded.args as { data: Hex }).data.slice(2), 'hex');
      const fields = parseFields(payload);
      const id = fields.find((f) => f.field === 1)?.bytes?.toString('utf8');
      if (id === factId) found = fields;
    }
    check(found !== null, 'found the remembered fact (field 1 == fact_id) in a staging DataEdge Log');
    if (found) {
      const numbers = found.map((f) => f.field);
      check(
        !numbers.some((n) => FORBIDDEN_FIELDS.includes(n)),
        `on-chain outer protobuf has no field 9 (source) / 11 (agent_id) — fields [${numbers.join(',')}]`,
      );
      check(found.find((f) => f.field === 8)?.varint === 4, 'on-chain outer protobuf version field = 4');
    }

    // 4. Staging subgraph (through the staging relay) indexed the same tx.
    const { deriveKeys } = require(MCP_CRYPTO) as { deriveKeys: (phrase: string) => { authKey: Buffer } };
    authKeyHex = deriveKeys(mnemonic).authKey.toString('hex');
    const query = 'query($ids: [String!]) { facts(where: { id_in: $ids }) { id txHash } }';
    let indexedTx = '';
    for (let i = 0; i < 30 && !indexedTx; i++) {
      await new Promise((r) => setTimeout(r, 10_000));
      const res = await fetch(`${RELAY_URL}/v1/subgraph`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${authKeyHex}`, ...TEST_HEADERS },
        body: JSON.stringify({ query, variables: { ids: [factId] } }),
      });
      const json = (await res.json().catch(() => ({}))) as { data?: { facts?: Array<{ id: string; txHash: string }> } };
      indexedTx = json.data?.facts?.[0]?.txHash ?? '';
      console.log(`# subgraph poll ${i + 1}: ${indexedTx ? 'indexed' : 'not yet'}`);
    }
    check(indexedTx.toLowerCase() === txHash.toLowerCase(), 'staging subgraph indexed the fact with the same txHash');
  } catch (err) {
    check(false, `unexpected error: ${redact(err instanceof Error ? err.message : String(err))}`);
  } finally {
    child.kill('SIGTERM');
    rmSync(tmpHome, { recursive: true, force: true });
    if (failed > 0) {
      console.error('# last MCP stderr lines (secrets redacted):');
      for (const line of stderrLines.slice(-60)) console.error(`#   ${redact(line)}`);
    }
  }

  console.log(`\n# e2e-dep6 — ${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
  process.exit(0);
}

main().catch((e) => {
  console.error('FATAL:', e instanceof Error ? e.message : String(e));
  process.exit(1);
});
