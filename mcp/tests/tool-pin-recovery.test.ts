/**
 * Pin-on-tombstone recovery tests for MCP server (Phase 2.1 parity with plugin).
 *
 * Verifies the recovery path: when the MCP pin tool encounters a fact whose
 * on-chain blob is a tombstone (1-byte `0x00` — either from auto-resolved
 * supersede or cross-client forget), it reconstructs the pre-tombstone claim
 * from `decisions.jsonl` and completes the pin normally.
 *
 * These tests intentionally mirror the plugin's recovery test suite at
 * `skill/plugin/pin-unpin.test.ts:784` so the two implementations' behavior
 * stays in lockstep. The shared state dir (`~/.totalreclaw/` or the env-var
 * override) means a plugin that writes a supersede row and an MCP that reads
 * the same row produce identical pin behavior.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  executePinOperation,
  decisionsLogPath,
  feedbackLogPath,
  type DecisionLogEntry,
  type PinOpDeps,
  type ScoreComponents,
} from '../src/tools/pin';
import { findLoserClaimInDecisionLog } from '../src/decision-log-reader';

// Isolate the on-disk state dir so tests never touch ~/.totalreclaw/.
const TEST_STATE_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-pin-recovery-'));
process.env.TOTALRECLAW_STATE_DIR = TEST_STATE_DIR;

afterAll(() => {
  try { fs.rmSync(TEST_STATE_DIR, { recursive: true, force: true }); } catch { /* ignore */ }
  delete process.env.TOTALRECLAW_STATE_DIR;
});

function clearLogs(): void {
  try { fs.rmSync(decisionsLogPath(), { force: true }); } catch { /* ignore */ }
  try { fs.rmSync(feedbackLogPath(), { force: true }); } catch { /* ignore */ }
}

function mkComponents(total: number): ScoreComponents {
  return {
    confidence: total * 0.25,
    corroboration: total * 0.25,
    recency: total * 0.25,
    validation: total * 0.25,
    weighted_total: total,
  };
}

function appendDecisionLog(entry: DecisionLogEntry): void {
  fs.mkdirSync(path.dirname(decisionsLogPath()), { recursive: true });
  fs.appendFileSync(decisionsLogPath(), JSON.stringify(entry) + '\n');
}

interface FakeFact {
  id: string;
  encryptedBlob: string;
  encryptedEmbedding: string | null;
  decayScore: string;
  timestamp: string;
  isActive: boolean;
}

/**
 * Build a mock deps object. Default `decryptBlob` is identity so ordinary
 * tests work; the recovery tests override it to throw on the tombstone blob
 * (matching what real XChaCha20-Poly1305 does — "Encrypted data too short"
 * when the ciphertext is shorter than the 16-byte auth tag).
 */
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

function makeFact(id: string, encryptedBlob: string): FakeFact {
  return {
    id,
    encryptedBlob,
    encryptedEmbedding: null,
    decayScore: '0',
    timestamp: '1700000000',
    isActive: true,
  };
}

// ─── findLoserClaimInDecisionLog helper ──────────────────────────────────────

