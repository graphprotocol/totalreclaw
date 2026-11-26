/**
 * TotalReclaw MCP - relay read denials and the client-wide read pause
 *
 * Port of the Python client's read-denial handling (internal #662,
 * `python/src/totalreclaw/relay.py`: `_classify_subgraph_error`,
 * `_set_read_block`, `read_block`; wording from `agent/read_block.py`;
 * spec `docs/specs/totalreclaw/read-error-surfacing.md`).
 *
 * Before this, `gqlQuery` collapsed every non-2xx answer to `null`, so a
 * relay read denial (429 `read_quota_exceeded`, 429 `rate_limited`, the
 * legacy 403 `quota_exceeded`) looked like an empty vault. Now a non-2xx
 * answer becomes a typed error; a denial also pauses reads for this account
 * until a deadline, so a recall's query fan-out stops at the first denied
 * call and later calls fail without touching the network.
 *
 * Classification matches `error_code`, then `error`, by string equality
 * (never substring: `read_quota_exceeded` contains `quota_exceeded`).
 * Only denials start a pause; 5xx and other errors do not.
 *
 * Pure module apart from one stderr line per pause episode.
 */

/** Same env var, floor, ceiling and default as the Python client. */
const REPROBE_FLOOR_S = 5;
const REPROBE_CEILING_S = 86_400;
const REPROBE_DEFAULT_S = 900;

const RATE_LIMIT_FLOOR_S = 30;
const RATE_LIMIT_CEILING_S = 3_600;
const RATE_LIMIT_DEFAULT_S = 300;

/** Non-2xx answer from `POST /v1/subgraph`. */
export class RelayReadError extends Error {
  readonly statusCode: number;
  readonly errorCode: string | null;
  readonly relayMessage: string | null;
  constructor(
    statusCode: number,
    opts: { errorCode?: string | null; relayMessage?: string | null; message?: string } = {},
  ) {
    const detail = opts.relayMessage || opts.errorCode || 'unknown error';
    super(opts.message ?? `TotalReclaw relay read failed (HTTP ${statusCode}): ${detail}`);
    this.name = 'RelayReadError';
    this.statusCode = statusCode;
    this.errorCode = opts.errorCode ?? null;
    this.relayMessage = opts.relayMessage ?? null;
  }
}

/** Reads are denied for a window. Starts the read pause. */
export class RelayReadBlocked extends RelayReadError {
  readonly kind: 'read_quota' | 'rate_limited';
  /** Seconds, from the body `retry_after`, else the `Retry-After` header. */
  readonly retryAfterS: number | null;
  readonly upgradeUrl: string | null;
  constructor(
    kind: 'read_quota' | 'rate_limited',
    statusCode: number,
    opts: {
      errorCode?: string | null;
      relayMessage?: string | null;
      retryAfterS?: number | null;
      upgradeUrl?: string | null;
      message: string;
    },
  ) {
    super(statusCode, opts);
    this.name = 'RelayReadBlocked';
    this.kind = kind;
    this.retryAfterS = opts.retryAfterS ?? null;
    this.upgradeUrl = opts.upgradeUrl ?? null;
  }
}

export class RelayReadQuotaExceeded extends RelayReadBlocked {
  readonly tier: string | null;
  readonly limit: number | null;
  readonly used: number | null;
  /** ISO string as the relay sent it; null on the legacy 403 contract. */
  readonly resetsAt: string | null;
  /** True for the legacy 403 `quota_exceeded` contract. */
  readonly legacy: boolean;
  constructor(
    statusCode: number,
    opts: {
      errorCode?: string | null;
      relayMessage?: string | null;
      retryAfterS?: number | null;
      upgradeUrl?: string | null;
      tier?: string | null;
      limit?: number | null;
      used?: number | null;
      resetsAt?: string | null;
      legacy?: boolean;
    },
  ) {
    super('read_quota', statusCode, {
      ...opts,
      message: 'TotalReclaw memory reads are paused: monthly read limit reached.',
    });
    this.name = 'RelayReadQuotaExceeded';
    this.tier = opts.tier ?? null;
    this.limit = opts.limit ?? null;
    this.used = opts.used ?? null;
    this.resetsAt = opts.resetsAt ?? null;
    this.legacy = opts.legacy ?? false;
  }
}

