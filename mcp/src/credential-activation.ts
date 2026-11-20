/**
 * Managed-mode credential activation (startup + hot reload).
 *
 * Until 3.6.1 the MCP server read credentials.json once, in `main()`, so a
 * pairing that finished while the host app was running left every memory tool
 * returning `not_configured` until the user quit and reopened the app. This
 * module holds the one activation routine both paths now share:
 *
 *   - `activate('startup')`: what `main()` always did. A credential that fails
 *     validation is FATAL (the error is rethrown, the process exits loudly).
 *   - Trigger A, `reloadAfterPair()`: `totalreclaw_pair` wrote the file in this
 *     process. Re-resolves from disk and, on a confirmed replace, swaps the
 *     running server to the new account.
 *   - Trigger B, `tryReload()`: called by the dispatcher on a tool call while
 *     the server is unconfigured. One `statSync`; activates only when the file
 *     appeared or changed since the last attempt (another process, e.g. Claude
 *     Code or Hermes, paired). Never swaps a configured server.
 *
 * Phrase safety: nothing here receives, holds or logs a recovery phrase. The
 * credential is always re-read from disk through `resolveCredential`, the same
 * validated path as startup. Log lines carry only the Smart Account address
 * (already logged at startup) and fixed text; error text from a failed
 * validation goes to stderr exactly as it did at startup and never into a tool
 * response.
 *
 * Self-hosted mode is out of scope: every reload entry point is a no-op there.
 */

import * as fs from 'node:fs';

import { UnsupportedCredentialsError, type ResolvedCredential } from './subgraph/credentials.js';
import type { UnusableCredentials } from './dispatch.js';

export type ActivationPhase = 'startup' | 'reload';

export interface ActivatorDeps<S> {
  /** credentials.json path; only `stat`ed here (to detect a change). */
  credentialsPath: string;
  /** TOTALRECLAW_SELF_HOSTED=true: reload is a no-op. */
  isSelfHosted: () => boolean;
  /** `resolveManagedCredential()` — the existing validated reader. */
  resolveCredential: () => ResolvedCredential | undefined;
  initFromBundle: (bundle: Extract<ResolvedCredential, { kind: 'bundle' }>['bundle']) => Promise<S>;
  initFromMnemonic: (mnemonic: string) => Promise<S>;
  /** True when a resolved mnemonic is valid for managed mode (`detectServerMode`). */
  isUsableMnemonic: (mnemonic: string) => boolean;
  /** Smart Account address of a state, for log lines. */
  ownerAddress: (state: S) => string;
  getState: () => S | null;
  /**
   * Install the result. `next === null` means unconfigured. `hadState` says a
   * configured state was running before, so the caller must reset every
   * per-account module value (the account may be changing).
   */
  commit: (next: S | null, info: { phase: ActivationPhase; hadState: boolean }) => void;
  getUnusable: () => UnusableCredentials | undefined;
  setUnusable: (u: UnusableCredentials | undefined) => void;
  /** stderr logger (default `console.error`). */
  log?: (msg: string) => void;
  /** Clock in ms (default `Date.now`); injected by tests. */
  now?: () => number;
}

/**
 * After an `activation_failed` (init threw, e.g. the relay is down), Trigger B
 * retries at most once per this interval. Each attempt can take many seconds
 * (billing lookup retries + register), so retrying on every tool call would
 * stall every call during an outage. A changed file (new signature) and a
 * finished pairing (Trigger A) run at once, regardless.
 */
export const ACTIVATION_RETRY_COOLDOWN_MS = 10_000;

export interface CredentialActivator {
  /** Startup activation; rethrows a fatal credential error. */
  activate(phase: 'startup'): Promise<void>;
  /** Trigger A. Never throws. */
  reloadAfterPair(): Promise<void>;
  /** Trigger B. Never throws. Resolves when no activation is running. */
  tryReload(): Promise<void>;
}

const INVALID_CREDENTIALS_MESSAGE =
  'The credentials file at ~/.totalreclaw/credentials.json could not be validated, so TotalReclaw ' +
  'did not load it (the reason is in the MCP server log). Nothing from the file is used and the ' +
  'file is untouched. Fixing or restoring the file, then calling any TotalReclaw tool again, ' +
  'retries; so does restarting the app.';

const ACTIVATION_FAILED_MESSAGE =
  'TotalReclaw found a credentials file but could not start with it just now (the reason is in the ' +
  'MCP server log). Nothing is stored or read until it works. Calling any TotalReclaw tool again ' +
  'after about 10 seconds retries; so does restarting the app.';

type FileSignature = string;

function fileSignature(credentialsPath: string): FileSignature {
  try {
    const st = fs.statSync(credentialsPath);
    return `${st.mtimeMs}:${st.size}`;
  } catch {
    return 'absent';
  }
}

