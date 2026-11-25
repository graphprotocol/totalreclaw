import { createRecallTimer } from '../src/recall-timing.js';
import { handleSupport } from '../src/tools/support.js';

describe('recall timing', () => {
  it('records every stage and emits one structured line with the expected keys', () => {
    const lines: string[] = [];
    const t = createRecallTimer('managed', (l) => lines.push(l));
    for (const s of ['trapdoors', 'embedding', 'search', 'broadened_search', 'decrypt', 'rerank', 'format']) t.mark(s);
    t.emit({ candidates: 3, k: 8 });
    t.emit({ candidates: 3, k: 8 }); // second emit is a no-op
    expect(lines).toHaveLength(1);
    expect(lines[0].startsWith('TotalReclaw recall-timing ')).toBe(true);
    const o = JSON.parse(lines[0].slice('TotalReclaw recall-timing '.length));
    expect(Object.keys(o).sort()).toEqual(['candidates', 'k', 'outcome', 'path', 'stages_ms', 'total_ms']);
    expect(Object.keys(o.stages_ms)).toEqual(['trapdoors', 'embedding', 'search', 'broadened_search', 'decrypt', 'rerank', 'format']);
    expect(o.candidates).toBe(3);
    expect(o.path).toBe('managed');
  });

  it('writes to stderr by default, never stdout', () => {
    const err = jest.spyOn(console, 'error').mockImplementation(() => {});
    const out = jest.spyOn(process.stdout, 'write').mockImplementation(() => true);
    const log = jest.spyOn(console, 'log').mockImplementation(() => {});
    const t = createRecallTimer('self-hosted');
    t.mark('client_recall');
    t.emit({ candidates: 1, k: 5 });
    expect(err).toHaveBeenCalledTimes(1);
    expect(out).not.toHaveBeenCalled();
    expect(log).not.toHaveBeenCalled();
    jest.restoreAllMocks();
  });

  it('support text no longer promises 140 ms', async () => {
    const res = await handleSupport({} as any, {});
    expect(res.content[0].text).not.toContain('140');
  });
});
