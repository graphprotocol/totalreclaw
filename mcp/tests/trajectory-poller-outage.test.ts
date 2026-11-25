/**
 * Trajectory poller: window retention (internal #604 / #553 / #781).
 *
 * One rule: the stored offset moves only when an extraction attempt consumed
 * the window (success or a genuine zero-facts answer) or the window was
 * finally given up on. Deferral (below the turn threshold, or the per-poll
 * cap) and extraction failures (an LLM outage) keep the offset.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  OUTAGE_MAX_RETRIES,
  outageBackoffMs,
  startTrajectoryPoller,
  type ExtractedFactLike,
  type TrajectoryPollerDeps,
} from '../src/trajectory-poller.js';

type Msg = { role: 'user' | 'assistant'; content: string };

const OUTAGE = (): Error =>
  Object.assign(new Error('LLM upstream outage after 3 attempts: LLM API 429: {"code":"1302"}'), {
    name: 'LLMUpstreamOutageError',
  });

let TMP: string;
beforeEach(() => {
  TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-poller-outage-'));
});
afterEach(() => {
  fs.rmSync(TMP, { recursive: true, force: true });
});

function sessionsDir(home: string): string {
  const d = path.join(home, '.openclaw', 'agents', 'main', 'sessions');
  fs.mkdirSync(d, { recursive: true });
  return d;
}

function appendTurns(file: string, from: number, to: number, tag = ''): void {
  const lines: string[] = [];
  for (let i = from; i <= to; i++) {
    lines.push(JSON.stringify({ type: 'prompt.submitted', data: { prompt: `${tag}user ${i}` } }));
    lines.push(JSON.stringify({ type: 'model.completed', data: { assistantTexts: [`${tag}assistant ${i}`] } }));
  }
  fs.appendFileSync(file, lines.join('\n') + '\n');
}

function writeFile(home: string, sid: string, turns: number, tag = ''): string {
  const file = path.join(sessionsDir(home), `${sid}.trajectory.jsonl`);
  fs.writeFileSync(file, '');
  appendTurns(file, 1, turns, tag);
  return file;
}

interface Run {
  home: string;
  stateFile: string;
  warns: string[];
  calls: Array<{ messages: Msg[]; mode: string }>;
  persisted: number;
  extractor: (messages: Msg[], mode: string) => Promise<ExtractedFactLike[]>;
}

function setup(interval = 2): Run & { poll: () => Promise<void> } {
  const home = TMP;
  const stateFile = path.join(home, '.totalreclaw', 'extract-state.json');
  const run: Run = {
    home,
    stateFile,
    warns: [],
    calls: [],
    persisted: 0,
    extractor: async () => [{ text: 'a fact', importance: 0.8 }],
  };
  const poll = async (): Promise<void> => {
    const origHomedir = os.homedir;
    (os as unknown as { homedir: () => string }).homedir = () => home;
    try {
      const deps: TrajectoryPollerDeps = {
        logger: { info: () => {}, warn: (m: string) => { run.warns.push(m); }, error: () => {} },
        ensureInitialized: async () => {},
        isPairingPending: () => false,
        isImportActive: () => false,
        getExtractInterval: () => interval,
        getMaxFactsPerExtraction: () => 10,
        isDedupEnabled: () => false,
        getDedupCandidates: async () => [],
        runExtraction: async (messages, mode) => {
          run.calls.push({ messages: messages as Msg[], mode: mode as string });
          return run.extractor(messages as Msg[], mode as string);
        },
        filterByImportance: (facts) => ({ kept: facts, dropped: 0 }),
        persistFacts: async (facts) => {
          run.persisted += facts.length;
          return facts.length;
        },
      };
      const h = startTrajectoryPoller(deps, { pollIntervalMs: 3_600_000, stateFile });
      try {
        await h.pollOnce();
      } finally {
        h.stop();
      }
    } finally {
      (os as unknown as { homedir: () => string }).homedir = origHomedir;
    }
  };
  return Object.assign(run, { poll });
}

const readState = (stateFile: string): Record<string, Record<string, number>> =>
  JSON.parse(fs.readFileSync(stateFile, 'utf-8'));

function presetState(stateFile: string, entries: Record<string, unknown>): void {
  fs.mkdirSync(path.dirname(stateFile), { recursive: true });
  fs.writeFileSync(stateFile, JSON.stringify(entries));
}

describe('outage retry', () => {
  it('keeps the offset and schedules a retry when extraction throws', async () => {
    const r = setup();
    const file = writeFile(r.home, 's1', 2);
    r.extractor = async () => { throw OUTAGE(); };
    const before = Date.now();
    await r.poll();
    expect(r.persisted).toBe(0);
    const e = readState(r.stateFile)[file];
    expect(e.offset).toBe(0);
    expect(e.turnsAccum).toBe(0);
    expect(e.outageRetries).toBe(1);
    expect(e.retryAt).toBeGreaterThan(before);
    expect(e.retryAt).toBeLessThanOrEqual(Date.now() + 121_000);
    expect(r.warns.some((w) => w.includes('attempt 1/5'))).toBe(true);
  });

  it('does not retry before retryAt', async () => {
    const r = setup();
    const file = writeFile(r.home, 's1', 2);
    const preset = { offset: 0, turnsAccum: 0, outageRetries: 1, retryAt: Date.now() + 60_000 };
    presetState(r.stateFile, { [file]: preset });
    await r.poll();
    expect(r.calls.length).toBe(0);
    expect(readState(r.stateFile)[file]).toEqual(preset);
  });

  it('retries the whole window in full mode after the backoff, then advances', async () => {
    const r = setup();
    const file = writeFile(r.home, 's1', 2);
    presetState(r.stateFile, { [file]: { offset: 0, turnsAccum: 0, outageRetries: 1, retryAt: 1 } });
    appendTurns(file, 3, 3);
    await r.poll();
    expect(r.calls.length).toBe(1);
    expect(r.calls[0].mode).toBe('full');
    const contents = r.calls[0].messages.map((m) => m.content);
    expect(contents).toContain('user 1');
    expect(contents).toContain('user 3');
    expect(readState(r.stateFile)[file]).toEqual({ offset: fs.statSync(file).size, turnsAccum: 0 });
  });

  it('zero facts still advances the offset', async () => {
    const r = setup();
    const file = writeFile(r.home, 's1', 2);
    r.extractor = async () => [];
    await r.poll();
    expect(readState(r.stateFile)[file]).toEqual({ offset: fs.statSync(file).size, turnsAccum: 0 });
  });

  it('gives up after OUTAGE_MAX_RETRIES and advances', async () => {
    const r = setup();
    const file = writeFile(r.home, 's1', 2);
    presetState(r.stateFile, {
      [file]: { offset: 0, turnsAccum: 0, outageRetries: OUTAGE_MAX_RETRIES - 1, retryAt: 1 },
    });
    r.extractor = async () => { throw OUTAGE(); };
    await r.poll();
    expect(readState(r.stateFile)[file]).toEqual({ offset: fs.statSync(file).size, turnsAccum: 0 });
    expect(r.warns.some((w) => w.includes('giving up'))).toBe(true);
  });

  it('a waiting window is not advanced when the per-poll cap defers it', async () => {
    const r = setup();
    const a = writeFile(r.home, 'a', 2, 'A ');
    const b = writeFile(r.home, 'b', 2, 'B ');
    presetState(r.stateFile, { [a]: { offset: 0, turnsAccum: 0, outageRetries: 1, retryAt: 1 } });
    await r.poll();
    expect(r.calls.length).toBe(1);
    const st = readState(r.stateFile);
    const extractedA = r.calls[0].messages[0].content.startsWith('A ');
    if (!extractedA) {
      expect(st[a].offset).toBe(0);
      expect(st[a].outageRetries).toBe(1);
    } else {
      expect(st[b]).toBeUndefined();
    }
  });

  it('outageBackoffMs doubles from 2 min and caps at 30 min', () => {
    expect([1, 2, 3, 4, 10].map(outageBackoffMs)).toEqual([120000, 240000, 480000, 960000, 1800000]);
  });
});

describe('deferred turns are kept (#781)', () => {
  it('interval 3, one turn per poll: extraction gets all three turns', async () => {
    const r = setup(3);
    const file = writeFile(r.home, 's1', 1);
    await r.poll();
    appendTurns(file, 2, 2);
    await r.poll();
    expect(r.calls.length).toBe(0);
    appendTurns(file, 3, 3);
    await r.poll();
    expect(r.calls.length).toBe(1);
    expect(r.calls[0].messages.map((m) => m.content)).toEqual([
      'user 1', 'assistant 1', 'user 2', 'assistant 2', 'user 3', 'assistant 3',
    ]);
    expect(readState(r.stateFile)[file]).toEqual({ offset: fs.statSync(file).size, turnsAccum: 0 });
  });

  it('a cap-skipped file keeps its messages and is extracted on the next poll', async () => {
    const r = setup();
    writeFile(r.home, 'a', 2, 'A ');
    writeFile(r.home, 'b', 2, 'B ');
    await r.poll();
    await r.poll();
    expect(r.calls.length).toBe(2);
    const tags = r.calls.map((c) => c.messages[0].content[0]).sort();
    expect(tags).toEqual(['A', 'B']);
    expect(r.calls.every((c) => c.messages.length === 4)).toBe(true);
  });

  it('an old state file with a turnsAccum value still loads', async () => {
    const r = setup();
    const file = writeFile(r.home, 's1', 2);
    presetState(r.stateFile, { [file]: { offset: 0, turnsAccum: 1 } });
    await r.poll();
    expect(r.calls.length).toBe(1);
    expect(readState(r.stateFile)[file]).toEqual({ offset: fs.statSync(file).size, turnsAccum: 0 });
  });
});