describe('findLoserClaimInDecisionLog', () => {
  beforeEach(clearLogs);

  test('returns null when decisions.jsonl is missing', () => {
    expect(findLoserClaimInDecisionLog('any-id')).toBeNull();
  });

  test('returns null when no matching row for factId', () => {
    const entry: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner',
      existing_claim_id: 'someone-else',
      similarity: 0.55,
      action: 'supersede_existing',
      loser_claim_json: JSON.stringify({ t: 'x', c: 'fact', cf: 0.9, i: 5, sa: 's', ea: '2026-01-01T00:00:00Z' }),
      mode: 'active',
    };
    appendDecisionLog(entry);
    expect(findLoserClaimInDecisionLog('target-id')).toBeNull();
  });

  test('returns null when matching row lacks loser_claim_json', () => {
    // Pre-Phase-2.1 shape — supersede row present but no loser_claim_json.
    const entry: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner',
      existing_claim_id: 'target-id',
      similarity: 0.55,
      action: 'supersede_existing',
      mode: 'active',
    };
    appendDecisionLog(entry);
    expect(findLoserClaimInDecisionLog('target-id')).toBeNull();
  });

  test('returns loser_claim_json on a matching supersede_existing row', () => {
    const loserClaim = {
      t: 'I use Vim',
      c: 'pref',
      cf: 0.9,
      i: 7,
      sa: 'auto-extraction',
      ea: '2026-01-01T00:00:00Z',
    };
    const entry: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner-id',
      existing_claim_id: 'target-id',
      similarity: 0.6,
      action: 'supersede_existing',
      loser_claim_json: JSON.stringify(loserClaim),
      mode: 'active',
    };
    appendDecisionLog(entry);
    const result = findLoserClaimInDecisionLog('target-id');
    expect(result).not.toBeNull();
    const parsed = JSON.parse(result!);
    expect(parsed.t).toBe('I use Vim');
  });

  test('returns most recent matching row when multiple exist (walks backward)', () => {
    const older: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'old-winner',
      existing_claim_id: 'target-id',
      similarity: 0.55,
      action: 'supersede_existing',
      loser_claim_json: JSON.stringify({ t: 'older loser', c: 'fact', cf: 0.8, i: 5, sa: 's', ea: '2026-01-01T00:00:00Z' }),
      mode: 'active',
    };
    const newer: DecisionLogEntry = {
      ...older,
      ts: 1_776_100_000,
      new_claim_id: 'new-winner',
      loser_claim_json: JSON.stringify({ t: 'newer loser', c: 'fact', cf: 0.85, i: 6, sa: 's', ea: '2026-01-02T00:00:00Z' }),
    };
    appendDecisionLog(older);
    appendDecisionLog(newer);
    const result = findLoserClaimInDecisionLog('target-id');
    expect(result).not.toBeNull();
    const parsed = JSON.parse(result!);
    expect(parsed.t).toBe('newer loser');
  });

  test('skips non-supersede_existing actions', () => {
    const entry: DecisionLogEntry = {
      ts: 1_776_000_000,
      entity_id: 'editor',
      new_claim_id: 'winner',
      existing_claim_id: 'target-id',
      similarity: 0.4,
      action: 'skip_new',
      loser_claim_json: JSON.stringify({ t: 'not-tombstoned', c: 'fact', cf: 0.9, i: 5, sa: 's', ea: '2026-01-01T00:00:00Z' }),
      mode: 'active',
    };
    appendDecisionLog(entry);
    expect(findLoserClaimInDecisionLog('target-id')).toBeNull();
  });
});

// ─── executePinOperation — recovery path ─────────────────────────────────────

