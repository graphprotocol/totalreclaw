/**
 * Gas Cost Measurement Script for TotalReclaw PoC v2 Payloads
 *
 * Measures the gas cost of writing encrypted fact payloads to the
 * EventfulDataEdge contract on a local Hardhat node. Generates a
 * markdown report with per-fact measurements and cost extrapolations.
 *
 * Prerequisites:
 *   1. Hardhat node running: cd contracts && npx hardhat node
 *   2. Contracts deployed: cd contracts && npx hardhat run scripts/deploy.ts --network localhost
 *
 * Usage (from subgraph/):
 *   npx tsx --tsconfig tsconfig.node.json tests/gas-measurement.ts
 */

import { ethers } from "ethers";
import * as fs from "fs";
import * as path from "path";
import * as crypto from "crypto";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const HARDHAT_RPC = "http://127.0.0.1:8545";

/** Assumed Base L2 data cost: ~$0.001 per KB of L1 calldata (post-EIP-4844). */
const BASE_L2_COST_PER_KB_USD = 0.001;

/** Base L2 gas price assumption for execution: 0.05 gwei. */
const BASE_L2_GAS_PRICE_GWEI = 0.05;

/** ETH price assumption for cost estimates. */
const ETH_PRICE_USD = 3500;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface GasResult {
  label: string;
  calldataBytes: number;
  gasUsed: number;
  blindIndexCount: number;
  hasEmbedding: boolean;
  wordCount: number;
}

interface FactPayload {
  id: string;
  timestamp: string;
  owner: string;
  encryptedBlob: string;
  blindIndices: string[];
  decayScore: number;
  source: string;
  contentFp: string;
  agentId: string;
  encryptedEmbedding?: string;
}

// ---------------------------------------------------------------------------
// Protobuf encoder (mirrored from skill/plugin/subgraph-store.ts)
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

function encodeFactProtobuf(fact: FactPayload): Buffer {
  const parts: Buffer[] = [];

  const writeString = (fieldNumber: number, value: string) => {
    if (!value) return;
    const data = Buffer.from(value, "utf-8");
    const key = (fieldNumber << 3) | 2;
    parts.push(encodeVarint(key));
    parts.push(encodeVarint(data.length));
    parts.push(data);
  };

  const writeBytes = (fieldNumber: number, value: Buffer) => {
    const key = (fieldNumber << 3) | 2;
    parts.push(encodeVarint(key));
    parts.push(encodeVarint(value.length));
    parts.push(value);
  };

  const writeDouble = (fieldNumber: number, value: number) => {
    const key = (fieldNumber << 3) | 1;
    parts.push(encodeVarint(key));
    const buf = Buffer.alloc(8);
    buf.writeDoubleLE(value);
    parts.push(buf);
  };

  const writeVarintField = (fieldNumber: number, value: number) => {
    const key = (fieldNumber << 3) | 0;
    parts.push(encodeVarint(key));
    parts.push(encodeVarint(value));
  };

  writeString(1, fact.id);
  writeString(2, fact.timestamp);
  writeString(3, fact.owner);
  writeBytes(4, Buffer.from(fact.encryptedBlob, "hex"));

  for (const index of fact.blindIndices) {
    writeString(5, index);
  }

  writeDouble(6, fact.decayScore);
  writeVarintField(7, 1); // is_active = true
  writeVarintField(8, 2); // version = 2
  writeString(9, fact.source);
  writeString(10, fact.contentFp);
  writeString(11, fact.agentId);
  // Field 12 (sequence_id) is assigned by the subgraph mapping
  if (fact.encryptedEmbedding) {
    writeString(13, fact.encryptedEmbedding);
  }

  return Buffer.concat(parts);
}

// ---------------------------------------------------------------------------
// Synthetic payload generators
// ---------------------------------------------------------------------------

/** Generate a random hex string of `byteCount` bytes. */
function randomHex(byteCount: number): string {
  return crypto.randomBytes(byteCount).toString("hex");
}

/** Generate a random SHA-256 hash (64 hex chars). */
function randomSha256(): string {
  return crypto.createHash("sha256").update(crypto.randomBytes(32)).digest("hex");
}

/** Generate a random UUID v4. */
function randomUuid(): string {
  return crypto.randomUUID();
}

/**
 * Generate a synthetic encrypted blob that approximates the size of
 * XChaCha20-Poly1305 ciphertext for `wordCount` words of plaintext.
 * Average English word = ~5 chars + 1 space = 6 bytes.
 * XChaCha20-Poly1305 adds 24-byte nonce + 16-byte auth tag = 40 bytes overhead.
 */
