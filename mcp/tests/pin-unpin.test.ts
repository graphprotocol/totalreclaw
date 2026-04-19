/** Pin/unpin tool tests for MCP server (Slice 2e-mcp, Phase 2). */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  pinToolDefinition,
  unpinToolDefinition,
  handlePin,
  executePinOperation,
  decisionsLogPath,
  feedbackLogPath,
  findDecisionForPin,
  buildFeedbackFromDecision,
  type DecisionLogEntry,
  type FeedbackEntry,
  type PinOpDeps,
  type PinOpResult,
  type ScoreComponents,
} from '../src/tools/pin';

import { buildCanonicalClaim } from '../src/claims-helper';

// Isolate the on-disk state dir so tests never touch ~/.totalreclaw/.
const TEST_STATE_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-pin-test-'));
process.env.TOTALRECLAW_STATE_DIR = TEST_STATE_DIR;

afterAll(() => {
  try { fs.rmSync(TEST_STATE_DIR, { recursive: true, force: true }); } catch { /* ignore */ }
  delete process.env.TOTALRECLAW_STATE_DIR;
});

// ─── Tool definitions ─────────────────────────────────────────────────────────

describe('pinToolDefinition', () => {
  test('has correct name', () => {
    expect(pinToolDefinition.name).toBe('totalreclaw_pin');
  });

  test('has non-empty description', () => {
    expect(typeof pinToolDefinition.description).toBe('string');
    expect(pinToolDefinition.description.length).toBeGreaterThan(0);
  });

  test('input schema is object with fact_id required', () => {
    expect(pinToolDefinition.inputSchema.type).toBe('object');
    expect(pinToolDefinition.inputSchema.properties).toHaveProperty('fact_id');
    expect(pinToolDefinition.inputSchema.properties.fact_id.type).toBe('string');
    expect(pinToolDefinition.inputSchema.required).toContain('fact_id');
  });

  test('input schema accepts optional reason', () => {
    expect(pinToolDefinition.inputSchema.properties).toHaveProperty('reason');
    expect(pinToolDefinition.inputSchema.properties.reason.type).toBe('string');
    expect(pinToolDefinition.inputSchema.required).not.toContain('reason');
  });

  test('annotations mark it as idempotent non-destructive', () => {
    expect(pinToolDefinition.annotations).toBeDefined();
    expect(pinToolDefinition.annotations.readOnlyHint).toBe(false);
    expect(pinToolDefinition.annotations.destructiveHint).toBe(false);
    expect(pinToolDefinition.annotations.idempotentHint).toBe(true);
  });
});

describe('unpinToolDefinition', () => {
  test('has correct name', () => {
    expect(unpinToolDefinition.name).toBe('totalreclaw_unpin');
  });

  test('input schema has required fact_id', () => {
    expect(unpinToolDefinition.inputSchema.type).toBe('object');
    expect(unpinToolDefinition.inputSchema.properties).toHaveProperty('fact_id');
    expect(unpinToolDefinition.inputSchema.required).toContain('fact_id');
  });

  test('annotations', () => {
    expect(unpinToolDefinition.annotations.idempotentHint).toBe(true);
    expect(unpinToolDefinition.annotations.destructiveHint).toBe(false);
  });
});

// ─── executePinOperation (pure core logic, dep-injected) ─────────────────────

/** Build a canonical Claim blob with a given status for fixtures. */
function buildFixtureClaim(status: 'a' | 'p' | 's' | 'r' | 'c'): string {
  const obj: Record<string, unknown> = {
    t: 'prefers coffee over tea',
    c: 'pref',
    cf: 0.9,
    i: 7,
    sa: 'mcp-server',
    ea: '2026-04-12T10:00:00Z',
  };
  if (status !== 'a') obj.st = status;
  return JSON.stringify(obj);
}

interface FakeFact {
  id: string;
  encryptedBlob: string;
  encryptedEmbedding: string | null;
  decayScore: string;
  timestamp: string;
  isActive: boolean;
}

