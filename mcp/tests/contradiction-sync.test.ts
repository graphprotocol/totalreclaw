/**
 * Tests for Phase 2 Slice 2d: contradiction detection + auto-resolution in
 * the MCP server write path.
 *
 * Ported from `skill/plugin/contradiction-sync.test.ts` — same v0 short-key
 * claim shape, same fake encrypt/decrypt, same WASM core. Cross-client parity
 * means MCP and plugin produce identical outcomes given identical inputs.
 *
 * The WASM core (@totalreclaw/core) is NOT mocked. Tests run against the live
 * bindings shipped with the MCP package — same @totalreclaw/core ^2.0.0 that
 * ships in production. The subgraph + decisions.jsonl I/O are stubbed.
 *
 * Run with:
 *   npx jest tests/contradiction-sync.test.ts --no-cache
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// eslint-disable-next-line @typescript-eslint/no-require-imports
const {
  appendDecisionLog,
  CONTRADICTION_CANDIDATE_CAP,
  decisionsLogPath,
  detectAndResolveContradictions,
  parseCandidateClaim,
  resolveWithCore,
  TIE_ZONE_SCORE_TOLERANCE,
  isPinnedClaim,
} = require('../src/contradiction-sync') as typeof import('../src/contradiction-sync');

// eslint-disable-next-line @typescript-eslint/no-require-imports
const { computeEntityTrapdoor } = require('../src/claims-helper') as typeof import('../src/claims-helper');

import type {
  CandidateClaim,
  CanonicalClaim,
  DecisionLogEntry,
  ResolutionDecision,
} from '../src/contradiction-sync';

// ---------------------------------------------------------------------------
// Shared helpers — mirror plugin's test harness exactly so diffs are easy to
// diagnose across packages.
// ---------------------------------------------------------------------------

function setupTempStateDir(label: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `mcp-contradiction-${label}-`));
  process.env.TOTALRECLAW_STATE_DIR = dir;
  return dir;
}

function cleanupTempStateDir(dir: string): void {
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
  delete process.env.TOTALRECLAW_STATE_DIR;
}

const silentLogger = {
  info: () => undefined,
  warn: () => undefined,
};

function makeClaim(fields: Partial<CanonicalClaim> & { t: string; c?: string }): CanonicalClaim {
  return {
    t: fields.t,
    c: fields.c ?? 'pref',
    cf: 0.9,
    i: 7,
    sa: 'auto-extraction',
    ea: '2026-04-01T00:00:00Z',
    e: [{ n: 'editor', tp: 'concept' }],
    ...fields,
  };
}

function embed(values: number[], dims = 8): number[] {
  const out = new Array<number>(dims).fill(0);
  for (let i = 0; i < values.length && i < dims; i++) out[i] = values[i];
  return out;
}

function fakeEncrypt(jsonString: string): string {
  return Buffer.from(jsonString, 'utf-8').toString('hex');
}

function fakeDecrypt(hex: string, _key: Buffer): string {
  const clean = hex.startsWith('0x') ? hex.slice(2) : hex;
  return Buffer.from(clean, 'hex').toString('utf-8');
}

type FakeRow = {
  id: string;
  encryptedBlob: string;
  encryptedEmbedding?: string | null;
  isActive?: boolean;
};

function fakeSubgraph(byTrapdoor: Record<string, FakeRow[]>) {
  return async (
    _owner: string,
    trapdoors: string[],
    maxCandidates: number,
    _authKeyHex?: string,
  ): Promise<FakeRow[]> => {
    const seen = new Set<string>();
    const out: FakeRow[] = [];
    for (const t of trapdoors) {
      const rows = byTrapdoor[t] ?? [];
      for (const r of rows) {
        if (seen.has(r.id)) continue;
        seen.add(r.id);
        out.push(r);
        if (out.length >= maxCandidates) return out;
      }
    }
    return out;
  };
}

// ---------------------------------------------------------------------------
// parseCandidateClaim — same surface as plugin, v0 short-key only
// ---------------------------------------------------------------------------

describe('parseCandidateClaim', () => {
  it('accepts canonical v0 claim shape', () => {
    const claim = makeClaim({ t: 'hello' });
    const parsed = parseCandidateClaim(JSON.stringify(claim));
    expect(parsed).not.toBeNull();
    expect(parsed?.t).toBe('hello');
  });

  it('rejects v1 long-form claims (they lack short-key `t`/`c`)', () => {
    const v1 = {
      id: 'abc',
      text: 'I use Vim',
      type: 'preference',
      source: 'user',
      created_at: '2026-04-10T00:00:00Z',
      schema_version: '1.0',
    };
    // v1 blobs are not in-band for core's Claim deserializer — the plugin
    // filters them the same way. Cross-client parity: MCP must mirror.
    expect(parseCandidateClaim(JSON.stringify(v1))).toBeNull();
  });

  it('rejects digest infra claims (category `dig`)', () => {
    const digest = { ...makeClaim({ t: 'digest', c: 'dig' }) };
    expect(parseCandidateClaim(JSON.stringify(digest))).toBeNull();
  });

  it('rejects entity-rollup claims (category `ent`)', () => {
    const ent = { ...makeClaim({ t: 'ent', c: 'ent' }) };
    expect(parseCandidateClaim(JSON.stringify(ent))).toBeNull();
  });

  it('rejects malformed JSON', () => {
    expect(parseCandidateClaim('not json')).toBeNull();
  });

  it('rejects empty blob (digest tombstone)', () => {
    // Digest tombstones round-trip as empty strings; make sure we skip them.
    expect(parseCandidateClaim('')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// isPinnedClaim — MCP delegates to core WASM
// ---------------------------------------------------------------------------

describe('isPinnedClaim', () => {
  it('returns true for claim with st=p', () => {
    const pinned = { ...makeClaim({ t: 'pinned' }), st: 'p' };
    expect(isPinnedClaim(pinned)).toBe(true);
  });

  it('returns false for active claim (no st field)', () => {
    expect(isPinnedClaim(makeClaim({ t: 'active' }))).toBe(false);
  });

  it('returns false for explicitly active claim (st=a)', () => {
    const active = { ...makeClaim({ t: 'active' }), st: 'a' };
    expect(isPinnedClaim(active)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// appendDecisionLog — roundtrips the jsonl format the plugin + MCP share
// ---------------------------------------------------------------------------

describe('appendDecisionLog', () => {
  let tmp: string;
  beforeEach(() => {
    tmp = setupTempStateDir('append');
  });
  afterEach(() => {
    cleanupTempStateDir(tmp);
  });

  it('writes a single jsonl line and round-trips it', async () => {
    const entry: DecisionLogEntry = {
      ts: 1_777_000_000,
      entity_id: 'deadbeef12345678',
      new_claim_id: 'new-1',
      existing_claim_id: '0xold',
      similarity: 0.7,
      action: 'supersede_existing',
      reason: 'new_wins',
      winner_score: 0.85,
      loser_score: 0.55,
      mode: 'active',
    };
    await appendDecisionLog(entry);
    const content = fs.readFileSync(decisionsLogPath(), 'utf-8');
    const lines = content.split('\n').filter((l: string) => l.length > 0);
    expect(lines.length).toBe(1);
    const parsed = JSON.parse(lines[0]);
    expect(parsed.entity_id).toBe('deadbeef12345678');
    expect(parsed.action).toBe('supersede_existing');
    expect(parsed.reason).toBe('new_wins');
    expect(parsed.mode).toBe('active');
  });

  it('appends a second line without overwriting the first', async () => {
    const entry: DecisionLogEntry = {
      ts: 1_777_000_000,
      entity_id: 'a',
      new_claim_id: 'n1',
      existing_claim_id: 'e1',
      similarity: 0.5,
      action: 'skip_new',
      reason: 'existing_wins',
      mode: 'active',
    };
    await appendDecisionLog(entry);
    await appendDecisionLog({ ...entry, existing_claim_id: 'e2' });
    const content = fs.readFileSync(decisionsLogPath(), 'utf-8');
    const lines = content.split('\n').filter((l: string) => l.length > 0);
    expect(lines.length).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// resolveWithCore — pure resolver, no file I/O
// ---------------------------------------------------------------------------

describe('resolveWithCore', () => {
  it('mode=off returns [] immediately', () => {
    const decisions = resolveWithCore({
      newClaim: makeClaim({ t: 'new' }),
      newClaimId: 'n1',
      newEmbedding: embed([1, 0, 0], 8),
      candidates: [
        {
          claim: makeClaim({ t: 'old' }),
          id: 'e1',
          embedding: embed([1, 0, 0], 8),
        },
      ],
      weightsJson: '{}',
      thresholdLower: 0.3,
      thresholdUpper: 0.85,
      nowUnixSeconds: 1_777_000_000,
      mode: 'off',
      logger: silentLogger,
    });
    expect(decisions).toEqual([]);
  });

  it('empty candidates returns []', () => {
    const decisions = resolveWithCore({
      newClaim: makeClaim({ t: 'new' }),
      newClaimId: 'n1',
      newEmbedding: embed([1, 0, 0], 8),
      candidates: [],
      weightsJson: '{}',
      thresholdLower: 0.3,
      thresholdUpper: 0.85,
      nowUnixSeconds: 1_777_000_000,
      mode: 'active',
      logger: silentLogger,
    });
    expect(decisions).toEqual([]);
  });

  it('empty new embedding returns []', () => {
    const decisions = resolveWithCore({
      newClaim: makeClaim({ t: 'new' }),
      newClaimId: 'n1',
      newEmbedding: [],
      candidates: [
        {
          claim: makeClaim({ t: 'old' }),
          id: 'e1',
          embedding: embed([1, 0, 0], 8),
        },
      ],
      weightsJson: '{}',
      thresholdLower: 0.3,
      thresholdUpper: 0.85,
      nowUnixSeconds: 1_777_000_000,
      mode: 'active',
      logger: silentLogger,
    });
    expect(decisions).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// detectAndResolveContradictions — end-to-end with real WASM
//
// These exercise the full pipeline: fetch candidates via fake subgraph,
// decrypt, parse, run core.resolveWithCandidates, write decision log.
// ---------------------------------------------------------------------------

describe('detectAndResolveContradictions (E2E)', () => {
  let tmp: string;
  const origEnv = process.env.TOTALRECLAW_AUTO_RESOLVE_MODE;

  beforeEach(() => {
    tmp = setupTempStateDir('e2e');
    delete process.env.TOTALRECLAW_AUTO_RESOLVE_MODE;
  });

  afterEach(() => {
    cleanupTempStateDir(tmp);
    if (origEnv !== undefined) {
      process.env.TOTALRECLAW_AUTO_RESOLVE_MODE = origEnv;
    } else {
      delete process.env.TOTALRECLAW_AUTO_RESOLVE_MODE;
    }
  });

  it('non-contradicting write: no decisions, proceed unchanged', async () => {
    // No candidates returned by subgraph — pristine write path.
    const search = fakeSubgraph({});
    const decisions = await detectAndResolveContradictions({
      newClaim: makeClaim({ t: 'I use VS Code' }),
      newClaimId: 'new-vscode',
      newEmbedding: embed([1, 0, 0, 0], 8),
      subgraphOwner: '0xowner',
      authKeyHex: 'authKey',
      encryptionKey: Buffer.alloc(32),
      deps: {
        searchSubgraph: search,
        decryptFromHex: fakeDecrypt,
        nowUnixSeconds: 1_777_000_000,
      },
      logger: silentLogger,
    });
    expect(decisions).toEqual([]);
    // No log written when no decisions fired.
    expect(fs.existsSync(decisionsLogPath())).toBe(false);
  });

  it('contradicting write: new wins → supersede_existing decision', async () => {
    // Stale existing claim that the new claim should beat on recency +
    // validation (new carries sa='totalreclaw_remember' = user-validated).
    const existingClaim = makeClaim({
      t: 'I use Vim as my primary editor',
      c: 'pref',
      ea: '2025-06-01T00:00:00Z',
      sa: 'auto-extraction',
    });
    const existingEmb = embed([Math.cos(Math.PI / 4), Math.sin(Math.PI / 4), 0, 0], 8);
    const trapdoor = computeEntityTrapdoor('editor');
    const search = fakeSubgraph({
      [trapdoor]: [
        {
          id: 'existing-vim',
          encryptedBlob: fakeEncrypt(JSON.stringify(existingClaim)),
          encryptedEmbedding: fakeEncrypt(JSON.stringify(existingEmb)),
          isActive: true,
        },
      ],
    });
    const newClaim = makeClaim({
      t: 'I have switched to VS Code full-time',
      c: 'pref',
      ea: '2026-04-10T00:00:00Z',
      sa: 'totalreclaw_remember',
    });
    const decisions = await detectAndResolveContradictions({
      newClaim,
      newClaimId: 'new-vscode',
      newEmbedding: embed([1, 0, 0, 0], 8),
      subgraphOwner: '0xowner',
      authKeyHex: 'authKey',
      encryptionKey: Buffer.alloc(32),
      deps: {
        searchSubgraph: search,
        decryptFromHex: fakeDecrypt,
        nowUnixSeconds: 1_777_000_000,
      },
      logger: silentLogger,
    });

    expect(decisions.length).toBe(1);
    expect(decisions[0].action).toBe('supersede_existing');
    if (decisions[0].action === 'supersede_existing') {
      expect(decisions[0].existingFactId).toBe('existing-vim');
    }

    const log = fs.readFileSync(decisionsLogPath(), 'utf-8');
    expect(log).toContain('supersede_existing');
    expect(log).toContain('new_wins');
  });

  it('pinned existing: skip_new with reason=existing_pinned (new never overrides pin)', async () => {
    const pinnedClaim = { ...makeClaim({ t: 'I use Vim' }), st: 'p' };
    const existingEmb = embed([Math.cos(Math.PI / 4), Math.sin(Math.PI / 4), 0, 0], 8);
    const trapdoor = computeEntityTrapdoor('editor');
    const search = fakeSubgraph({
      [trapdoor]: [
        {
          id: 'pinned-vim',
          encryptedBlob: fakeEncrypt(JSON.stringify(pinnedClaim)),
          encryptedEmbedding: fakeEncrypt(JSON.stringify(existingEmb)),
          isActive: true,
        },
      ],
    });
    const decisions = await detectAndResolveContradictions({
      newClaim: makeClaim({ t: 'I use VS Code now', ea: '2026-04-10T00:00:00Z' }),
      newClaimId: 'new-vscode',
      newEmbedding: embed([1, 0, 0, 0], 8),
      subgraphOwner: '0xowner',
      authKeyHex: 'authKey',
      encryptionKey: Buffer.alloc(32),
      deps: {
        searchSubgraph: search,
        decryptFromHex: fakeDecrypt,
        nowUnixSeconds: 1_777_000_000,
      },
      logger: silentLogger,
    });

    expect(decisions.length).toBe(1);
    expect(decisions[0].action).toBe('skip_new');
    if (decisions[0].action === 'skip_new') {
      expect(decisions[0].reason).toBe('existing_pinned');
      expect(decisions[0].existingFactId).toBe('pinned-vim');
    }

    // Log row reflects the pin-respecting skip.
    const log = fs.readFileSync(decisionsLogPath(), 'utf-8');
    expect(log).toContain('existing_pinned');
  });

  it('mode=off: short-circuits before any subgraph query', async () => {
    process.env.TOTALRECLAW_AUTO_RESOLVE_MODE = 'off';
    let searchCalls = 0;
    const search = (async () => {
      searchCalls++;
      return [];
    }) as unknown as Parameters<typeof detectAndResolveContradictions>[0]['deps']['searchSubgraph'];
    const decisions = await detectAndResolveContradictions({
      newClaim: makeClaim({ t: 'I use VS Code now' }),
      newClaimId: 'new-1',
      newEmbedding: embed([1, 0, 0, 0], 8),
      subgraphOwner: '0xowner',
      authKeyHex: 'authKey',
      encryptionKey: Buffer.alloc(32),
      deps: { searchSubgraph: search, decryptFromHex: fakeDecrypt, nowUnixSeconds: 1_777_000_000 },
      logger: silentLogger,
    });
    expect(decisions).toEqual([]);
    expect(searchCalls).toBe(0);
    expect(fs.existsSync(decisionsLogPath())).toBe(false);
  });

  it('no entities on new claim: returns [] without querying subgraph', async () => {
    let searchCalls = 0;
    const search = (async () => {
      searchCalls++;
      return [];
    }) as unknown as Parameters<typeof detectAndResolveContradictions>[0]['deps']['searchSubgraph'];
    const noEntityClaim = { ...makeClaim({ t: 'lone fact' }), e: [] };
    const decisions = await detectAndResolveContradictions({
      newClaim: noEntityClaim,
      newClaimId: 'new-lone',
      newEmbedding: embed([1, 0, 0, 0], 8),
      subgraphOwner: '0xowner',
      authKeyHex: 'authKey',
      encryptionKey: Buffer.alloc(32),
      deps: { searchSubgraph: search, decryptFromHex: fakeDecrypt, nowUnixSeconds: 1_777_000_000 },
      logger: silentLogger,
    });
    expect(decisions).toEqual([]);
    expect(searchCalls).toBe(0);
  });

  it('subgraph failure: swallows error, returns []', async () => {
    const search = (async () => {
      throw new Error('network boom');
    }) as unknown as Parameters<typeof detectAndResolveContradictions>[0]['deps']['searchSubgraph'];
    const decisions = await detectAndResolveContradictions({
      newClaim: makeClaim({ t: 'new' }),
      newClaimId: 'n1',
      newEmbedding: embed([1, 0, 0, 0], 8),
      subgraphOwner: '0xowner',
      authKeyHex: 'authKey',
      encryptionKey: Buffer.alloc(32),
      deps: { searchSubgraph: search, decryptFromHex: fakeDecrypt, nowUnixSeconds: 1_777_000_000 },
      logger: silentLogger,
    });
    expect(decisions).toEqual([]);
  });

  it('candidate with missing embedding: still processed but drops from WASM call', async () => {
    // Candidate missing encryptedEmbedding should be decrypted + parsed but
    // dropped in resolveWithCore before reaching WASM. No crash.
    const existingClaim = makeClaim({ t: 'existing' });
    const trapdoor = computeEntityTrapdoor('editor');
    const search = fakeSubgraph({
      [trapdoor]: [
        {
          id: 'no-embed',
          encryptedBlob: fakeEncrypt(JSON.stringify(existingClaim)),
          encryptedEmbedding: null,
          isActive: true,
        },
      ],
    });
    const decisions = await detectAndResolveContradictions({
      newClaim: makeClaim({ t: 'new' }),
      newClaimId: 'n1',
      newEmbedding: embed([1, 0, 0, 0], 8),
      subgraphOwner: '0xowner',
      authKeyHex: 'authKey',
      encryptionKey: Buffer.alloc(32),
      deps: { searchSubgraph: search, decryptFromHex: fakeDecrypt, nowUnixSeconds: 1_777_000_000 },
      logger: silentLogger,
    });
    // No embedding → dropped from WASM call → no candidates → no decisions.
    expect(decisions).toEqual([]);
  });

  it('inactive candidate: excluded from detection', async () => {
    const existingClaim = makeClaim({ t: 'inactive existing' });
    const existingEmb = embed([Math.cos(Math.PI / 4), Math.sin(Math.PI / 4), 0, 0], 8);
    const trapdoor = computeEntityTrapdoor('editor');
    const search = fakeSubgraph({
      [trapdoor]: [
        {
          id: 'inactive-row',
          encryptedBlob: fakeEncrypt(JSON.stringify(existingClaim)),
          encryptedEmbedding: fakeEncrypt(JSON.stringify(existingEmb)),
          isActive: false,
        },
      ],
    });
    const decisions = await detectAndResolveContradictions({
      newClaim: makeClaim({ t: 'new' }),
      newClaimId: 'n1',
      newEmbedding: embed([1, 0, 0, 0], 8),
      subgraphOwner: '0xowner',
      authKeyHex: 'authKey',
      encryptionKey: Buffer.alloc(32),
      deps: { searchSubgraph: search, decryptFromHex: fakeDecrypt, nowUnixSeconds: 1_777_000_000 },
      logger: silentLogger,
    });
    expect(decisions).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

describe('module constants', () => {
  it('TIE_ZONE_SCORE_TOLERANCE is 0.01 (matches plugin)', () => {
    expect(TIE_ZONE_SCORE_TOLERANCE).toBe(0.01);
  });

  it('CONTRADICTION_CANDIDATE_CAP is 20 (matches plugin)', () => {
    expect(CONTRADICTION_CANDIDATE_CAP).toBe(20);
  });
});
