/**
 * 3.2.0 secure-onboarding CLI wizard.
 *
 * Runs as `openclaw totalreclaw onboard` via OpenClaw's `api.registerCli`
 * plugin hook. Lives on a pure TTY surface — stdout/stderr/stdin — and
 * NEVER routes any of its I/O through the LLM provider, the gateway
 * transcript, or any session-persisted channel. This is the load-bearing
 * property of the 3.2.0 threat model: the recovery phrase is generated,
 * displayed, entered, and persisted ENTIRELY on the user's local machine.
 *
 * Dispatch (registered from `index.ts`):
 *   openclaw totalreclaw onboard    — interactive generate / import / skip
 *   openclaw totalreclaw status     — show current onboarding state
 *
 * Scope (per user ratification 2026-04-19):
 *   - LOCAL USERS ONLY in 3.2.0. Import on a remote OpenClaw gateway is
 *     not supported; QR-pairing is deferred to 3.3.0.
 *   - Two paths: generate a new 12-word BIP-39 mnemonic, or import an
 *     existing one. Skip exits without side-effects.
 *
 * Non-goals:
 *   - This file does NOT touch the gateway's `register()` flow. It neither
 *     calls `initialize()` nor drives key derivation. Users re-enter their
 *     chat session after running the wizard; `initialize()` reads the new
 *     credentials.json on the next `ensureInitialized()` call. Forcing a
 *     re-init here would require importing the full plugin key-derivation
 *     stack, which this module intentionally avoids.
 *
 * Architectural constraints:
 *   - No network. No `process.env` reads. No outbound markers in comments
 *     — see skill/scripts/check-scanner.mjs. The module imports nothing
 *     that contains a network surface.
 *   - Minimal deps. Uses `@scure/bip39` (already a transitive dep via
 *     `@totalreclaw/core`) for mnemonic generation + validation. Uses
 *     `node:readline/promises` for interactive prompts. No `inquirer`,
 *     no `readline-sync`.
 *
 * See docs/plans/2026-04-20-plugin-320-secure-onboarding.md (internal repo,
 * commit dc6bddd) for the full design rationale.
 */