function makeDeps(overrides: Partial<PinOpDeps> = {}): PinOpDeps & { _submitted: Buffer[][] } {
  const submitted: Buffer[][] = [];
  const base: PinOpDeps = {
    owner: '0x1234567890abcdef1234567890abcdef12345678',
    sourceAgent: 'mcp-server',
    async fetchFactById(_factId: string) {
      return null;
    },
    decryptBlob(encryptedBlob: string) {
      return encryptedBlob; // identity for test
    },
    encryptBlob(plaintext: string) {
      return Buffer.from(plaintext, 'utf-8').toString('hex');
    },
    async submitBatch(payloads: Buffer[]) {
      submitted.push(payloads);
      return { txHash: '0xdeadbeef', success: true };
    },
    async generateIndices(_text: string, _entityNames: string[]) {
      return { blindIndices: ['mock_trapdoor_1', 'mock_trapdoor_2'], encryptedEmbedding: 'mock_enc_embed' };
    },
  };
  const merged = { ...base, ...overrides } as PinOpDeps & { _submitted: Buffer[][] };
  merged._submitted = submitted;
  return merged;
}

function makeFact(id: string, plaintextBlob: string): FakeFact {
  return {
    id,
    encryptedBlob: plaintextBlob, // identity decrypt in test
    encryptedEmbedding: null,
    decayScore: '0.8',
    timestamp: '1700000000',
    isActive: true,
  };
}

describe('executePinOperation — pin', () => {
  test('pins an active claim: tombstone old + write new with status=pinned', async () => {
    const activeBlob = buildFixtureClaim('a');
    const deps = makeDeps({
      async fetchFactById(id) {
        return makeFact(id, activeBlob);
      },
    });

    const result = await executePinOperation('old-uuid-1', 'pinned', deps);

    expect(result.success).toBe(true);
    expect(result.previous_status).toBe('active');
    expect(result.new_status).toBe('pinned');
    expect(result.fact_id).toBe('old-uuid-1');
    expect(result.new_fact_id).toBeDefined();
    expect(result.new_fact_id).not.toBe('old-uuid-1');
    expect(result.tx_hash).toBe('0xdeadbeef');

    // Must have submitted exactly 2 payloads: tombstone + new
    expect(deps._submitted).toHaveLength(1);
    expect(deps._submitted[0]).toHaveLength(2);
  });

  test('pinning an already-pinned claim is a no-op (idempotent, no chain write)', async () => {
    const pinnedBlob = buildFixtureClaim('p');
    const deps = makeDeps({
      async fetchFactById(id) {
        return makeFact(id, pinnedBlob);
      },
    });

    const result = await executePinOperation('already-pinned-id', 'pinned', deps);

    expect(result.success).toBe(true);
    expect(result.previous_status).toBe('pinned');
    expect(result.new_status).toBe('pinned');
    expect(result.idempotent).toBe(true);
    // No chain write
    expect(deps._submitted).toHaveLength(0);
  });

  test('returns error when fact is not found', async () => {
    const deps = makeDeps({
      async fetchFactById() {
        return null;
      },
    });

    const result = await executePinOperation('missing-id', 'pinned', deps);

    expect(result.success).toBe(false);
    expect(result.error).toBeDefined();
    expect(result.error).toMatch(/not found/i);
    expect(deps._submitted).toHaveLength(0);
  });

  test('new blob on-chain is valid v1.1 MemoryClaim with pin_status=pinned', async () => {
    const activeBlob = buildFixtureClaim('a');
    let capturedNewBlobPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) {
        return makeFact(id, activeBlob);
      },
      encryptBlob(plaintext: string) {
        capturedNewBlobPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });

    await executePinOperation('old-id', 'pinned', deps);

    expect(capturedNewBlobPlaintext).not.toBeNull();
    const parsed = JSON.parse(capturedNewBlobPlaintext!);
    // v1.1 canonical fields — long-form, schema_version 1.0, pin_status.
    expect(parsed.text).toBe('prefers coffee over tea');
    expect(parsed.type).toBe('preference');
    expect(parsed.schema_version).toBe('1.0');
    expect(parsed.pin_status).toBe('pinned');
    expect(parsed.superseded_by).toBe('old-id');
    // v0 short-key fields MUST NOT leak.
    expect(parsed.t).toBeUndefined();
    expect(parsed.c).toBeUndefined();
    expect(parsed.st).toBeUndefined();
    expect(parsed.sup).toBeUndefined();
  });

  test('pin works on a legacy {text, metadata} blob', async () => {
    const legacyBlob = JSON.stringify({
      text: 'lives in Lisbon',
      metadata: { type: 'fact', importance: 0.8, source: 'mcp_remember' },
    });
    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) {
        return makeFact(id, legacyBlob);
      },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });

    const result = await executePinOperation('legacy-id', 'pinned', deps);

    expect(result.success).toBe(true);
    expect(result.previous_status).toBe('active');
    expect(result.new_status).toBe('pinned');
    // v1.1 output: legacy blob is UPGRADED to v1.
    const parsed = JSON.parse(capturedPlaintext!);
    expect(parsed.text).toBe('lives in Lisbon');
    expect(parsed.schema_version).toBe('1.0');
    expect(parsed.pin_status).toBe('pinned');
    expect(parsed.superseded_by).toBe('legacy-id');
    // v0 legacy type "fact" → v1 type "claim".
    expect(parsed.type).toBe('claim');
  });

  test('stores reason in metadata when provided (does not affect canonical blob)', async () => {
    const activeBlob = buildFixtureClaim('a');
    const deps = makeDeps({
      async fetchFactById(id) {
        return makeFact(id, activeBlob);
      },
    });

    const result = await executePinOperation('id-1', 'pinned', deps, 'I still use Vim');

    expect(result.success).toBe(true);
    expect(result.reason).toBe('I still use Vim');
  });
});