function generateEncryptedBlob(wordCount: number): string {
  const plaintextSize = wordCount * 6;
  const ciphertextSize = plaintextSize + 28; // IV + auth tag
  return randomHex(ciphertextSize);
}

/**
 * Generate synthetic blind indices (SHA-256 hashes).
 * In production, these are SHA-256(word + LSH bucket) values.
 */
function generateBlindIndices(count: number): string[] {
  const indices: string[] = [];
  for (let i = 0; i < count; i++) {
    indices.push(randomSha256());
  }
  return indices;
}

/**
 * Generate a synthetic encrypted embedding.
 * A 640-dim float32 embedding = 640 * 4 = 2,560 bytes.
 * After AES-GCM encryption: 2,560 + 28 = 2,588 bytes.
 * Represented as hex string.
 */
function generateEncryptedEmbedding(): string {
  const embeddingBytes = 640 * 4; // float32 x 640 dims
  const ciphertextSize = embeddingBytes + 28; // IV + auth tag
  return randomHex(ciphertextSize);
}

/** Create a fact payload with specified parameters. */
function createFactPayload(
  wordCount: number,
  blindIndexCount: number,
  includeEmbedding: boolean,
): FactPayload {
  const payload: FactPayload = {
    id: randomUuid(),
    timestamp: new Date().toISOString(),
    owner: "0x" + randomHex(20), // 20-byte Ethereum address
    encryptedBlob: generateEncryptedBlob(wordCount),
    blindIndices: generateBlindIndices(blindIndexCount),
    decayScore: 0.95,
    source: "gas-measurement",
    contentFp: randomSha256(),
    agentId: "benchmark-agent-001",
  };

  if (includeEmbedding) {
    payload.encryptedEmbedding = generateEncryptedEmbedding();
  }

  return payload;
}

// ---------------------------------------------------------------------------
// Test payload definitions
// ---------------------------------------------------------------------------

interface TestCase {
  label: string;
  wordCount: number;
  blindIndexCount: number;
  hasEmbedding: boolean;
}

const TEST_CASES: TestCase[] = [
  // Small facts — simple preferences, short statements
  { label: "Small (20w, 50 idx, emb)", wordCount: 20, blindIndexCount: 50, hasEmbedding: true },
  { label: "Small (20w, 50 idx, no emb)", wordCount: 20, blindIndexCount: 50, hasEmbedding: false },

  // Medium facts — typical conversation extractions
  { label: "Medium (50w, 80 idx, emb)", wordCount: 50, blindIndexCount: 80, hasEmbedding: true },
  { label: "Medium (50w, 80 idx, no emb)", wordCount: 50, blindIndexCount: 80, hasEmbedding: false },

  // Large facts — detailed context, long descriptions
  { label: "Large (100w, 120 idx, emb)", wordCount: 100, blindIndexCount: 120, hasEmbedding: true },
  { label: "Large (100w, 120 idx, no emb)", wordCount: 100, blindIndexCount: 120, hasEmbedding: false },

  // Edge cases
  { label: "Minimal (5w, 10 idx, no emb)", wordCount: 5, blindIndexCount: 10, hasEmbedding: false },
  { label: "Heavy indices (30w, 200 idx, emb)", wordCount: 30, blindIndexCount: 200, hasEmbedding: true },
  { label: "XL fact (200w, 150 idx, emb)", wordCount: 200, blindIndexCount: 150, hasEmbedding: true },
  { label: "XL no emb (200w, 150 idx, no emb)", wordCount: 200, blindIndexCount: 150, hasEmbedding: false },
];

// ---------------------------------------------------------------------------
// Gas measurement
// ---------------------------------------------------------------------------

async function measureGas(
  signer: ethers.Signer,
  contractAddress: string,
  testCase: TestCase,
): Promise<GasResult> {
  const fact = createFactPayload(
    testCase.wordCount,
    testCase.blindIndexCount,
    testCase.hasEmbedding,
  );

  const protobuf = encodeFactProtobuf(fact);

  const tx = await signer.sendTransaction({
    to: contractAddress,
    data: protobuf,
  });

  const receipt = await tx.wait();
  if (!receipt) {
    throw new Error(`Transaction receipt is null for ${testCase.label}`);
  }

  return {
    label: testCase.label,
    calldataBytes: protobuf.length,
    gasUsed: Number(receipt.gasUsed),
    blindIndexCount: testCase.blindIndexCount,
    hasEmbedding: testCase.hasEmbedding,
    wordCount: testCase.wordCount,
  };
}

