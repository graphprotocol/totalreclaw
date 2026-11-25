/**
 * Per-stage recall timing (internal #703).
 *
 * One structured line per recall, written to STDERR only. MCP stdio servers
 * must never write logs to stdout (it carries the JSON-RPC stream), and the
 * timing data is deliberately never added to the tool response.
 */

export type RecallTimingPath = 'managed' | 'self-hosted';

export interface RecallTimer {
  /** Record the time elapsed since the previous mark (or start) under `stage`. */
  mark(stage: string): void;
  /** Emit the single log line. Safe to call more than once; only the first emits. */
  emit(info: { candidates?: number; k?: number; outcome?: 'ok' | 'empty' | 'error' | 'invalid' }): void;
}

const nowMs = (): number => Number(process.hrtime.bigint()) / 1e6;

export function createRecallTimer(
  path: RecallTimingPath,
  write: (line: string) => void = (line) => console.error(line),
): RecallTimer {
  const start = nowMs();
  let last = start;
  const stages: Record<string, number> = {};
  let emitted = false;
  return {
    mark(stage: string): void {
      const t = nowMs();
      stages[stage] = Math.round((stages[stage] ?? 0) + (t - last));
      last = t;
    },
    emit(info): void {
      if (emitted) return;
      emitted = true;
      try {
        write(
          'TotalReclaw recall-timing ' +
            JSON.stringify({
              path,
              outcome: info.outcome ?? 'ok',
              k: info.k,
              candidates: info.candidates ?? 0,
              total_ms: Math.round(nowMs() - start),
              stages_ms: stages,
            }),
        );
      } catch {
        // Logging must never break a recall.
      }
    },
  };
}