describe('executePinOperation — unpin', () => {
  test('unpins a pinned claim', async () => {
    const pinnedBlob = buildFixtureClaim('p');
    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) {
        return makeFact(id, pinnedBlob);
      },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });

    const result = await executePinOperation('pinned-id', 'active', deps);

    expect(result.success).toBe(true);
    expect(result.previous_status).toBe('pinned');
    expect(result.new_status).toBe('active');
    expect(result.new_fact_id).toBeDefined();
    expect(result.new_fact_id).not.toBe('pinned-id');

    // v1.1 unpin: explicit pin_status=unpinned, superseded_by on v1 field.
    expect(capturedPlaintext).not.toBeNull();
    const parsed = JSON.parse(capturedPlaintext!);
    expect(parsed.schema_version).toBe('1.0');
    expect(parsed.pin_status).toBe('unpinned');
    expect(parsed.superseded_by).toBe('pinned-id');
    // v0 short-key fields MUST NOT leak.
    expect(parsed.st).toBeUndefined();
    expect(parsed.sup).toBeUndefined();

    // Submitted 2 payloads
    expect(deps._submitted[0]).toHaveLength(2);
  });

  test('unpinning a non-pinned claim is idempotent (no-op, no chain write)', async () => {
    const activeBlob = buildFixtureClaim('a');
    const deps = makeDeps({
      async fetchFactById(id) {
        return makeFact(id, activeBlob);
      },
    });

    const result = await executePinOperation('active-id', 'active', deps);

    expect(result.success).toBe(true);
    expect(result.previous_status).toBe('active');
    expect(result.new_status).toBe('active');
    expect(result.idempotent).toBe(true);
    expect(deps._submitted).toHaveLength(0);
  });

  test('returns error when fact is not found', async () => {
    const deps = makeDeps({
      async fetchFactById() {
        return null;
      },
    });

    const result = await executePinOperation('missing', 'active', deps);

    expect(result.success).toBe(false);
    expect(result.error).toMatch(/not found/i);
  });
});

// ─── handlePin / handleUnpin — top-level handler input validation ─────────────

describe('handlePin — input validation (HTTP mode, not supported)', () => {
  test('rejects missing fact_id', async () => {
    const result = await handlePin({});
    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(false);
    expect(parsed.error).toMatch(/fact_id/i);
  });

  test('rejects empty string fact_id', async () => {
    const result = await handlePin({ fact_id: '' });
    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(false);
    expect(parsed.error).toMatch(/fact_id/i);
  });

  test('rejects non-string fact_id', async () => {
    const result = await handlePin({ fact_id: 42 });
    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(false);
    expect(parsed.error).toMatch(/fact_id/i);
  });

  test('returns managed-service-only error for valid input in HTTP mode', async () => {
    const result = await handlePin({ fact_id: 'some-id' });
    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(false);
    expect(parsed.error).toMatch(/managed service|self-hosted/i);
  });
});

// ─── Parity sanity: built claim canonicalizes ─────────────────────────────────

describe('canonical claim round-trip sanity', () => {
  test('buildCanonicalClaim still works (parity sanity)', () => {
    const c = buildCanonicalClaim({
      fact: { text: 'hello', type: 'fact' },
      importance: 5,
      sourceAgent: 'mcp-server',
      extractedAt: '2026-04-12T10:00:00Z',
    });
    const parsed = JSON.parse(c);
    expect(parsed.t).toBe('hello');
    expect(parsed.c).toBe('fact');
    expect(parsed.st).toBeUndefined();
  });
});