export function createCredentialActivator<S>(deps: ActivatorDeps<S>): CredentialActivator {
  const log = deps.log ?? ((m: string) => console.error(m));
  const now = deps.now ?? (() => Date.now());

  /** Signature of credentials.json at the start of the last attempt. */
  let lastSignature: FileSignature | undefined;
  /** Time of the last `activation_failed` attempt; cleared on any other outcome. */
  let lastFailureAt: number | undefined;
  let inFlight: Promise<void> | null = null;
  let rerunRequested = false;

  async function activateOnce(phase: ActivationPhase): Promise<void> {
    const hadState = deps.getState() !== null;
    // Taken BEFORE reading, so a write that lands mid-activation is seen as a
    // change on the next call instead of being masked.
    lastSignature = fileSignature(deps.credentialsPath);
    lastFailureAt = undefined;

    const drop = (): void => {
      // A replace that cannot be activated must not leave the old account
      // running while credentials.json (shared with other clients) says
      // another one. Go unconfigured; a restart is the recovery.
      if (hadState) deps.commit(null, { phase, hadState });
    };

    let credential: ResolvedCredential | undefined;
    try {
      credential = deps.resolveCredential();
    } catch (err) {
      if (err instanceof UnsupportedCredentialsError) {
        // Not evidence of tampering: a credential from another client or a
        // newer version. Unconfigured, reason on every tool call. Nothing in
        // the file is used.
        deps.setUnusable({ reason: err.reason, message: err.message });
        drop();
        log(
          phase === 'startup'
            ? `TotalReclaw MCP server started (unconfigured): ${err.message}`
            : `TotalReclaw MCP: credentials reload: unconfigured: ${err.message}`,
        );
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      if (phase === 'startup') {
        // Loud, actionable, FATAL — never a silent downgrade (see
        // derived-bundle-v1.md §3.2). Behaviour unchanged from 3.6.0.
        log(`TotalReclaw MCP: fatal credential error: ${msg}`);
        throw err;
      }
      // Mid-session: do not crash the process, do not use anything from the
      // file, do not fall back to another source. Say why on every tool call.
      deps.setUnusable({ reason: 'invalid_credentials', message: INVALID_CREDENTIALS_MESSAGE });
      drop();
      log(`TotalReclaw MCP: credentials reload failed validation, staying unconfigured: ${msg}`);
      return;
    }

    try {
      let next: S | null = null;
      let how = '';
      if (credential?.kind === 'bundle') {
        next = await deps.initFromBundle(credential.bundle);
        how = 'managed service, bundle-mode';
      } else if (credential?.kind === 'mnemonic' && deps.isUsableMnemonic(credential.mnemonic)) {
        next = await deps.initFromMnemonic(credential.mnemonic);
        how = 'managed service';
      }

      if (next) {
        deps.setUnusable(undefined);
        deps.commit(next, { phase, hadState });
        log(
          phase === 'startup'
            ? `TotalReclaw MCP server started (${how}, owner: ${deps.ownerAddress(next)})`
            : `TotalReclaw MCP: credentials loaded without a restart (${how}, owner: ${deps.ownerAddress(next)})`,
        );
        return;
      }

      // Nothing usable. Keep a previously recorded unusable reason only at
      // startup (where it was just set above); on reload the file changed, so
      // the old reason no longer applies.
      deps.setUnusable(undefined);
      drop();
      if (phase === 'startup') {
        log(
          'TotalReclaw MCP server started (unconfigured — ask the assistant to run totalreclaw_pair to set up an account)',
        );
      }
    } catch (err) {
      if (phase === 'startup') throw err;
      const msg = err instanceof Error ? err.message : String(err);
      deps.setUnusable({ reason: 'activation_failed', message: ACTIVATION_FAILED_MESSAGE });
      drop();
      // Possibly transient (network): Trigger B retries after the cooldown.
      lastFailureAt = now();
      log(`TotalReclaw MCP: credentials reload could not start the account, staying unconfigured: ${msg}`);
    }
  }

  /** Single flight. `force` (Trigger A) re-runs once if a run is already going. */
  function run(phase: ActivationPhase, force: boolean): Promise<void> {
    if (inFlight) {
      if (force) rerunRequested = true;
      return inFlight;
    }
    const p = (async () => {
      try {
        do {
          rerunRequested = false;
          await activateOnce(phase);
        } while (rerunRequested);
      } finally {
        inFlight = null;
      }
    })();
    inFlight = p;
    return p;
  }

  return {
    activate(phase) {
      return run(phase, false);
    },

    async reloadAfterPair() {
      if (deps.isSelfHosted()) return;
      try {
        await run('reload', true);
      } catch (err) {
        log(`TotalReclaw MCP: credentials reload failed: ${err instanceof Error ? err.message : String(err)}`);
      }
    },

    async tryReload() {
      if (deps.isSelfHosted()) return;
      if (inFlight) {
        try { await inFlight; } catch { /* reported by the run itself */ }
        return;
      }
      // Trigger B never swaps a configured server.
      if (deps.getState() !== null) return;
      const sig = fileSignature(deps.credentialsPath);
      if (sig === 'absent') {
        // The file is gone: a stored reason or failure no longer applies.
        // Report plain not_configured.
        if (deps.getUnusable()) deps.setUnusable(undefined);
        lastFailureAt = undefined;
        lastSignature = sig;
        return;
      }
      if (sig === lastSignature) {
        const cooledDown =
          lastFailureAt !== undefined && now() - lastFailureAt >= ACTIVATION_RETRY_COOLDOWN_MS;
        if (!cooledDown) return;
      }
      try {
        await run('reload', false);
      } catch (err) {
        log(`TotalReclaw MCP: credentials reload failed: ${err instanceof Error ? err.message : String(err)}`);
      }
    },
  };
}