export class RelayRateLimited extends RelayReadBlocked {
  readonly limitScope: string | null;
  constructor(
    statusCode: number,
    opts: {
      errorCode?: string | null;
      relayMessage?: string | null;
      retryAfterS?: number | null;
      limitScope?: string | null;
    },
  ) {
    const retry = opts.retryAfterS;
    const message = typeof retry === 'number'
      ? `TotalReclaw memory reads are paused: too many requests (retry in ~${Math.max(1, Math.round(retry / 60))} min).`
      : 'TotalReclaw memory reads are paused: too many requests.';
    super('rate_limited', statusCode, { ...opts, message });
    this.name = 'RelayRateLimited';
    this.limitScope = opts.limitScope ?? null;
  }
}

function bodyString(body: Record<string, unknown> | null, key: string): string | null {
  const v = body?.[key];
  return typeof v === 'string' && v ? v : null;
}

function bodyInt(body: Record<string, unknown> | null, key: string): number | null {
  const v = body?.[key];
  return typeof v === 'number' && Number.isInteger(v) ? v : null;
}

function retryAfterSeconds(body: Record<string, unknown> | null, header: string | null): number | null {
  const raw = body?.retry_after;
  if (typeof raw === 'number' && Number.isFinite(raw) && raw >= 0) return raw;
  const h = header?.trim();
  if (!h) return null;
  const secs = Number(h);
  if (Number.isFinite(secs)) return secs >= 0 ? secs : null;
  const at = Date.parse(h);
  if (Number.isNaN(at)) return null;
  const delta = (at - Date.now()) / 1000;
  return delta >= 0 ? delta : null;
}

/**
 * Classify a non-2xx `/v1/subgraph` response. First match wins:
 *   1. code === 'read_quota_exceeded' (any status) -> quota, new contract
 *   2. status 403 and code === 'quota_exceeded'    -> quota, legacy contract
 *   3. status 429                                   -> rate limited
 *   4. anything else                                -> plain RelayReadError
 * `bodyText` may be any text (a Cloudflare HTML page, empty).
 */
export function classifySubgraphError(
  status: number,
  bodyText: string,
  retryAfterHeader: string | null = null,
): RelayReadError {
  let body: Record<string, unknown> | null = null;
  try {
    const parsed: unknown = JSON.parse(bodyText);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      body = parsed as Record<string, unknown>;
    }
  } catch { /* non-JSON body */ }

  const rawCode = body ? (body.error_code || body.error) : null;
  const code = typeof rawCode === 'string' ? rawCode : null;
  const message = bodyString(body, 'message');
  const relayMessage = message ? message.slice(0, 200) : null;
  const retryAfterS = retryAfterSeconds(body, retryAfterHeader);

  if (code === 'read_quota_exceeded') {
    return new RelayReadQuotaExceeded(status, {
      errorCode: code,
      relayMessage,
      retryAfterS,
      upgradeUrl: bodyString(body, 'upgrade_url'),
      tier: bodyString(body, 'tier'),
      limit: bodyInt(body, 'limit'),
      used: bodyInt(body, 'used'),
      resetsAt: bodyString(body, 'resets_at'),
      legacy: false,
    });
  }
  if (status === 403 && code === 'quota_exceeded') {
    return new RelayReadQuotaExceeded(status, {
      errorCode: code,
      relayMessage,
      retryAfterS,
      upgradeUrl: bodyString(body, 'upgrade_url'),
      legacy: true,
    });
  }
  if (status === 429) {
    return new RelayRateLimited(status, {
      errorCode: code,
      relayMessage,
      retryAfterS,
      limitScope: bodyString(body, 'limit_scope'),
    });
  }
  return new RelayReadError(status, { errorCode: code, relayMessage });
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** Re-probe cadence in seconds (`TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS`, 5 s to 24 h). */
export function readPauseReprobeSeconds(): number {
  const raw = process.env.TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS;
  if (raw) {
    const v = Number(raw);
    if (Number.isFinite(v)) return clamp(v, REPROBE_FLOOR_S, REPROBE_CEILING_S);
  }
  return REPROBE_DEFAULT_S;
}

