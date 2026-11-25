/**
 * #789 (interim) — live-fact decayScore is floored at the subgraph's active
 * threshold (0.3); tombstones stay at 0.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ACTIVE_DECAY_THRESHOLD, liveDecayScore } from '../src/subgraph/decay.js';
import { encodeFactProtobuf, type FactPayload } from '../src/subgraph/store.js';

const INDEX_SRC = readFileSync(join(__dirname, '..', 'src', 'index.ts'), 'utf8');

describe('ACTIVE_DECAY_THRESHOLD matches the subgraph', () => {
  it('subgraph/src/mapping.ts INACTIVE_THRESHOLD is still 0.3', () => {
    const mapping = readFileSync(join(__dirname, '..', '..', 'subgraph', 'src', 'mapping.ts'), 'utf8');
    const m = mapping.match(/INACTIVE_THRESHOLD\s*=\s*BigDecimal\.fromString\("([0-9.]+)"\)/);
    expect(m).not.toBeNull();
    expect(Number(m![1])).toBe(ACTIVE_DECAY_THRESHOLD);
    expect(ACTIVE_DECAY_THRESHOLD).toBe(0.3);
  });
});

describe('liveDecayScore', () => {
  it.each([1, 2, 0, -5, 2.5, NaN, Infinity, -Infinity])('importance %p stays visible', (imp) => {
    const d = liveDecayScore(imp);
    expect(d).toBeGreaterThanOrEqual(0.3);
    expect(d).toBeLessThanOrEqual(1.0);
  });
  it('importance 2 and crystal importance 2 -> 0.3', () => {
    expect(liveDecayScore(2)).toBe(0.3);
  });
  it('non-finite -> 5 -> 0.5', () => {
    expect(liveDecayScore(NaN)).toBe(0.5);
  });
  it.each([[3, 0.3], [5, 0.5], [7, 0.7], [8, 0.8], [10, 1.0]])('importance %p unchanged -> %p', (imp, want) => {
    expect(liveDecayScore(imp)).toBe(want);
  });
});

describe('write sites in index.ts', () => {
  it('every live decayScore goes through liveDecayScore (explicit remember, crystal, debrief items)', () => {
    expect(INDEX_SRC).toContain('decayScore: liveDecayScore(effectiveImportance)');
    expect(INDEX_SRC).toContain('decayScore: liveDecayScore(importance)');
    expect(INDEX_SRC).toContain('decayScore: liveDecayScore(item.importance)');
    expect(INDEX_SRC).not.toMatch(/decayScore:\s*[\w.]*[iI]mportance\s*\/\s*10/);
  });
  it('tombstone writes in index.ts keep decayScore 0 and never use the floor', () => {
    const lines = INDEX_SRC.split('\n');
    const tomb = lines.map((l, i) => (l.includes('encryptedBlob: TOMBSTONE_BLOB_HEX') ? i : -1)).filter((i) => i >= 0);
    expect(tomb.length).toBeGreaterThan(0);
    for (const i of tomb) {
      const near = lines.slice(i, i + 4).join('\n');
      expect(near).toMatch(/decayScore:\s*0,/);
    }
  });
});

describe('tombstone payload encoding', () => {
  it('a decayScore 0 payload is encoded with 0, not floored', () => {
    const p: FactPayload = {
      id: 'x', timestamp: '2026-01-01T00:00:00.000Z', owner: '0x' + '1'.repeat(40),
      encryptedBlob: '00', blindIndices: [], decayScore: 0, source: 's', contentFp: '', agentId: 'a',
    };
    const hex = Buffer.from(encodeFactProtobuf(p)).toString('hex');
    const floored = Buffer.from(encodeFactProtobuf({ ...p, decayScore: 0.3 })).toString('hex');
    expect(hex).not.toBe(floored);
  });
});