// ─── Slice 2f: feedback-log wiring ────────────────────────────────────────────

function mkComponents(weighted: number): ScoreComponents {
  return {
    confidence: 0.85,
    corroboration: 1.0,
    recency: 0.5,
    validation: 0.7,
    weighted_total: weighted,
  };
}

function seedDecisionRow(entry: DecisionLogEntry): void {
  fs.mkdirSync(path.dirname(decisionsLogPath()), { recursive: true });
  const existing = fs.existsSync(decisionsLogPath())
    ? fs.readFileSync(decisionsLogPath(), 'utf-8')
    : '';
  fs.writeFileSync(decisionsLogPath(), existing + JSON.stringify(entry) + '\n', 'utf-8');
}

function clearLogs(): void {
  try { fs.rmSync(decisionsLogPath(), { force: true }); } catch { /* ignore */ }
  try { fs.rmSync(feedbackLogPath(), { force: true }); } catch { /* ignore */ }
}

describe('Slice 2f: decision-log lookup helpers', () => {
  test('findDecisionForPin returns null on empty log', () => {
    expect(findDecisionForPin('any-id', 'loser', '')).toBeNull();
  });

  test('findDecisionForPin matches loser role', () => {
    const entry: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner-id',
      existing_claim_id: 'loser-id',
      similarity: 0.5,
      action: 'supersede_existing',
      reason: 'new_wins',
      winner_score: 0.83,
      loser_score: 0.73,
      winner_components: mkComponents(0.83),
      loser_components: mkComponents(0.73),
      mode: 'active',
    };
    const log = JSON.stringify(entry) + '\n';
    const found = findDecisionForPin('loser-id', 'loser', log);
    expect(found).not.toBeNull();
    expect(found!.existing_claim_id).toBe('loser-id');
  });

  test('findDecisionForPin matches winner role', () => {
    const entry: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner-id',
      existing_claim_id: 'loser-id',
      similarity: 0.5,
      action: 'supersede_existing',
      reason: 'new_wins',
      winner_components: mkComponents(0.83),
      loser_components: mkComponents(0.73),
    };
    const log = JSON.stringify(entry) + '\n';
    const found = findDecisionForPin('winner-id', 'winner', log);
    expect(found).not.toBeNull();
    expect(found!.new_claim_id).toBe('winner-id');
  });

  test('findDecisionForPin skips rows without components', () => {
    const legacy: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner-id',
      existing_claim_id: 'loser-id',
      similarity: 0.5,
      action: 'supersede_existing',
      reason: 'new_wins',
    };
    const log = JSON.stringify(legacy) + '\n';
    expect(findDecisionForPin('loser-id', 'loser', log)).toBeNull();
  });

  test('findDecisionForPin walks backward, returns most recent match', () => {
    const older: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'old-winner',
      existing_claim_id: 'loser-id',
      similarity: 0.5,
      action: 'supersede_existing',
      winner_components: mkComponents(0.8),
      loser_components: mkComponents(0.7),
    };
    const newer: DecisionLogEntry = {
      ...older,
      ts: 1_776_100_000,
      new_claim_id: 'new-winner',
    };
    const log = JSON.stringify(older) + '\n' + JSON.stringify(newer) + '\n';
    const found = findDecisionForPin('loser-id', 'loser', log);
    expect(found!.new_claim_id).toBe('new-winner');
  });

  test('buildFeedbackFromDecision pin_loser → user_decision=pin_a', () => {
    const decision: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner-id',
      existing_claim_id: 'loser-id',
      similarity: 0.5,
      action: 'supersede_existing',
      winner_components: mkComponents(0.83),
      loser_components: mkComponents(0.73),
    };
    const fb = buildFeedbackFromDecision(decision, 'pin_loser', 1_777_000_000)!;
    expect(fb.claim_a_id).toBe('loser-id');
    expect(fb.claim_b_id).toBe('winner-id');
    expect(fb.formula_winner).toBe('b');
    expect(fb.user_decision).toBe('pin_a');
  });

  test('buildFeedbackFromDecision unpin_winner → user_decision=pin_b', () => {
    const decision: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner-id',
      existing_claim_id: 'loser-id',
      similarity: 0.5,
      action: 'supersede_existing',
      winner_components: mkComponents(0.83),
      loser_components: mkComponents(0.73),
    };
    const fb = buildFeedbackFromDecision(decision, 'unpin_winner', 1_777_000_000)!;
    expect(fb.user_decision).toBe('pin_b');
  });
});

