/**
 * pair-cli — the `openclaw totalreclaw pair` CLI subcommand.
 *
 * Purpose
 * -------
 * Starts a remote-onboarding session FROM the gateway host's terminal.
 * Creates a pair-session, renders the QR + URL + 6-digit secondary code
 * to stdout, then polls /status until the browser completes the flow.
 *
 * This is the gateway-operator's surface. The operator reads the QR
 * with their phone (or opens the URL on their laptop browser); the
 * browser takes over from there.
 *
 * Scope and scanner surface
 * -------------------------
 * Has `fetch` (for status polling) AND `POST` (never actually POSTs,
 * but the word lives in comments describing the paired browser POST).
 * MUST NOT also read disk or env vars. All state operations delegate
 * to pair-session-store; the CLI itself is a thin coordinator.
 *
 * Zero logging of secret material. The secondary code IS printed to
 * stdout (required for the user to type), but never logged to file
 * and never to api.logger.
 */

import readline from 'node:readline';

import {
  createPairSession,
  getPairSession,
  rejectPairSession,
  type PairSession,
  type PairSessionMode,
} from './pair-session-store.js';
import { generateGatewayKeypair } from './pair-crypto.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PairCliIo {
  stdout: NodeJS.WritableStream;
  stderr: NodeJS.WritableStream;
  /** Install a Ctrl+C handler that invokes `cb`; returns an uninstaller. */
  onInterrupt(cb: () => void): () => void;
}

export interface PairCliDeps {
  sessionsPath: string;
  /** Caller-injected function that returns the full `url#pk=` string
   *  for the browser. Takes the session and returns the URL with the
   *  public-key fragment embedded. Signature keeps URL resolution out
   *  of this module (same rationale as pair-http). */
  renderPairingUrl(session: PairSession): string;
  /** QR renderer — takes a text payload + callback. Injectable for tests. */
  renderQr(payload: string, cb: (ascii: string) => void): void;
  /** Poll interval in ms. Default 1500. */
  pollIntervalMs?: number;
  /** Override for Date.now(). */
  now?: () => number;
  io: PairCliIo;
  /** Optional TTL override (sec → ms conversion happens here). */
  ttlSeconds?: number;
}

export type PairCliMode = PairSessionMode;

export interface PairCliOutcome {
  status: 'completed' | 'canceled' | 'expired' | 'rejected' | 'error';
  sid?: string;
  error?: string;
}

// ---------------------------------------------------------------------------
// Default stdout IO
// ---------------------------------------------------------------------------

export function buildDefaultPairCliIo(): PairCliIo {
  return {
    stdout: process.stdout,
    stderr: process.stderr,
    onInterrupt(cb) {
      const handler = () => {
        try { cb(); } catch { /* swallow */ }
      };
      process.once('SIGINT', handler);
      return () => process.off('SIGINT', handler);
    },
  };
}

// ---------------------------------------------------------------------------
// Copy — same security principles as onboarding-cli COPY but terser.
// ---------------------------------------------------------------------------

const COPY = {
  intro:
    '\nTotalReclaw — Remote pairing\n\n' +
    'Your TotalReclaw account key will be created (or imported) in your\n' +
    'BROWSER and delivered to this gateway encrypted end-to-end. The key\n' +
    'never touches the LLM, the session transcript, or the relay server\n' +
    'in plaintext.\n\n' +
    'Scan the QR code below with your phone, or open the URL on any\n' +
    'device. Then type the 6-digit code shown here into the browser.\n',
  introGenerate:
    '\nMode: GENERATE — your browser will create a NEW 12-word recovery phrase.\n' +
    'You will be asked to write it down and retype 3 words before the\n' +
    'gateway accepts it.\n',
  introImport:
    '\nMode: IMPORT — your browser will accept an existing TotalReclaw\n' +
    'recovery phrase that you already have. Paste it in the browser; it\n' +
    'will be validated locally and encrypted before upload.\n',
  codeLabel: '\nSecondary code (type this into the browser):\n\n    ',
  urlLabel:
    '\n\nURL (QR encodes this plus a one-time public key):\n\n    ',
  securityWarning:
    '\n\nSecurity:\n' +
    '  * Do NOT share your screen during pairing.\n' +
    '  * Do NOT screenshot this terminal.\n' +
    '  * The browser page will warn you never to reuse this key for\n' +
    '    wallets, banking, email, or any other service.\n',
  awaiting: '\nWaiting for browser to connect… (press Ctrl+C to cancel)',
  deviceConnected: '\nBrowser connected. Waiting for encrypted payload…',
  completed: '\nPairing complete. Account is active.',
  canceled: '\nCanceled. Pairing session invalidated.',
  expired: '\nSession expired. Run the command again to restart.',
  rejected: '\nPairing rejected (too many wrong codes, or gateway aborted).',
};

