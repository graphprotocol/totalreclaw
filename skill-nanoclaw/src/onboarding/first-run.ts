/**
 * First-run onboarding — NanoClaw skill 3.1.1.
 *
 * Parity with OpenClaw plugin 3.3.0 (first-run welcome via prependContext) and
 * Hermes 2.3.1 (first-run welcome via stdout). The NanoClaw equivalent uses
 * the Claude Agent SDK's SessionStart hook (source='startup') to inject a
 * one-time welcome + branch question into the agent's additional context
 * when the credentials file is missing / empty / invalid.
 *
 * NOTE: NanoClaw has no interactive CLI onboarding wizard (unlike OpenClaw's
 * `openclaw totalreclaw onboard` and Hermes's `hermes setup`). The NanoClaw
 * agent runs in a container; the user must either (a) pre-populate
 * `$WORKSPACE_DIR/.totalreclaw/credentials.json` with a recovery phrase they
 * already generated via another client, or (b) use the OpenClaw or Hermes
 * CLI on a local machine to mint a phrase, then hand-copy the credentials
 * file into the NanoClaw workspace. This limitation is surfaced verbatim to
 * the user in the welcome message.
 */

import * as fs from 'fs';
import * as path from 'path';

/**
 * The canonical welcome message, shared across OpenClaw plugin 3.3.0 +
 * Hermes 2.3.1 + NanoClaw 3.1.1.
 */
export const WELCOME_MESSAGE = `Welcome to TotalReclaw — encrypted, agent-portable memory.

Your memories are stored end-to-end encrypted and on-chain. You can restore them on any agent — OpenClaw, Hermes, or NanoClaw — with a single recovery phrase.`;

/**
 * The branch question: existing user vs. new user. NanoClaw is a single
 * runtime (no local/remote distinction like OpenClaw), so no mode-switching
 * — just ask about the recovery phrase.
 */
export const BRANCH_QUESTION = `Let's set up your account. Do you already have a recovery phrase, or should we generate a new one?`;

/**
 * NanoClaw-specific setup instructions. Unlike OpenClaw / Hermes, NanoClaw
 * does NOT have an interactive wizard. The user has three paths:
 *   1. Generate a BIP-39 phrase with an external tool, hand-populate the
 *      credentials file.
 *   2. Use the OpenClaw CLI (`openclaw totalreclaw onboard`) or the Hermes
 *      CLI (`hermes setup`) on a local machine to mint a phrase, then share
 *      the credentials file with the NanoClaw workspace.
 *   3. Re-use a phrase they already have from another TotalReclaw client —
 *      cross-client portability is a v1 contract guarantee.
 */
export const NANOCLAW_INSTRUCTIONS = `To set up: generate a recovery phrase with your preferred BIP39 tool, or provide one you already have. Save it to ~/.totalreclaw/credentials.json in this shape:
  { "mnemonic": "word1 word2 ... word12", "scope_address": "0x..." }

Or use the OpenClaw or Hermes CLI to generate one — the phrase will work across all three.`;

/**
 * Security + storage guidance. The recovery phrase is the user's only
 * identity and cannot be recovered if lost.
 */
export const STORAGE_GUIDANCE = `Your recovery phrase is 12 words. Store it somewhere safe — a password manager works well. Use it only for TotalReclaw. Don't reuse it anywhere else. Don't put funds on it.`;

/**
 * Shape of the credentials file written by the OpenClaw / Hermes CLIs. The
 * NanoClaw MCP server reads the same shape.
 */
interface CredentialsShape {
  mnemonic?: string;
  scope_address?: string;
}

/**
 * Detect first-run state: returns true if the credentials file is missing,
 * empty, or does not contain a usable mnemonic. Any read / parse error is
 * treated as first-run (safe default — the alternative is silently failing
 * with a cryptic error downstream when the MCP server tries to derive keys).
 */