describe('Slice 2f: executePinOperation writes feedback on override', () => {
  test('pinning a prior formula-loser writes a counterexample to feedback.jsonl', async () => {
    clearLogs();
    const canonical = buildFixtureClaim('a');

    // Seed a decision row where "old-id-1" was the formula's loser.
    seedDecisionRow({
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'vscode-winner-id',
      existing_claim_id: 'old-id-1',
      similarity: 0.5,
      action: 'supersede_existing',
      reason: 'new_wins',
      winner_score: 0.83,
      loser_score: 0.73,
      winner_components: mkComponents(0.83),
      loser_components: mkComponents(0.73),
      mode: 'active',
    });

    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, canonical); },
    });
    const result = await executePinOperation('old-id-1', 'pinned', deps);
    expect(result.success).toBe(true);

    expect(fs.existsSync(feedbackLogPath())).toBe(true);
    const content = fs.readFileSync(feedbackLogPath(), 'utf-8');
    const lines = content.split('\n').filter((l) => l.length > 0);
    expect(lines).toHaveLength(1);
    const fb = JSON.parse(lines[0]) as FeedbackEntry;
    expect(fb.claim_a_id).toBe('old-id-1');
    expect(fb.claim_b_id).toBe('vscode-winner-id');
    expect(fb.user_decision).toBe('pin_a');
    expect(fb.winner_components.weighted_total).toBe(0.83);
    expect(fb.loser_components.weighted_total).toBe(0.73);
  });

  test('voluntary pin (no matching decision row) writes no feedback', async () => {
    clearLogs();
    const canonical = buildFixtureClaim('a');
    // Seed a decision row that mentions a different fact.
    seedDecisionRow({
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'a',
      existing_claim_id: 'b',
      similarity: 0.5,
      action: 'supersede_existing',
      winner_components: mkComponents(0.83),
      loser_components: mkComponents(0.73),
    });

    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, canonical); },
    });
    const result = await executePinOperation('unrelated-id', 'pinned', deps);
    expect(result.success).toBe(true);

    const feedbackExists = fs.existsSync(feedbackLogPath());
    if (feedbackExists) {
      expect(fs.readFileSync(feedbackLogPath(), 'utf-8').trim()).toBe('');
    }
  });

  test('idempotent pin writes no feedback even with matching decision row', async () => {
    clearLogs();
    const pinned = buildFixtureClaim('p');
    seedDecisionRow({
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner',
      existing_claim_id: 'already-pinned',
      similarity: 0.5,
      action: 'supersede_existing',
      winner_components: mkComponents(0.83),
      loser_components: mkComponents(0.73),
    });
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, pinned); },
    });
    const result = await executePinOperation('already-pinned', 'pinned', deps);
    expect(result.idempotent).toBe(true);
    const feedbackExists = fs.existsSync(feedbackLogPath());
    if (feedbackExists) {
      expect(fs.readFileSync(feedbackLogPath(), 'utf-8').trim()).toBe('');
    }
  });

  test('unpinning a prior formula-winner writes feedback with user_decision=pin_b', async () => {
    clearLogs();
    const pinned = buildFixtureClaim('p');
    seedDecisionRow({
      ts: 1_776_100_000,
      entity_id: 'editor',
      new_claim_id: 'was-winner',
      existing_claim_id: 'was-loser',
      similarity: 0.5,
      action: 'supersede_existing',
      winner_components: mkComponents(0.83),
      loser_components: mkComponents(0.73),
    });
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, pinned); },
    });
    const result = await executePinOperation('was-winner', 'active', deps);
    expect(result.success).toBe(true);
    const content = fs.readFileSync(feedbackLogPath(), 'utf-8');
    const fb = JSON.parse(content.split('\n').filter((l) => l.length > 0)[0]) as FeedbackEntry;
    expect(fb.user_decision).toBe('pin_b');
    expect(fb.claim_b_id).toBe('was-winner');
  });
});

// ─── v1.1 pin_status end-to-end ──────────────────────────────────────────────

