/**
 * #789 — on-chain decayScore: 0 = tombstone, 1.0 = live, no importance.
 * Every live write goes through liveDecayScore(); tombstones stay at 0.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ACTIVE_DECAY_THRESHOLD, LIVE_DECAY_SCORE, liveDecayScore } from '../src/subgraph/decay.js';
import { encodeFactProtobufKeyed, type FactPayload } from '../src/subgraph/store.js';

const SRC = (f: string) => readFileSync(join(__dirname, '..', 'src', f), 'utf8');
const INDEX_SRC = SRC('index.ts');
const FIXTURE = JSON.parse(
  readFileSync(join(__dirname, '..', '..', 'tests', 'parity', 'fixtures', 'decay-score-v1.json'), 'utf8'),
);

describe('threshold matches the subgraph', () => {
  it('subgraph/src/mapping.ts INACTIVE_THRESHOLD is still 0.3 and compares with ge', () => {
    const mapping = readFileSync(join(__dirname, '..', '..', 'subgraph', 'src', 'mapping.ts'), 'utf8');
    const m = mapping.match(/INACTIVE_THRESHOLD\s*=\s*BigDecimal\.fromString\("([0-9.]+)"\)/);
    expect(m).not.toBeNull();
    expect(Number(m![1])).toBe(ACTIVE_DECAY_THRESHOLD);
    expect(mapping).toContain('decoded.decayScore.ge(INACTIVE_THRESHOLD)');
    expect(ACTIVE_DECAY_THRESHOLD).toBe(FIXTURE.subgraph_threshold);
    expect(liveDecayScore()).toBeGreaterThanOrEqual(ACTIVE_DECAY_THRESHOLD);
  });
});

describe('liveDecayScore', () => {
  it('is the fixture constant 1.0', () => {
    expect(liveDecayScore()).toBe(FIXTURE.live);
    expect(LIVE_DECAY_SCORE).toBe(1.0);
  });
});

describe('write sites', () => {
  it('remember, crystal, debrief items all use liveDecayScore() through buildFactPayload (internal#573)', () => {
    // The three per-item assemblies now share one builder; index.ts keeps the dedup candidate.
    expect((SRC('subgraph/keyed-tags.ts').match(/decayScore: liveDecayScore\(\)/g) ?? []).length).toBe(1);
    expect((INDEX_SRC.match(/buildFactPayload\(/g) ?? []).length).toBe(3);
    expect((INDEX_SRC.match(/decayScore: liveDecayScore\(\)/g) ?? []).length).toBeGreaterThanOrEqual(1);
    expect(INDEX_SRC).not.toMatch(/decayScore:\s*[^,\n]*(\/\s*10\b|\*\s*0\.1\b)/);
  });
  it.each(['index.ts', 'tools/pin.ts', 'tools/retype.ts'])('%s has no importance-derived decayScore', (f) => {
    expect(SRC(f)).not.toMatch(/decayScore:\s*[^,\n]*(\/\s*10\b|\*\s*0\.1\b)/);
  });
  it('retype/set_scope successor writes the live value, not 0.85', () => {
    const src = SRC('tools/retype.ts');
    expect(src).toContain('decayScore: liveDecayScore()');
    expect(src).not.toMatch(/decayScore:\s*0\.85/);
  });
  it('tombstone writes in index.ts keep decayScore 0', () => {
    const lines = INDEX_SRC.split('\n');
    const tomb = lines.map((l, i) => (l.includes('encryptedBlob: TOMBSTONE_BLOB_HEX') ? i : -1)).filter((i) => i >= 0);
    expect(tomb.length).toBeGreaterThan(0);
    for (const i of tomb) {
      expect(lines.slice(i, i + 4).join('\n')).toMatch(/decayScore:\s*0,/);
    }
  });
});

describe('readers take importance from the blob', () => {
  it('recall does not derive importance from decayScore', () => {
    expect(INDEX_SRC).not.toMatch(/importance:\s*parseFloat\(c\.decayScore\)/);
    expect(INDEX_SRC).not.toMatch(/Math\.round\(\(r\.importance \?\? 0\.5\) \* 10\)/);
    expect(INDEX_SRC).toContain('importance: doc.importance, // blob importance');
  });
});

describe('payload encoding', () => {
  const base: FactPayload = {
    id: 'x', timestamp: '2026-01-01T00:00:00.000Z', owner: '0x' + '1'.repeat(40),
    encryptedBlob: '00', blindIndices: [], decayScore: 1.0, source: 's', contentFp: '', agentId: 'a',
  };
  it('live payload encodes 1.0', () => {
    const a = Buffer.from(encodeFactProtobufKeyed({ ...base, decayScore: liveDecayScore() })).toString('hex');
    expect(a).toBe(Buffer.from(encodeFactProtobufKeyed(base)).toString('hex'));
  });
  it('a decayScore 0 payload (tombstone) is encoded with 0, not the live value', () => {
    const hex = Buffer.from(encodeFactProtobufKeyed({ ...base, decayScore: 0 })).toString('hex');
    expect(hex).not.toBe(Buffer.from(encodeFactProtobufKeyed(base)).toString('hex'));
  });
});