export async function detectFirstRun(credentialsPath: string): Promise<boolean> {
  try {
    if (!fs.existsSync(credentialsPath)) {
      return true;
    }
    const raw = await fs.promises.readFile(credentialsPath, 'utf-8');
    const trimmed = raw.trim();
    if (trimmed.length === 0) {
      return true;
    }

    let parsed: CredentialsShape;
    try {
      parsed = JSON.parse(trimmed) as CredentialsShape;
    } catch {
      // Unparseable JSON — treat as first-run so we re-emit onboarding.
      return true;
    }

    if (!parsed || typeof parsed !== 'object') {
      return true;
    }

    const mnemonic = typeof parsed.mnemonic === 'string' ? parsed.mnemonic.trim() : '';
    if (mnemonic.length === 0) {
      return true;
    }

    // A valid recovery phrase is 12 BIP-39 words. We don't verify the
    // checksum here (that's @totalreclaw/core's job at key-derivation
    // time) — we only check structural validity so we don't emit the
    // welcome for a clearly-populated file with a bogus phrase; leave
    // deeper validation to the MCP server + billing.ts.
    const wordCount = mnemonic.split(/\s+/).filter(Boolean).length;
    if (wordCount !== 12 && wordCount !== 24) {
      return true;
    }

    return false;
  } catch {
    // Any unexpected error (permission, IO) → treat as first-run so the
    // user is walked through the setup rather than hitting a cryptic fail.
    return true;
  }
}

/**
 * Build the full welcome message injected via `SessionStart` →
 * `additionalContext`. Includes the branded intro, the branch question, the
 * NanoClaw-specific setup instructions (no interactive wizard), and the
 * storage guidance — all four pieces shipped as one block so the agent has
 * the full picture to answer the user's next message.
 */
export function buildWelcomeMessage(): string {
  return [
    WELCOME_MESSAGE,
    '',
    BRANCH_QUESTION,
    '',
    NANOCLAW_INSTRUCTIONS,
    '',
    STORAGE_GUIDANCE,
  ].join('\n');
}

/**
 * Resolve the credentials file path the NanoClaw agent-runner uses by
 * default. Mirrors the precedence documented in SKILL.md / nanoclaw-agent-runner.ts:
 *
 *   1. $TOTALRECLAW_CREDENTIALS_PATH (explicit override)
 *   2. $WORKSPACE_DIR/.totalreclaw/credentials.json (container-scoped)
 *   3. ~/.totalreclaw/credentials.json (user-scoped fallback)
 */
export function resolveCredentialsPath(): string {
  if (process.env.TOTALRECLAW_CREDENTIALS_PATH) {
    return process.env.TOTALRECLAW_CREDENTIALS_PATH;
  }
  const workspace = process.env.WORKSPACE_DIR;
  if (workspace) {
    return path.join(workspace, '.totalreclaw', 'credentials.json');
  }
  const home = process.env.HOME ?? '/home/node';
  return path.join(home, '.totalreclaw', 'credentials.json');
}

// ---------------------------------------------------------------------------
// Session-scoped sentinel — emit the welcome at most once per Node process.
// SessionStart fires for `startup`, `resume`, `clear`, and `compact`; we only
// want to welcome on real first-contact (startup + missing credentials), not
// on every compact.
// ---------------------------------------------------------------------------

let _welcomeEmittedForProcess = false;

/** Test-only reset for the session sentinel. */
export function _resetWelcomeSentinel(): void {
  _welcomeEmittedForProcess = false;
}

/**
 * Full first-run check-and-inject: returns the welcome `additionalContext`
 * string if this is a real first-run and we haven't already emitted once,
 * else returns undefined. Honours SessionStart `source` to avoid emitting
 * on resume / clear / compact events.
 */
export async function maybeBuildFirstRunContext(options: {
  credentialsPath?: string;
  source?: 'startup' | 'resume' | 'clear' | 'compact';
}): Promise<string | undefined> {
  if (_welcomeEmittedForProcess) {
    return undefined;
  }
  // Only emit on startup-like events. `compact` is a mid-session compaction
  // and should never re-inject onboarding (even if credentials went missing
  // mid-session, which would indicate a different bug).
  const source = options.source ?? 'startup';
  if (source === 'compact') {
    return undefined;
  }
  const credentialsPath = options.credentialsPath ?? resolveCredentialsPath();
  const isFirstRun = await detectFirstRun(credentialsPath);
  if (!isFirstRun) {
    return undefined;
  }
  _welcomeEmittedForProcess = true;
  return buildWelcomeMessage();
}