describe('executePinOperation — v1.1 pin_status', () => {
  function buildV1Blob(overrides: Partial<Record<string, unknown>> = {}): string {
    const base = {
      id: '01900000-0000-7000-8000-000000000099',
      text: 'prefers PostgreSQL over MongoDB for transactional workloads',
      type: 'preference',
      source: 'user',
      created_at: '2026-04-19T10:00:00.000Z',
      schema_version: '1.0',
      scope: 'work',
      volatility: 'stable',
      importance: 9,
      confidence: 0.95,
    };
    return JSON.stringify({ ...base, ...overrides });
  }

  test('pin on v1 blob preserves all v1 fields + sets pin_status=pinned', async () => {
    const v1Blob = buildV1Blob({
      reasoning: 'ACID guarantees matter for OrbitLedger',
    });
    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, v1Blob); },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });
    const result = await executePinOperation('v1-src', 'pinned', deps);
    expect(result.success).toBe(true);
    const parsed = JSON.parse(capturedPlaintext!);
    expect(parsed.schema_version).toBe('1.0');
    expect(parsed.pin_status).toBe('pinned');
    expect(parsed.type).toBe('preference');
    expect(parsed.source).toBe('user');
    expect(parsed.scope).toBe('work');
    expect(parsed.volatility).toBe('stable');
    expect(parsed.reasoning).toContain('ACID');
    expect(parsed.superseded_by).toBe('v1-src');
    expect(parsed.t).toBeUndefined();
    expect(parsed.st).toBeUndefined();
  });

  test('unpin on v1 pinned blob → pin_status=unpinned', async () => {
    const v1PinnedBlob = buildV1Blob({
      type: 'directive',
      pin_status: 'pinned',
    });
    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, v1PinnedBlob); },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });
    const result = await executePinOperation('v1-pinned', 'active', deps);
    expect(result.success).toBe(true);
    expect(result.previous_status).toBe('pinned');
    expect(result.new_status).toBe('active');
    const parsed = JSON.parse(capturedPlaintext!);
    expect(parsed.pin_status).toBe('unpinned');
    expect(parsed.type).toBe('directive');
    expect(parsed.superseded_by).toBe('v1-pinned');
  });

  test('idempotent pin on v1.1 pinned blob → no on-chain write', async () => {
    const v1PinnedBlob = buildV1Blob({ pin_status: 'pinned' });
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, v1PinnedBlob); },
    });
    const result = await executePinOperation('already-pinned-v1', 'pinned', deps);
    expect(result.success).toBe(true);
    expect(result.idempotent).toBe(true);
    expect(deps._submitted).toHaveLength(0);
  });

  test('cross-impl parity: canonical field shape matches plugin output', async () => {
    const v1Source = buildV1Blob({
      // Minimal shape used by the plugin parity test.
      text: 'cross-client parity test',
      type: 'claim',
      source: 'user',
      scope: undefined,
      volatility: undefined,
      importance: undefined,
      confidence: undefined,
    });
    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, v1Source); },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });
    await executePinOperation('parity-src', 'pinned', deps);
    const parsed = JSON.parse(capturedPlaintext!);
    // Required v1 fields present.
    expect(typeof parsed.id).toBe('string');
    expect(typeof parsed.text).toBe('string');
    expect(typeof parsed.type).toBe('string');
    expect(typeof parsed.source).toBe('string');
    expect(typeof parsed.created_at).toBe('string');
    expect(parsed.schema_version).toBe('1.0');
    expect(parsed.pin_status).toBe('pinned');
    expect(parsed.superseded_by).toBe('parity-src');
    // No v0 leak.
    expect(parsed.t).toBeUndefined();
    expect(parsed.c).toBeUndefined();
    expect(parsed.st).toBeUndefined();
    expect(parsed.sup).toBeUndefined();
  });

  test('v1 source with entities round-trips entities on pin', async () => {
    const v1WithEntities = buildV1Blob({
      entities: [
        { name: 'PostgreSQL', type: 'tool', role: 'chosen' },
        { name: 'OrbitLedger', type: 'company' },
      ],
    });
    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, v1WithEntities); },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });
    await executePinOperation('v1-entities', 'pinned', deps);
    const parsed = JSON.parse(capturedPlaintext!);
    expect(Array.isArray(parsed.entities)).toBe(true);
    expect(parsed.entities).toHaveLength(2);
    expect(parsed.entities[0].name).toBe('PostgreSQL');
    expect(parsed.entities[0].type).toBe('tool');
    expect(parsed.entities[0].role).toBe('chosen');
  });
});