/**
 * How long to pause reads for `err`. A rate limit pauses for the relay's
 * `retry_after` (30 s to 1 h, default 5 min). A quota denial re-probes every
 * `readPauseReprobeSeconds()` (or sooner if the reset is closer), so a raised
 * cap, an upgrade or a transient relay error does not stay stuck until the
 * reset date.
 */
export function pauseSeconds(err: RelayReadBlocked): number {
  if (err instanceof RelayRateLimited) {
    return clamp(err.retryAfterS ?? RATE_LIMIT_DEFAULT_S, RATE_LIMIT_FLOOR_S, RATE_LIMIT_CEILING_S);
  }
  const reprobe = readPauseReprobeSeconds();
  if (err instanceof RelayReadQuotaExceeded && err.resetsAt) {
    const at = Date.parse(err.resetsAt);
    if (!Number.isNaN(at)) {
      const remaining = (at - Date.now()) / 1000;
      if (remaining > 0) return Math.min(reprobe, remaining);
    }
  }
  return reprobe;
}

export interface ReadBlockState {
  error: RelayReadBlocked;
  /** Epoch ms deadline. */
  pausedUntilMs: number;
  sinceMs: number;
  episode: number;
  /** Lowercased owner the denial was for. */
  wallet: string;
}

let current: ReadBlockState | null = null;
let episodeCounter = 0;

const defaultLog = (line: string) => console.error(line);
let logLine: (line: string) => void = defaultLog;

/** Test hook: capture log lines. */
export function setReadBlockLogger(fn: ((line: string) => void) | null): void {
  logLine = fn ?? defaultLog;
}

/** Test hook: forget any pause. */
export function resetReadBlock(): void {
  current = null;
  episodeCounter = 0;
  logLine = defaultLog;
}

/**
 * The active pause for `wallet`, or null once its deadline passed (or when
 * the pause belongs to another account). The record itself is kept past the
 * deadline: a re-probe that is denied again stays in the same episode and
 * logs nothing; only a success (`clearReadBlock`) ends the episode.
 */
export function getReadBlock(wallet?: string): ReadBlockState | null {
  const blk = current;
  if (!blk) return null;
  if (wallet && blk.wallet !== wallet.toLowerCase()) return null;
  if (Date.now() >= blk.pausedUntilMs) return null;
  return blk;
}

export function setReadBlock(err: RelayReadBlocked, wallet: string): void {
  const now = Date.now();
  const pauseS = pauseSeconds(err);
  const w = wallet.toLowerCase();
  const prev = current && current.wallet === w ? current : null;
  const state: ReadBlockState = {
    error: err,
    pausedUntilMs: now + pauseS * 1000,
    sinceMs: prev ? prev.sinceMs : now,
    episode: prev ? prev.episode : ++episodeCounter,
    wallet: w,
  };
  current = state;
  // One line per episode: a recall's parallel queries are all denied at once.
  if (!prev) {
    logLine(
      `[search] relay reads paused (${err.kind}) until ${new Date(state.pausedUntilMs).toISOString()}; ` +
      'recall and status will show a notice',
    );
  }
}

/** Any 2xx `/v1/subgraph` answer ends the episode. */
export function clearReadBlock(wallet?: string): void {
  if (!current) return;
  if (wallet && current.wallet !== wallet.toLowerCase()) return;
  current = null;
}