describe('executePinOperation — pin-on-tombstone recovery', () => {
  beforeEach(clearLogs);

  test('recovers a tombstoned fact from decisions.jsonl and pins it', async () => {
    // Seed the "on-chain" fact with a 1-byte tombstone blob.
    const tombstonedFact = makeFact('tombstoned-vim', '00');

    // Seed decisions.jsonl with the pre-tombstone canonical Claim JSON —
    // this is the row the auto-resolver or MCP `forget` would have written.
    const loserClaim = {
      t: 'I use Neovim as my primary editor',
      c: 'pref',
      cf: 0.95,
      i: 8,
      sa: 'auto-extraction',
      ea: '2026-01-01T00:00:00Z',
      e: [
        { n: 'editor', tp: 'concept' },
        { n: 'Neovim', tp: 'tool' },
      ],
    };
    const decision: DecisionLogEntry = {
      ts: 1_777_000_000,
      entity_id: 'editor',
      new_claim_id: 'new-vscode-id',
      existing_claim_id: 'tombstoned-vim',
      similarity: 0.55,
      action: 'supersede_existing',
      reason: 'new_wins',
      winner_score: 0.91,
      loser_score: 0.74,
      winner_components: mkComponents(0.91),
      loser_components: mkComponents(0.74),
      loser_claim_json: JSON.stringify(loserClaim),
      mode: 'active',
    };
    appendDecisionLog(decision);

    let capturedNewPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) {
        return id === 'tombstoned-vim' ? tombstonedFact : null;
      },
      decryptBlob(hex: string) {
        // Real XChaCha20-Poly1305 throws this exact error on a 1-byte blob
        // (ciphertext shorter than the 16-byte auth tag).
        if (hex === '00' || hex === '') {
          throw new Error('Encrypted data too short');
        }
        return Buffer.from(hex.startsWith('0x') ? hex.slice(2) : hex, 'hex').toString('utf8');
      },
      encryptBlob(plaintext: string) {
        capturedNewPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });

    const result = await executePinOperation('tombstoned-vim', 'pinned', deps, 'user override');

    expect(result.success).toBe(true);
    expect(result.fact_id).toBe('tombstoned-vim');
    // Recovery always forces previous_status to 'active' so the pin proceeds
    // (not idempotent no-op). Matches plugin behavior.
    expect(result.previous_status).toBe('active');
    expect(result.new_status).toBe('pinned');
    expect(result.new_fact_id).toBeDefined();
    expect(result.new_fact_id).not.toBe('tombstoned-vim');
    expect(result.tx_hash).toBe('0xdeadbeef');

    // Exactly one batch with tombstone + new payloads.
    expect(deps._submitted).toHaveLength(1);
    expect(deps._submitted[0]).toHaveLength(2);

    // The new blob reflects the RECOVERED claim text + flipped pin_status.
    // v1.1: long-form fields, schema_version "1.0", pin_status, superseded_by.
    expect(capturedNewPlaintext).not.toBeNull();
    const parsed = JSON.parse(capturedNewPlaintext!);
    expect(parsed.text).toBe('I use Neovim as my primary editor');
    expect(parsed.schema_version).toBe('1.0');
    expect(parsed.pin_status).toBe('pinned');
    expect(parsed.superseded_by).toBe('tombstoned-vim');
  });

  test('returns error when tombstone blob has no matching decisions.jsonl row', async () => {
    // Tombstoned on-chain BUT no decision row — recovery can't help.
    const tombstonedFact = makeFact('lost-forever', '00');
    const deps = makeDeps({
      async fetchFactById(id) {
        return id === 'lost-forever' ? tombstonedFact : null;
      },
      decryptBlob(hex: string) {
        if (hex === '00' || hex === '') {
          throw new Error('Encrypted data too short');
        }
        return hex;
      },
    });

    const result = await executePinOperation('lost-forever', 'pinned', deps);

    expect(result.success).toBe(false);
    expect(result.error).toMatch(/no recovery row in decisions.jsonl/i);
    expect(deps._submitted).toHaveLength(0);
  });

  test('non-tombstone decrypt failure returns the original error (no recovery attempt)', async () => {
    // A genuinely corrupted non-tombstone blob must NOT trigger recovery —
    // returning a stale decision-log claim would silently overwrite real data.
    const corruptFact = makeFact('corrupt-id', 'deadbeef1234567890abcdef');
    const deps = makeDeps({
      async fetchFactById(id) {
        return id === 'corrupt-id' ? corruptFact : null;
      },
      decryptBlob(_hex: string) {
        throw new Error('MAC verification failed — key mismatch');
      },
    });

    // Seed a matching row to prove recovery would succeed if the guard were wrong.
    const decision: DecisionLogEntry = {
      ts: 1_777_000_000,
      entity_id: 'x',
      new_claim_id: 'y',
      existing_claim_id: 'corrupt-id',
      similarity: 0.5,
      action: 'supersede_existing',
      loser_claim_json: JSON.stringify({ t: 'should not be used', c: 'fact', cf: 0.9, i: 5, sa: 's', ea: '2026-01-01T00:00:00Z' }),
      mode: 'active',
    };
    appendDecisionLog(decision);

    const result = await executePinOperation('corrupt-id', 'pinned', deps);

    expect(result.success).toBe(false);
    expect(result.error).toMatch(/MAC verification failed/i);
    expect(deps._submitted).toHaveLength(0);
  });

  test('recovery forces a real on-chain write even if recovered claim already has st=p', async () => {
    // Plugin parity: recovered claims reset currentStatus to 'active' before
    // the idempotent-no-op check, so pinning after recovery always writes.
    const tombstonedFact = makeFact('tombstoned-pinned', '00');
    const loserClaim = {
      t: 'originally pinned claim',
      c: 'fact',
      cf: 0.9,
      i: 8,
      sa: 's',
      ea: '2026-01-01T00:00:00Z',
      st: 'p', // was pinned before tombstone
    };
    const decision: DecisionLogEntry = {
      ts: 1_777_000_000,
      entity_id: 'x',
      new_claim_id: 'y',
      existing_claim_id: 'tombstoned-pinned',
      similarity: 0.5,
      action: 'supersede_existing',
      loser_claim_json: JSON.stringify(loserClaim),
      mode: 'active',
    };
    appendDecisionLog(decision);

    const deps = makeDeps({
      async fetchFactById(id) {
        return id === 'tombstoned-pinned' ? tombstonedFact : null;
      },
      decryptBlob(hex: string) {
        if (hex === '00') throw new Error('Encrypted data too short');
        return hex;
      },
    });

    const result = await executePinOperation('tombstoned-pinned', 'pinned', deps);

    // Even though the recovered claim had st='p', the recovery path resets
    // currentStatus to 'active' so pinning proceeds as a normal write.
    expect(result.success).toBe(true);
    expect(result.idempotent).toBeUndefined();
    expect(result.previous_status).toBe('active');
    expect(result.new_status).toBe('pinned');
    expect(deps._submitted).toHaveLength(1);
  });

  test('pin → forget → re-pin parity flow (end-to-end simulation)', async () => {
    // Simulates the cross-client user journey the audit calls out:
    //   1. User pins fact F1 (written on-chain)
    //   2. User forgets F1 in another client (tombstones F1, writes decision row)
    //   3. User re-pins F1 via MCP → must recover from decision row.
    //
    // This test drives step 3 only; steps 1+2 are simulated via fixture state.
    const f1Id = 'f1-cross-client';
    const pinnedClaim = {
      t: 'My preferred editor is Neovim',
      c: 'pref',
      cf: 0.95,
      i: 9,
      sa: 'openclaw-plugin',
      ea: '2026-01-01T00:00:00Z',
      st: 'p',
    };

    // Step 2 artifact: decision row written when F1 was forgotten.
    const decision: DecisionLogEntry = {
      ts: 1_777_100_000,
      entity_id: 'editor',
      new_claim_id: 'forget-tombstone',
      existing_claim_id: f1Id,
      similarity: 1.0,
      action: 'supersede_existing',
      reason: 'new_wins',
      winner_components: mkComponents(0.95),
      loser_components: mkComponents(0.85),
      loser_claim_json: JSON.stringify(pinnedClaim),
      mode: 'active',
    };
    appendDecisionLog(decision);

    const tombstonedF1 = makeFact(f1Id, '00');
    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) {
        return id === f1Id ? tombstonedF1 : null;
      },
      decryptBlob(hex: string) {
        if (hex === '00') throw new Error('Encrypted data too short');
        return hex;
      },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });

    // Step 3: user re-pins F1 via MCP.
    const result = await executePinOperation(f1Id, 'pinned', deps, 'cross-client re-pin');

    expect(result.success).toBe(true);
    expect(result.fact_id).toBe(f1Id);
    expect(result.new_status).toBe('pinned');

    // The re-pinned fact MUST match the pre-forget claim's text + type.
    // v1.1: long-form fields + pin_status + superseded_by.
    const parsed = JSON.parse(capturedPlaintext!);
    expect(parsed.text).toBe('My preferred editor is Neovim');
    // Legacy v0 type "preference" → v1 type "preference" (identity mapping).
    expect(parsed.type).toBe('preference');
    expect(parsed.pin_status).toBe('pinned');
    expect(parsed.superseded_by).toBe(f1Id);
  });
});
