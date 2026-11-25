import fs from 'node:fs';
import path from 'node:path';
import {
  RecentWrites,
  recordSubmittedPayloads,
  TOMBSTONE_BLOB_HEX,
} from '../src/subgraph/recent-writes';

const OWNER = '0xAbC0000000000000000000000000000000000001';
const OTHER = '0x0000000000000000000000000000000000000002';

describe('RecentWrites', () => {
  test('record then get returns the ciphertext fact', () => {
    const c = new RecentWrites();
    c.recordWrite(OWNER, { id: 'a', encryptedBlob: '0xdead', decayScore: 0.8, timestamp: '2026-11-25T10:00:00Z' });
    const got = c.get(OWNER, 'a');
    expect(got).toEqual({
      id: 'a',
      encryptedBlob: 'dead',
      encryptedEmbedding: null,
      decayScore: '0.8',
      timestamp: String(Math.floor(Date.parse('2026-11-25T10:00:00Z') / 1000)),
      isActive: true,
    });
    expect(Object.keys(got!).sort()).toEqual(
      ['decayScore', 'encryptedBlob', 'encryptedEmbedding', 'id', 'isActive', 'timestamp'],
    );
  });

  test('entries expire after the TTL', () => {
    let t = 0;
    const c = new RecentWrites({ ttlMs: 1000, now: () => t });
    c.recordWrite(OWNER, { id: 'a', encryptedBlob: 'aa', decayScore: 1, timestamp: 'x' });
    c.recordTombstone(OWNER, 'b');
    t = 999;
    expect(c.get(OWNER, 'a')).not.toBeNull();
    expect(c.isTombstoned(OWNER, 'b')).toBe(true);
    t = 1001;
    expect(c.get(OWNER, 'a')).toBeNull();
    expect(c.isTombstoned(OWNER, 'b')).toBe(false);
  });

  test('max entries drops the oldest', () => {
    const c = new RecentWrites({ maxEntries: 2 });
    for (const id of ['a', 'b', 'c']) c.recordWrite(OWNER, { id, encryptedBlob: 'aa', decayScore: 1, timestamp: 'x' });
    expect(c.get(OWNER, 'a')).toBeNull();
    expect(c.get(OWNER, 'b')).not.toBeNull();
    expect(c.get(OWNER, 'c')).not.toBeNull();
  });

  test('tombstone evicts and marks; a later write clears the mark', () => {
    const c = new RecentWrites();
    c.recordWrite(OWNER, { id: 'a', encryptedBlob: 'aa', decayScore: 1, timestamp: 'x' });
    c.recordTombstone(OWNER, 'a');
    expect(c.get(OWNER, 'a')).toBeNull();
    expect(c.isTombstoned(OWNER, 'a')).toBe(true);
    c.recordWrite(OWNER, { id: 'a', encryptedBlob: 'bb', decayScore: 1, timestamp: 'x' });
    expect(c.isTombstoned(OWNER, 'a')).toBe(false);
  });

  test('owner isolation', () => {
    const c = new RecentWrites();
    c.recordWrite(OWNER, { id: 'a', encryptedBlob: 'aa', decayScore: 1, timestamp: 'x' });
    expect(c.get(OWNER.toLowerCase(), 'a')).not.toBeNull();
    expect(c.get(OTHER, 'a')).toBeNull();
  });

  test('clear empties both maps', () => {
    const c = new RecentWrites();
    c.recordWrite(OWNER, { id: 'a', encryptedBlob: 'aa', decayScore: 1, timestamp: 'x' });
    c.recordTombstone(OWNER, 'b');
    c.clear();
    expect(c.get(OWNER, 'a')).toBeNull();
    expect(c.isTombstoned(OWNER, 'b')).toBe(false);
  });

  test('recordSubmittedPayloads classifies tombstones', () => {
    const c = new RecentWrites();
    recordSubmittedPayloads(c, OWNER, [
      { id: 'old', encryptedBlob: TOMBSTONE_BLOB_HEX, decayScore: 0, timestamp: 'x' },
      { id: 'new', encryptedBlob: 'beef', decayScore: 1, timestamp: 'x' },
    ], { success: true });
    expect(c.isTombstoned(OWNER, 'old')).toBe(true);
    expect(c.get(OWNER, 'new')).not.toBeNull();
  });

  test('records nothing when the submit did not succeed', () => {
    const c = new RecentWrites();
    recordSubmittedPayloads(c, OWNER, [
      { id: 'old', encryptedBlob: TOMBSTONE_BLOB_HEX, decayScore: 0, timestamp: 'x' },
      { id: 'new', encryptedBlob: 'beef', decayScore: 1, timestamp: 'x' },
    ], { success: false });
    expect(c.isTombstoned(OWNER, 'old')).toBe(false);
    expect(c.get(OWNER, 'new')).toBeNull();
  });

  test('modules never touch the filesystem', () => {
    for (const f of ['src/subgraph/recent-writes.ts', 'src/subgraph/fact-lookup.ts']) {
      const src = fs.readFileSync(path.join(__dirname, '..', f), 'utf-8');
      expect(src).not.toMatch(/node:fs|from 'fs'|require\('fs'\)/);
    }
  });
});