function renderUnsafelyVisibleCode(code: string): string {
  // Pad digits with spaces so terminal copy-paste can't accidentally
  // pick them up as a single token.
  return code.split('').join(' ');
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

/**
 * Start a pairing session, display the QR + code + URL, and poll
 * until terminal state. Returns the final outcome.
 *
 * Blocks until the session finishes, expires, or the operator hits
 * Ctrl+C.
 */
export async function runPairCli(
  mode: PairCliMode,
  deps: PairCliDeps,
): Promise<PairCliOutcome> {
  const now = deps.now ?? Date.now;
  const pollInterval = Math.max(500, deps.pollIntervalMs ?? 1500);
  const io = deps.io;
  const stdout = io.stdout;

  // 1. Generate keypair + create the session
  const kp = generateGatewayKeypair();
  let session: PairSession;
  try {
    session = await createPairSession(deps.sessionsPath, {
      mode,
      operatorContext: { channel: 'cli' },
      ttlMs: deps.ttlSeconds !== undefined ? deps.ttlSeconds * 1000 : undefined,
      rngPrivateKey: () => Buffer.from(kp.skB64, 'base64url'),
      rngPublicKey: () => Buffer.from(kp.pkB64, 'base64url'),
      now,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    io.stderr.write(`\nFailed to create pairing session: ${msg}\n`);
    return { status: 'error', error: msg };
  }

  // 2. Render the QR + text
  const url = deps.renderPairingUrl(session);
  stdout.write(COPY.intro);
  stdout.write(mode === 'generate' ? COPY.introGenerate : COPY.introImport);
  await new Promise<void>((resolve) => {
    deps.renderQr(url, (ascii) => {
      stdout.write('\n' + ascii + '\n');
      resolve();
    });
  });
  stdout.write(COPY.codeLabel);
  stdout.write(renderUnsafelyVisibleCode(session.secondaryCode));
  stdout.write(COPY.urlLabel);
  stdout.write(url);
  stdout.write(COPY.securityWarning);
  stdout.write(COPY.awaiting);
  stdout.write('\n');

  // 3. Set up Ctrl+C to cancel the session server-side
  let canceled = false;
  const releaseInterrupt = io.onInterrupt(() => {
    canceled = true;
  });

  // 4. Poll
  let lastStatus = session.status;
  let showedDeviceConnected = false;
  try {
    while (true) {
      if (canceled) {
        await rejectPairSession(deps.sessionsPath, session.sid, now);
        stdout.write(COPY.canceled + '\n');
        return { status: 'canceled', sid: session.sid };
      }
      await sleep(pollInterval);
      const fresh = await getPairSession(deps.sessionsPath, session.sid, now);
      if (!fresh) {
        // Pruned — session is gone entirely.
        stdout.write(COPY.expired + '\n');
        return { status: 'expired', sid: session.sid };
      }
      if (fresh.status !== lastStatus) {
        lastStatus = fresh.status;
        if (fresh.status === 'device_connected' && !showedDeviceConnected) {
          stdout.write(COPY.deviceConnected + '\n');
          showedDeviceConnected = true;
        }
      }
      if (fresh.status === 'completed') {
        stdout.write(COPY.completed + '\n');
        return { status: 'completed', sid: session.sid };
      }
      if (fresh.status === 'expired') {
        stdout.write(COPY.expired + '\n');
        return { status: 'expired', sid: session.sid };
      }
      if (fresh.status === 'rejected') {
        stdout.write(COPY.rejected + '\n');
        return { status: 'rejected', sid: session.sid };
      }
    }
  } finally {
    releaseInterrupt();
  }
}

// ---------------------------------------------------------------------------
// Wrap qrcode-terminal in a promise-friendly renderer. Dynamic import
// keeps the module out of the plugin's register() hot path.
// ---------------------------------------------------------------------------

/**
 * Default QR renderer using `qrcode-terminal`. Lazy-imports so the
 * module only loads when the CLI is actually invoked.
 */
export function defaultRenderQr(payload: string, cb: (ascii: string) => void): void {
  // `qrcode-terminal` ships no type declarations; we describe the
  // public surface we rely on inline via a cast.
  type QrMod = {
    generate(text: string, opts: { small?: boolean }, cb: (ascii: string) => void): void;
  };
  import('qrcode-terminal' as string).then((rawMod: unknown) => {
    const mod = rawMod as { default?: QrMod } & QrMod;
    const qr: QrMod = mod.default ?? mod;
    qr.generate(payload, { small: true }, cb);
  }).catch((err: unknown) => {
    cb(`(QR renderer unavailable: ${err instanceof Error ? err.message : String(err)})`);
  });
}

// ---------------------------------------------------------------------------
// CLI registrar — hooked from `index.ts registerCli`.
// ---------------------------------------------------------------------------

/**
 * Register the `openclaw totalreclaw pair [generate|import]` subcommand
 * on the caller's commander program. The onboarding-cli's
 * `registerOnboardingCli` function already attaches `totalreclaw` as a
 * top-level command with `onboard`+`status` subcommands; we hook in by
 * finding that command and adding `pair` alongside.
 *
 * If the commander program is provided without the prior attachments,
 * we create `totalreclaw pair` fresh. The caller in index.ts decides
 * composition.
 */
/**
 * Minimal structural shape of commander's `Command` used by this file.
 * We don't import from `commander` because it's not a declared
 * dependency of the plugin (it's injected by OpenClaw's CLI runtime
 * at call time).
 */
type CommanderCommand = {
  name(): string;
  command(name: string): CommanderCommand;
  description(text: string): CommanderCommand;
  action(fn: (...args: unknown[]) => Promise<void> | void): CommanderCommand;
  commands: CommanderCommand[];
};

export function registerPairCli(
  program: CommanderCommand,
  deps: {
    sessionsPath: string;
    renderPairingUrl(session: PairSession): string;
    logger: { info(msg: string): void; warn(msg: string): void; error(msg: string): void };
  },
): void {
  // If the onboarding-cli already attached `totalreclaw`, reuse it.
  // Otherwise create a fresh top-level command.
  let tr: CommanderCommand | undefined = program.commands.find(
    (c: CommanderCommand) => c.name() === 'totalreclaw',
  );
  if (!tr) {
    tr = program
      .command('totalreclaw')
      .description('TotalReclaw encrypted memory — pairing + onboarding + status');
  }

  tr.command('pair [mode]')
    .description(
      'Pair a remote browser device to this gateway (mode = generate | import; default generate)',
    )
    .action(async (...args: unknown[]) => {
      const modeRaw = typeof args[0] === 'string' ? args[0] : undefined;
      const mode: PairCliMode =
        modeRaw === 'import' || modeRaw === 'imp' ? 'import' : 'generate';
      const io = buildDefaultPairCliIo();
      try {
        const outcome = await runPairCli(mode, {
          sessionsPath: deps.sessionsPath,
          renderPairingUrl: deps.renderPairingUrl,
          renderQr: defaultRenderQr,
          io,
        });
        if (outcome.status !== 'completed') {
          process.exit(outcome.status === 'canceled' ? 130 : 1);
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        deps.logger.error(`pair-cli crashed: ${msg}`);
        process.exit(2);
      }
    });
}

// ---------------------------------------------------------------------------
// Utils
// ---------------------------------------------------------------------------

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Keep readline import reachable (pair-cli doesn't use it directly yet,
// but future interactive prompts will land here; prevents tree-shaking
// from dropping a future dep). TypeScript requires the import to have
// an effect.
void readline;