import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline/promises';
import { generateMnemonic, validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';

import {
  writeCredentialsJson,
  loadCredentialsJson,
  type CredentialsFile,
  writeOnboardingState,
  loadOnboardingState,
  type OnboardingState,
} from './fs-helpers.js';

// ---------------------------------------------------------------------------
// User-facing strings (centralised so tests can assert on them + localisation
// has one place to grow into later).
// ---------------------------------------------------------------------------

export const COPY = {
  welcome:
    '\nTotalReclaw — Secure onboarding\n\n' +
    'TotalReclaw is an end-to-end encrypted memory vault for AI agents.\n' +
    'Your memories are encrypted with a key only you control, stored on-chain\n' +
    'so they persist across sessions and clients.\n',
  menu:
    'How would you like to set up TotalReclaw?\n\n' +
    '  [1] Generate a new recovery phrase (first-time users)\n' +
    '  [2] Import an existing TotalReclaw recovery phrase (returning users)\n' +
    '  [3] Skip for now — memory features stay disabled\n',
  menuPrompt: 'Enter 1, 2, or 3: ',
  alreadyActive:
    '\nTotalReclaw is already set up and active on this machine.\n' +
    'Run `openclaw totalreclaw status` for details, or delete\n' +
    '~/.totalreclaw/credentials.json to start over.\n',
  generateWarning:
    '\nABOUT YOUR RECOVERY PHRASE\n\n' +
    '  - It is the ONLY key to your encrypted memories. TotalReclaw servers\n' +
    '    cannot recover it. If you lose it, your memories are gone forever.\n' +
    '  - You can import it into other TotalReclaw clients (Hermes, MCP) to\n' +
    '    recall the same memories everywhere.\n' +
    '  - You can restore your account on a new machine at any time using\n' +
    '    this phrase.\n' +
    '  - It is NOT a blockchain wallet. Do NOT fund it with crypto, and do\n' +
    '    NOT reuse an existing wallet\'s phrase here.\n',
  importWarning:
    '\nBEFORE YOU PASTE AN EXISTING PHRASE\n\n' +
    '  Your TotalReclaw recovery phrase must be DEDICATED to TotalReclaw.\n\n' +
    '  - NEVER import a phrase that controls a crypto wallet with funds.\n' +
    '  - NEVER import a phrase used with another service (Metamask, Ledger,\n' +
    '    Trust, seed backups, etc.).\n' +
    '  - If your only copy of a TotalReclaw phrase is on a shared wallet,\n' +
    '    STOP NOW, move your funds off the wallet, and pick a new dedicated\n' +
    '    phrase.\n',
  importRemoteLimitation:
    '\nNote: in 3.2.0, import only works when you run OpenClaw locally on the\n' +
    'machine that will hold the credentials. Importing an existing vault on a\n' +
    'remote OpenClaw gateway is not yet supported — that arrives in 3.3.0 via\n' +
    'QR-pairing.\n',
  importPrompt: 'Paste your 12-word recovery phrase (input hidden): ',
  clipboardHint:
    '\nTip: prefer typing the phrase into a password manager over copy-paste.\n' +
    'OS clipboards can be captured by other apps.\n',
  ackPromptTemplate: 'Type word #%N% from your phrase: ',
  postSuccessGenerate:
    '\nDone. Your recovery phrase is saved at ~/.totalreclaw/credentials.json\n' +
    '(mode 0600). Memory tools are now active.\n\n' +
    '  Next: run `openclaw chat` to start. I will automatically remember\n' +
    '        important things and recall relevant context across sessions.\n\n' +
    '  To view your phrase again later on this machine, open credentials.json\n' +
    '  directly. Keep it safe — on a new machine, run this wizard again and\n' +
    '  choose "import" with this phrase.\n',
  postSuccessImport:
    '\nDone. Your phrase is saved at ~/.totalreclaw/credentials.json (mode 0600).\n' +
    'Memory tools are now active.\n\n' +
    '  Next: run `openclaw chat` to start. Existing memories tied to this\n' +
    '        phrase will be available via totalreclaw_recall.\n',
  skipped:
    '\nSkipped. Run `openclaw totalreclaw onboard` anytime to resume.\n' +
    'Memory tools remain disabled until you do.\n',
  ackFailed:
    '\nWord mismatch. Please write the phrase down carefully and run this\n' +
    'wizard again. No credentials have been written.\n',
  importInvalid:
    '\nInvalid BIP-39 phrase (12 words required, checksum must match).\n' +
    'No credentials have been written. Run the wizard again with the\n' +
    'correct phrase.\n',
  existingPhraseHint:
    '\nA recovery phrase already exists at ~/.totalreclaw/credentials.json.\n' +
    'Delete that file first to replace it, or re-import with the same phrase.\n',
  statusHeader: 'TotalReclaw status\n',
  statusFresh:
    '  onboarding: not complete\n' +
    '  next step:  run `openclaw totalreclaw onboard` on this machine\n' +
    '  note:       your recovery phrase will be shown on the terminal, never in chat.\n',
  statusActive:
    '  onboarding: complete\n' +
    '  credentials: ~/.totalreclaw/credentials.json (mode 0600)\n' +
    '  state:       ~/.totalreclaw/state.json\n',
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type WizardChoice = 'generate' | 'import' | 'skip';

export interface WizardIo {
  stdout: NodeJS.WritableStream;
  stderr: NodeJS.WritableStream;
  /** Prompt and return the user's input. Single-line. */
  ask(question: string): Promise<string>;
  /** Prompt and return the user's input with no echo to stdout. */
  askHidden(question: string): Promise<string>;
  close(): void;
}

export interface WizardResult {
  choice: WizardChoice;
  /**
   * On 'skip' or failure, undefined. On successful 'generate' or 'import',
   * the final state persisted to disk (useful for tests).
   */
  state?: OnboardingState;
  /** Non-null only on error outcomes; stdout has the human copy. */
  error?: string;
}

export interface WizardDeps {
  credentialsPath: string;
  statePath: string;
  io: WizardIo;
  /** Override for tests — defaults to @scure/bip39 generateMnemonic(128). */
  generateMnemonic?: () => string;
  /** Override for tests — defaults to @scure/bip39 validateMnemonic. */
  validateMnemonic?: (phrase: string) => boolean;
  /** Override for tests to force deterministic probe indices. */
  randomProbeIndices?: () => number[];
}

// ---------------------------------------------------------------------------
// IO factory — default builds a readline-backed prompter over process.stdin/out.
// ---------------------------------------------------------------------------

/**
 * Build a WizardIo backed by `process.stdin` / `process.stdout`. Hidden
 * input (for the import path) is implemented via direct raw-mode manipulation
 * of the TTY, which is the standard node technique when no prompter library
 * is available. Falls back to non-hidden input if the process is not
 * attached to a TTY (e.g. CI / piped stdin) — the caller's responsibility
 * to decide whether that is OK.
 */
export function buildDefaultIo(): WizardIo {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const stdout = process.stdout;
  const stderr = process.stderr;

  async function ask(question: string): Promise<string> {
    return (await rl.question(question)).trim();
  }

  async function askHidden(question: string): Promise<string> {
    const stdin = process.stdin;
    const isTTY = (stdin as NodeJS.ReadStream & { isTTY?: boolean }).isTTY === true;
    if (!isTTY || typeof stdin.setRawMode !== 'function') {
      // No TTY — fall back to visible input with a note. Safer than refusing.
      stdout.write('(input will be visible because stdin is not a TTY)\n');
      return ask(question);
    }

    stdout.write(question);

    return new Promise<string>((resolve) => {
      const chars: string[] = [];
      const onData = (chunk: Buffer) => {
        const str = chunk.toString('utf-8');
        for (const ch of str) {
          const code = ch.charCodeAt(0);
          if (code === 13 || code === 10) {
            // Enter / newline → finalise
            stdout.write('\n');
            stdin.setRawMode(false);
            stdin.pause();
            stdin.off('data', onData);
            resolve(chars.join('').trim());
            return;
          }
          if (code === 3) {
            // Ctrl-C → propagate SIGINT so the CLI exits cleanly
            stdin.setRawMode(false);
            stdin.pause();
            stdin.off('data', onData);
            process.kill(process.pid, 'SIGINT');
            return;
          }
          if (code === 127 || code === 8) {
            // Backspace / delete
            if (chars.length > 0) chars.pop();
            continue;
          }
          chars.push(ch);
        }
      };
      stdin.setRawMode(true);
      stdin.resume();
      stdin.on('data', onData);
    });
  }

  return {
    stdout,
    stderr,
    ask,
    askHidden,
    close: () => rl.close(),
  };
}

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

function printMnemonicGrid(mnemonic: string, out: NodeJS.WritableStream): void {
  const words = mnemonic.trim().split(/\s+/);
  const cols = 4;
  const pad = 14;
  const lines: string[] = [];
  for (let row = 0; row * cols < words.length; row++) {
    const parts: string[] = [];
    for (let col = 0; col < cols; col++) {
      const idx = row * cols + col;
      if (idx >= words.length) break;
      const label = `${String(idx + 1).padStart(2, ' ')}. ${words[idx]}`;
      parts.push(label.padEnd(pad, ' '));
    }
    lines.push('  ' + parts.join(''));
  }
  out.write(lines.join('\n') + '\n');
}

/**
 * Pick three distinct random word indices (0..11) for the retype-ack challenge.
 * Overridable for tests.
 */
function defaultRandomProbeIndices(): number[] {
  const pool = [...Array(12).keys()];
  const out: number[] = [];
  for (let i = 0; i < 3; i++) {
    const pick = Math.floor(Math.random() * pool.length);
    out.push(pool[pick]);
    pool.splice(pick, 1);
  }
  return out.sort((a, b) => a - b);
}

async function runAckChallenge(
  mnemonic: string,
  indices: number[],
  io: WizardIo,
): Promise<boolean> {
  const words = mnemonic.trim().split(/\s+/);
  for (const idx of indices) {
    const ans = await io.ask(COPY.ackPromptTemplate.replace('%N%', String(idx + 1)));
    if (ans.trim().toLowerCase() !== words[idx]) {
      return false;
    }
  }
  return true;
}

function normaliseMnemonic(input: string): string {
  // Collapse whitespace, strip zero-width, lowercase. BIP-39 wordlist is
  // entirely lowercase ASCII; any uppercase the user paste-in gets normalised.
  return input
    .normalize('NFKC')
    .replace(/[\u200B-\u200F\uFEFF]/g, '')
    .toLowerCase()
    .trim()
    .split(/\s+/)
    .join(' ');
}

function writeCredsAndState(
  credentialsPath: string,
  statePath: string,
  mnemonic: string,
  createdBy: 'generate' | 'import',
): OnboardingState {
  const creds: CredentialsFile = { mnemonic };
  if (!writeCredentialsJson(credentialsPath, creds)) {
    throw new Error(
      `Could not write credentials.json at ${credentialsPath}. Check that the parent directory is writable.`,
    );
  }
  const state: OnboardingState = {
    onboardingState: 'active',
    createdBy,
    credentialsCreatedAt: new Date().toISOString(),
    version: '3.2.0',
  };
  if (!writeOnboardingState(statePath, state)) {
    throw new Error(
      `Could not write state.json at ${statePath}. Check that the parent directory is writable.`,
    );
  }
  return state;
}

// ---------------------------------------------------------------------------
// Public entry points
// ---------------------------------------------------------------------------

/**
 * Interactive onboarding wizard. Single shot — caller builds a WizardDeps and
 * awaits. All user-visible output goes to `io.stdout` / `io.stderr`; all
 * input comes from `io.ask` / `io.askHidden`. No console.log calls.
 *
 * Returns a `WizardResult`. Tests can assert on both the result and the
 * stdout buffer.
 */
export async function runOnboardingWizard(deps: WizardDeps): Promise<WizardResult> {
  const { credentialsPath, statePath, io } = deps;
  const genMnemonic = deps.generateMnemonic ?? (() => generateMnemonic(wordlist, 128));
  const validate = deps.validateMnemonic ?? ((p: string) => validateMnemonic(p, wordlist));
  const probe = deps.randomProbeIndices ?? defaultRandomProbeIndices;

  // If onboarding is already complete on disk, short-circuit instead of
  // letting the user overwrite an existing phrase by accident.
  const existingCreds = loadCredentialsJson(credentialsPath);
  if (existingCreds?.mnemonic && typeof existingCreds.mnemonic === 'string' && existingCreds.mnemonic.trim()) {
    io.stdout.write(COPY.alreadyActive);
    const persisted = loadOnboardingState(statePath);
    return {
      choice: 'skip',
      state: persisted ?? {
        onboardingState: 'active',
        version: '3.2.0',
      },
    };
  }

  io.stdout.write(COPY.welcome);
  io.stdout.write(COPY.menu);

  const choiceRaw = await io.ask(COPY.menuPrompt);
  let choice: WizardChoice;
  if (choiceRaw === '1' || choiceRaw.toLowerCase() === 'generate') {
    choice = 'generate';
  } else if (choiceRaw === '2' || choiceRaw.toLowerCase() === 'import') {
    choice = 'import';
  } else if (choiceRaw === '3' || choiceRaw.toLowerCase() === 'skip') {
    choice = 'skip';
  } else {
    io.stderr.write(`\nUnrecognised choice "${choiceRaw}". Aborting.\n`);
    return { choice: 'skip', error: `invalid-choice:${choiceRaw}` };
  }

  if (choice === 'skip') {
    io.stdout.write(COPY.skipped);
    return { choice: 'skip' };
  }

  if (choice === 'generate') {
    io.stdout.write(COPY.generateWarning);
    io.stdout.write(COPY.importRemoteLimitation);
    const mnemonic = genMnemonic();
    if (typeof mnemonic !== 'string' || mnemonic.trim().split(/\s+/).length !== 12) {
      io.stderr.write('\nInternal error: mnemonic generator returned an invalid phrase.\n');
      return { choice, error: 'generator-invalid' };
    }

    io.stdout.write('\nYour recovery phrase (WRITE THIS DOWN):\n\n');
    printMnemonicGrid(mnemonic, io.stdout);
    io.stdout.write(COPY.clipboardHint);
    io.stdout.write('\n');

    const indices = probe();
    const ok = await runAckChallenge(mnemonic, indices, io);
    if (!ok) {
      io.stderr.write(COPY.ackFailed);
      return { choice, error: 'ack-failed' };
    }

    try {
      const state = writeCredsAndState(credentialsPath, statePath, mnemonic, 'generate');
      io.stdout.write(COPY.postSuccessGenerate);
      return { choice, state };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      io.stderr.write(`\n${msg}\n`);
      return { choice, error: `write-failed:${msg}` };
    }
  }

  // choice === 'import'
  io.stdout.write(COPY.importWarning);
  io.stdout.write(COPY.importRemoteLimitation);
  const raw = await io.askHidden(COPY.importPrompt);
  const normalised = normaliseMnemonic(raw);
  const words = normalised.split(/\s+/).filter(Boolean);
  if (words.length !== 12 || !validate(normalised)) {
    io.stderr.write(COPY.importInvalid);
    return { choice, error: 'invalid-phrase' };
  }

  try {
    const state = writeCredsAndState(credentialsPath, statePath, normalised, 'import');
    io.stdout.write(COPY.postSuccessImport);
    return { choice, state };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    io.stderr.write(`\n${msg}\n`);
    return { choice, error: `write-failed:${msg}` };
  }
}

/**
 * Print the current onboarding status to the given stdout. Safe — never
 * displays the mnemonic; only the state + file paths. Called by both the
 * `openclaw totalreclaw status` CLI subcommand and (if desired) the
 * `totalreclaw_status` tool.
 */
export function printStatus(
  credentialsPath: string,
  statePath: string,
  out: NodeJS.WritableStream,
): void {
  out.write(COPY.statusHeader);
  const credExists = fs.existsSync(credentialsPath);
  const stateFileExists = fs.existsSync(statePath);
  if (credExists) {
    const creds = loadCredentialsJson(credentialsPath);
    const hasMnemonic = typeof creds?.mnemonic === 'string' && creds.mnemonic.trim().length > 0;
    if (hasMnemonic) {
      out.write(COPY.statusActive);
      if (stateFileExists) {
        const st = loadOnboardingState(statePath);
        if (st?.credentialsCreatedAt) out.write(`  created:     ${st.credentialsCreatedAt}\n`);
        if (st?.createdBy) out.write(`  method:      ${st.createdBy}\n`);
      }
      return;
    }
  }
  out.write(COPY.statusFresh);
}

/**
 * Helper the `index.ts` registerCli hook calls to wire both subcommands
 * onto the OpenClaw program. Kept here so the wiring + the wizard live in
 * the same module — index.ts just forwards.
 *
 * `program` is the OpenClaw-provided commander Command. We attach a
 * top-level `totalreclaw` command with `onboard` + `status` subcommands.
 */
export function registerOnboardingCli(
  program: import('commander').Command,
  opts: { credentialsPath: string; statePath: string; logger: { info(msg: string): void; warn(msg: string): void; error(msg: string): void } },
): void {
  const tr = program
    .command('totalreclaw')
    .description('TotalReclaw encrypted memory — secure onboarding + status');

  tr.command('onboard')
    .description('Interactive onboarding: generate or import a recovery phrase (runs locally, no LLM)')
    .action(async () => {
      const io = buildDefaultIo();
      try {
        const result = await runOnboardingWizard({
          credentialsPath: opts.credentialsPath,
          statePath: opts.statePath,
          io,
        });
        if (result.error) {
          opts.logger.warn(`onboarding wizard exited with error: ${result.error}`);
          io.close();
          process.exit(1);
        }
        if (result.choice === 'generate' || result.choice === 'import') {
          opts.logger.info(`onboarding: state=active createdBy=${result.state?.createdBy}`);
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        opts.logger.error(`onboarding wizard crashed: ${msg}`);
        io.close();
        process.exit(2);
      }
      io.close();
    });

  tr.command('status')
    .description('Show TotalReclaw onboarding state — never displays the recovery phrase')
    .action(() => {
      printStatus(opts.credentialsPath, opts.statePath, process.stdout);
    });
}

// Ensure this module is reachable for import resolution in ESM tests.
export const __modulePath = (() => {
  try {
    return path.resolve(path.dirname(new URL(import.meta.url).pathname));
  } catch {
    return __dirname;
  }
})();
