import fs from 'node:fs';
import path from 'node:path';

// index.ts boots a stdio server on import, so the wiring is checked as source.
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'index.ts'), 'utf-8');

describe('recent-writes wiring in index.ts', () => {
  test('remember, debrief, crystal debrief and pin deps record their writes', () => {
    expect((src.match(/recordSubmittedPayloads\(recentWrites,/g) ?? []).length).toBeGreaterThanOrEqual(4);
  });
  test('forget, lookups and the subgraph lookup are wired', () => {
    expect(src).toContain('recentWrites.recordTombstone(');
    expect(src).toContain('createRewriteLookup(');
    expect(src).toContain('createForgetLookup(');
    expect(src).toContain('lookupFactById(');
  });
  test('resetPerAccountState clears the record', () => {
    const start = src.indexOf('function resetPerAccountState(');
    const body = src.slice(start, src.indexOf('\n}', start));
    expect(body).toContain('recentWrites.clear()');
  });
});