// ---------------------------------------------------------------------------
// Report generation
// ---------------------------------------------------------------------------

function formatNumber(n: number): string {
  return n.toLocaleString("en-US");
}

function formatUsd(n: number): string {
  if (n < 0.000001) return `$${n.toExponential(2)}`;
  if (n < 0.01) return `$${n.toFixed(6)}`;
  if (n < 1) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function generateReport(results: GasResult[]): string {
  const lines: string[] = [];

  // Header
  lines.push("# TotalReclaw Gas Cost Report");
  lines.push("");
  lines.push(`**Generated:** ${new Date().toISOString()}`);
  lines.push(`**Network:** Hardhat (local)`);
  lines.push(`**Contract:** EventfulDataEdge (fallback -> Log event)`);
  lines.push(`**Payload format:** Protobuf-encoded encrypted facts`);
  lines.push("");

  // Assumptions
  lines.push("## Assumptions");
  lines.push("");
  lines.push(`| Parameter | Value |`);
  lines.push(`|-----------|-------|`);
  lines.push(`| Base L2 data cost | ${formatUsd(BASE_L2_COST_PER_KB_USD)}/KB (post-EIP-4844) |`);
  lines.push(`| Base L2 gas price | ${BASE_L2_GAS_PRICE_GWEI} gwei |`);
  lines.push(`| ETH price | ${formatUsd(ETH_PRICE_USD)} |`);
  lines.push(`| Embedding dims | 640 (float32) |`);
  lines.push(`| Encryption overhead | 40 bytes (XChaCha20-Poly1305: 24B nonce + 16B tag) |`);
  lines.push("");

  // Per-fact measurements
  lines.push("## Per-Fact Gas Measurements");
  lines.push("");
  lines.push("| Fact Type | Words | Blind Indices | Embedding | Calldata (bytes) | Gas Used | Gas/Byte |");
  lines.push("|-----------|-------|---------------|-----------|-----------------|----------|----------|");

  for (const r of results) {
    const gasPerByte = (r.gasUsed / r.calldataBytes).toFixed(1);
    lines.push(
      `| ${r.label} | ${r.wordCount} | ${r.blindIndexCount} | ${r.hasEmbedding ? "Yes" : "No"} | ${formatNumber(r.calldataBytes)} | ${formatNumber(r.gasUsed)} | ${gasPerByte} |`,
    );
  }
  lines.push("");

  // Gas per byte analysis
  const gasPerByteValues = results.map((r) => r.gasUsed / r.calldataBytes);
  const avgGasPerByte = gasPerByteValues.reduce((a, b) => a + b, 0) / gasPerByteValues.length;
  const minGasPerByte = Math.min(...gasPerByteValues);
  const maxGasPerByte = Math.max(...gasPerByteValues);

  lines.push("## Gas Per Byte Analysis");
  lines.push("");
  lines.push(`| Metric | Gas/Byte |`);
  lines.push(`|--------|----------|`);
  lines.push(`| Average | ${avgGasPerByte.toFixed(1)} |`);
  lines.push(`| Min | ${minGasPerByte.toFixed(1)} |`);
  lines.push(`| Max | ${maxGasPerByte.toFixed(1)} |`);
  lines.push("");
  lines.push("> **Note:** Gas per byte decreases with payload size because the fixed base");
  lines.push("> cost (~21,000 gas for tx + ~1,200 for Log event) is amortized over more bytes.");
  lines.push("");

  // Embedding cost breakdown
  const withEmb = results.filter((r) => r.hasEmbedding);
  const withoutEmb = results.filter((r) => !r.hasEmbedding);

  if (withEmb.length > 0 && withoutEmb.length > 0) {
    lines.push("## Embedding Cost Impact");
    lines.push("");
    lines.push("| Comparison | Avg Gas (with emb) | Avg Gas (no emb) | Embedding Overhead |");
    lines.push("|------------|-------------------|-----------------|-------------------|");

    // Group by word count for matched comparison
    const wordCounts = [...new Set(results.map((r) => r.wordCount))];
    for (const wc of wordCounts) {
      const we = results.find((r) => r.wordCount === wc && r.hasEmbedding);
      const woe = results.find((r) => r.wordCount === wc && !r.hasEmbedding);
      if (we && woe) {
        const overhead = we.gasUsed - woe.gasUsed;
        const pct = ((overhead / woe.gasUsed) * 100).toFixed(1);
        lines.push(
          `| ${wc} words | ${formatNumber(we.gasUsed)} | ${formatNumber(woe.gasUsed)} | +${formatNumber(overhead)} (+${pct}%) |`,
        );
      }
    }
    lines.push("");
  }

  // Cost extrapolation
  lines.push("## Cost Extrapolation (Base L2)");
  lines.push("");
  lines.push("Estimated costs on Base L2 mainnet for different fact volumes.");
  lines.push("");

  // Use the "Medium with embedding" result as the representative fact
  const representative =
    results.find((r) => r.label.startsWith("Medium") && r.hasEmbedding) || results[0];
  const factSizeKB = representative.calldataBytes / 1024;

  lines.push(`**Representative fact:** ${representative.label}`);
  lines.push(`- Calldata: ${formatNumber(representative.calldataBytes)} bytes (${factSizeKB.toFixed(2)} KB)`);
  lines.push(`- Gas: ${formatNumber(representative.gasUsed)}`);
  lines.push("");

  // L2 cost model:
  //   1. L1 data cost (dominant): calldata_KB * $0.001/KB
  //   2. L2 execution cost: gas * gasPrice * ethPrice
  const l2ExecCostPerFact =
    (representative.gasUsed * BASE_L2_GAS_PRICE_GWEI * 1e-9) * ETH_PRICE_USD;
  const l1DataCostPerFact = factSizeKB * BASE_L2_COST_PER_KB_USD;
  const totalCostPerFact = l2ExecCostPerFact + l1DataCostPerFact;

  lines.push("### Per-Fact Cost Breakdown");
  lines.push("");
  lines.push(`| Component | Cost |`);
  lines.push(`|-----------|------|`);
  lines.push(`| L2 execution | ${formatUsd(l2ExecCostPerFact)} |`);
  lines.push(`| L1 data posting | ${formatUsd(l1DataCostPerFact)} |`);
  lines.push(`| **Total per fact** | **${formatUsd(totalCostPerFact)}** |`);
  lines.push("");

  // Volume extrapolation
  const volumes = [
    { label: "5,000 facts", count: 5_000 },
    { label: "50,000 facts", count: 50_000 },
    { label: "500,000 facts", count: 500_000 },
    { label: "5,000,000 facts", count: 5_000_000 },
    { label: "50,000,000 facts", count: 50_000_000 },
  ];

  lines.push("### Volume Extrapolation");
  lines.push("");
  lines.push("| Volume | Total Gas | Total Calldata | L2 Exec Cost | L1 Data Cost | Total Cost |");
  lines.push("|--------|-----------|---------------|-------------|-------------|-----------|");

  for (const vol of volumes) {
    const totalGas = representative.gasUsed * vol.count;
    const totalCalldata = representative.calldataBytes * vol.count;
    const totalCalldataMB = totalCalldata / (1024 * 1024);
    const l2Exec = l2ExecCostPerFact * vol.count;
    const l1Data = l1DataCostPerFact * vol.count;
    const total = totalCostPerFact * vol.count;

    lines.push(
      `| ${vol.label} | ${formatNumber(totalGas)} | ${totalCalldataMB.toFixed(1)} MB | ${formatUsd(l2Exec)} | ${formatUsd(l1Data)} | ${formatUsd(total)} |`,
    );
  }
  lines.push("");

  // All fact types cost table
  lines.push("### Cost Per Fact Type (Base L2)");
  lines.push("");
  lines.push("| Fact Type | Calldata | L2 Exec | L1 Data | Total |");
  lines.push("|-----------|----------|---------|---------|-------|");

  for (const r of results) {
    const sizeKB = r.calldataBytes / 1024;
    const exec = (r.gasUsed * BASE_L2_GAS_PRICE_GWEI * 1e-9) * ETH_PRICE_USD;
    const data = sizeKB * BASE_L2_COST_PER_KB_USD;
    const total = exec + data;
    lines.push(
      `| ${r.label} | ${formatNumber(r.calldataBytes)} B | ${formatUsd(exec)} | ${formatUsd(data)} | ${formatUsd(total)} |`,
    );
  }
  lines.push("");

  // Summary
  lines.push("## Key Takeaways");
  lines.push("");
  lines.push("1. **Base gas cost** is dominated by the 21,000 intrinsic transaction gas.");
  lines.push("   The Log event itself adds ~1,200 gas plus calldata costs.");
  lines.push("2. **Embeddings** (640-dim float32, encrypted) add ~5,168 bytes of calldata,");
  lines.push("   which is the single largest component of most facts.");
  lines.push("3. **Blind indices** at 64 hex chars (32 bytes) + protobuf overhead each,");
  lines.push("   scale linearly. 100 indices ~ 6.6 KB of calldata.");
  lines.push("4. **On Base L2**, costs are extremely low: a medium fact with embedding");
  lines.push(`   costs approximately ${formatUsd(totalCostPerFact)} per write.`);
  lines.push("5. **Batching** multiple facts per transaction would amortize the 21,000");
  lines.push("   intrinsic gas cost, reducing per-fact cost further.");
  lines.push("");

  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  console.log("=== TotalReclaw Gas Cost Measurement ===\n");

  // Connect to Hardhat node
  const provider = new ethers.JsonRpcProvider(HARDHAT_RPC);

  try {
    const network = await provider.getNetwork();
    console.log(`Connected to network: chainId=${network.chainId}`);
  } catch (err) {
    console.error("ERROR: Cannot connect to Hardhat node at", HARDHAT_RPC);
    console.error("Make sure the node is running: cd contracts && npx hardhat node");
    process.exit(1);
  }

  // Use Hardhat account #0 as the deployer (has ETH by default)
  const signer = await provider.getSigner(0);
  const signerAddress = await signer.getAddress();
  console.log(`Signer (deployer/entryPoint): ${signerAddress}`);

  // Read deployed contract address
  const addressesPath = path.resolve(__dirname, "../../contracts/deployed-addresses.json");
  if (!fs.existsSync(addressesPath)) {
    console.error("ERROR: deployed-addresses.json not found at", addressesPath);
    console.error("Deploy contracts first: cd contracts && npx hardhat run scripts/deploy.ts --network localhost");
    process.exit(1);
  }

  const addresses = JSON.parse(fs.readFileSync(addressesPath, "utf-8"));
  const dataEdgeAddress: string = addresses.eventfulDataEdge;

  console.log(`DataEdge contract: ${dataEdgeAddress}`);

  // The EventfulDataEdge fallback checks `require(msg.sender == entryPoint)`.
  // On localhost, deploy.ts sets entryPoint to the canonical ERC-4337 address.
  // Use setEntryPoint() to update it to the deployer for local testing.
  if (signerAddress.toLowerCase() !== addresses.entryPoint.toLowerCase()) {
    console.log("EntryPoint != deployer — calling setEntryPoint() to update...");
    const abi = ["function setEntryPoint(address _newEntryPoint) external"];
    const dataEdge = new ethers.Contract(dataEdgeAddress, abi, signer);
    const tx = await dataEdge.setEntryPoint(signerAddress);
    await tx.wait();
    console.log(`EntryPoint updated to deployer: ${signerAddress}`);
  }

  console.log("");

  // Verify the contract exists and we can call it
  const code = await provider.getCode(dataEdgeAddress);
  if (code === "0x") {
    console.error("ERROR: No contract at", dataEdgeAddress);
    console.error("The Hardhat node may have been restarted. Re-deploy contracts.");
    process.exit(1);
  }

  // Run gas measurements
  console.log(`Running ${TEST_CASES.length} test cases...\n`);

  const results: GasResult[] = [];

  for (const testCase of TEST_CASES) {
    process.stdout.write(`  ${testCase.label} ... `);
    try {
      const result = await measureGas(signer, dataEdgeAddress, testCase);
      console.log(
        `${formatNumber(result.gasUsed)} gas, ${formatNumber(result.calldataBytes)} bytes`,
      );
      results.push(result);
    } catch (err: any) {
      console.log(`FAILED: ${err.message}`);
      // Continue with remaining tests
    }
  }

  if (results.length === 0) {
    console.error("\nERROR: All measurements failed. Check Hardhat node and contract deployment.");
    process.exit(1);
  }

  console.log(`\n${results.length}/${TEST_CASES.length} measurements completed.\n`);

  // Generate and write report
  const report = generateReport(results);
  const reportPath = path.resolve(__dirname, "gas-report.md");
  fs.writeFileSync(reportPath, report);
  console.log(`Report written to ${reportPath}`);

  // Print summary to console
  console.log("\n--- Quick Summary ---");
  for (const r of results) {
    const gasPerByte = (r.gasUsed / r.calldataBytes).toFixed(1);
    console.log(
      `  ${r.label.padEnd(40)} ${String(r.gasUsed).padStart(8)} gas  ${String(r.calldataBytes).padStart(7)} bytes  ${gasPerByte} gas/byte`,
    );
  }
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