function resetPhrase(err: RelayReadBlocked): string {
  if (err instanceof RelayReadQuotaExceeded && !err.legacy && err.resetsAt) {
    const at = new Date(err.resetsAt);
    if (!Number.isNaN(at.getTime())) {
      return `it resets on ${at.toISOString().slice(0, 10)} (UTC)`;
    }
  }
  return 'the 1st of next month (UTC)';
}

/** Plain-language sentence for a denial (wording from the Python client). */
export function readBlockText(err: RelayReadBlocked, blk: ReadBlockState | null): string {
  if (err instanceof RelayReadQuotaExceeded) {
    return (
      'Memory lookups are paused: this account has used its monthly ' +
      'memory-read allowance. Memories are safe and new ones are still ' +
      `being saved; lookups resume automatically — ${resetPhrase(err)}.`
    );
  }
  if (blk) {
    const minutes = Math.max(1, Math.round(Math.max(0, blk.pausedUntilMs - Date.now()) / 60_000));
    return (
      'Memory lookups are briefly paused (too many requests). ' +
      `They resume automatically in about ${minutes} minutes.`
    );
  }
  return 'Memory lookups are briefly paused (too many requests).';
}

export const READ_BLOCK_INSTRUCTION =
  'Relay this to the user in one sentence. Do NOT say they have no memories.';

/**
 * Tool-result payload for a denial. Deliberately carries no `memories` or
 * `count` key: that shape reads as "0 results".
 */
export function readBlockPayload(err: RelayReadBlocked, blk?: ReadBlockState | null): Record<string, unknown> {
  const state = blk === undefined ? getReadBlock() : blk;
  const isQuota = err instanceof RelayReadQuotaExceeded;
  return {
    error: readBlockText(err, state),
    error_code: isQuota ? 'read_quota_exceeded' : 'rate_limited',
    ...(err.relayMessage ? { relay_message: err.relayMessage } : {}),
    reads_paused_until: state ? new Date(state.pausedUntilMs).toISOString() : null,
    retry_after_seconds: state ? Math.max(0, Math.round((state.pausedUntilMs - Date.now()) / 1000)) : null,
    relay_retry_after_seconds: err.retryAfterS,
    resets_at: isQuota ? err.resetsAt : null,
    upgrade_url: err.upgradeUrl,
    instruction: READ_BLOCK_INSTRUCTION,
  };
}

type ToolResult = { content: Array<{ type: string; text: string }>; isError?: boolean };

/** Recall's tool result for a denial: the payload plus latency and mode. */
export function readBlockToolResult(
  err: RelayReadBlocked,
  wallet: string,
  extra: Record<string, unknown> = {},
): ToolResult {
  return {
    content: [{
      type: 'text',
      text: JSON.stringify({ ...readBlockPayload(err, getReadBlock(wallet)), ...extra }),
    }],
  };
}

/**
 * While reads are paused for `wallet`, add a `memory_reads` block to the
 * status result so the agent can say why recall is not answering. Returns the
 * result unchanged when reads are not paused or its text is not JSON.
 */
export function withReadPauseNotice<T extends ToolResult>(result: T, wallet: string | undefined): T {
  const blk = wallet ? getReadBlock(wallet) : null;
  if (!blk) return result;
  try {
    const first = result.content[0];
    const parsed = JSON.parse(first.text) as Record<string, unknown>;
    const payload = readBlockPayload(blk.error, blk);
    parsed.memory_reads = { paused: true, ...payload };
    if (typeof parsed.formatted === 'string') {
      parsed.formatted = `${parsed.formatted}\nMemory lookups: ${payload.error}`;
    }
    return { ...result, content: [{ ...first, text: JSON.stringify(parsed) }, ...result.content.slice(1)] };
  } catch {
    return result;
  }
}
