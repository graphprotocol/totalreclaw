/**
 * cli-registercli-scope.test.ts (#402)
 *
 * Regression test for a ReferenceError that shipped in the published
 * 3.3.x line (present in rc.19 AND rc.20 dists — found by rc.20 auto-QA,
 * internal#434 / #402).
 *
 * THE BUG
 * -------
 * The `api.registerCli(async ({ program }) => { ... })` callback in index.ts
 * calls, in order:
 *   1. registerOnboardingCli(program, ...)  — declares its OWN local
 *      `const tr = program.command('totalreclaw')` inside onboarding-cli.ts,
 *   2. registerPairCli(program, ...)        — safely resolves `let tr =
 *      program.commands.find(c => c.name() === 'totalreclaw')`,
 *   3. then — DIRECTLY in the callback — the 3.3.13 import/upgrade wiring:
 *        const importCmd = tr.command('import')   // <- bare `tr`
 *        tr.command('upgrade')                     // <- bare `tr`
 *
 * `tr` is declared in NEITHER onboarding-cli's nor pair-cli's scope that the
 * callback can see — both are locals inside those helper functions. So the
 * bare `tr` in the callback is undeclared → `ReferenceError: tr is not
 * defined` the moment OpenClaw executes the callback → EVERY
 * `openclaw totalreclaw <sub>` (onboard/pair/status/import/upgrade) is dead.
 *
 * WHY IT SHIPPED
 * --------------
 * The build is `tsc --noCheck` (type-checking is skipped), so the undeclared
 * identifier sails through to dist untouched and only throws at runtime. And
 * the existing import-upgrade-cli.test.ts exercises the wiring via STATIC
 * regex over the source text — it never actually invokes the callback, so
 * the scope error was invisible to it.
 *
 * THIS TEST
 * ---------
 * Drives the REAL registerCli callback the way OpenClaw does:
 *   - Fake `api` (mirrors pair-http-route-registration.test.ts's buildMockApi,
 *     which is proven to run plugin.register() to completion) whose
 *     `registerCli` CAPTURES the callback instead of no-op'ing it.
 *   - Fake commander `Command` program (commander is an OpenClaw-provided
 *     runtime dep — NOT in the plugin's node_modules — so we mint a minimal
 *     stand-in exposing only the surface these paths touch: `command`,
 *     `description`, `option`, `argument`, `action`, `name`, `commands`).
 *   - Invoke the captured callback with `{ program }`.
 *
 * RED  (pre-fix): the callback REJECTS with `tr is not defined`.
 * GREEN (post-fix): the callback resolves and the `totalreclaw` command group
 *   has all five subcommands: onboard, status, pair, import, upgrade.
 *
 * Run with: `npx tsx cli-registercli-scope.test.ts`
 */

import plugin from '../index.js';

let passed = 0;
let failed = 0;

function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

// ---------------------------------------------------------------------------
// Minimal commander `Command` stand-in.
//
// commander is provided by OpenClaw at runtime (used only as a type in the
// plugin — `program: import('commander').Command`), so it is NOT installed in
// the plugin's node_modules and cannot be imported here. This fake exposes
// exactly the surface the three registration paths call:
//   - program.command(name, opts?)  → child Command (recorded in .commands)
//   - .description() / .option() / .argument() / .action()  → chainable (this)
//   - .name()  → first whitespace token of the declared name (commander
//                strips `[mode]` / `<source>` arg tokens from the name)
//   - .commands  → array of child Commands (what pair-cli's find() walks)
// ---------------------------------------------------------------------------

class FakeCommand {
  readonly rawName: string;
  readonly commands: FakeCommand[] = [];

  constructor(rawName = '<root>') {
    this.rawName = rawName;
  }

  name(): string {
    // commander's .name() is the declared name with arg tokens stripped:
    // 'pair [mode]' → 'pair'.
    return this.rawName.split(/\s+/)[0];
  }

  command(name: string, _opts?: unknown): FakeCommand {
    const child = new FakeCommand(name);
    this.commands.push(child);
    return child;
  }

  description(_d: string): this {
    return this;
  }

  option(..._args: unknown[]): this {
    return this;
  }

  argument(..._args: unknown[]): this {
    return this;
  }

  action(_fn: (...a: unknown[]) => unknown): this {
    return this;
  }
}

// ---------------------------------------------------------------------------
// Fake OpenClaw plugin API whose `registerCli` CAPTURES the callback.
// Everything else mirrors pair-http-route-registration.test.ts's buildMockApi
// (the known-good minimal surface that drives register() to completion).
// ---------------------------------------------------------------------------

type RegisterCliCallback = (ctx: { program: unknown }) => unknown;

function buildCapturingApi(): {
  api: unknown;
  getCallback: () => RegisterCliCallback | null;
} {
  let captured: RegisterCliCallback | null = null;
  const noop = (): void => {};
  const logger = {
    info: noop,
    warn: noop,
    error: noop,
    debug: noop,
  };
  const api = {
    logger,
    config: {
      agents: { defaults: { model: { primary: undefined as string | undefined } } },
      models: { providers: {} },
    },
    pluginConfig: {},
    registerTool: noop,
    registerService: noop,
    on: noop,
    registerCommand: noop,
    registerHttpRoute: noop,
    registerCli: (cb: RegisterCliCallback, _opts?: unknown): void => {
      captured = cb;
    },
  };
  return { api, getCallback: () => captured };
}

// ---------------------------------------------------------------------------
// Drive it.
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const { api, getCallback } = buildCapturingApi();

  // register() is synchronous; the pair-http test proves this minimal api
  // drives it to completion. It calls api.registerCli(cb, ...) → we capture cb.
  plugin.register(api as Parameters<typeof plugin.register>[0]);

  const cb = getCallback();
  assert(typeof cb === 'function', 'register() called api.registerCli with a callback');

  if (typeof cb !== 'function') {
    console.log(`\n# ${passed} passed, ${failed} failed`);
    process.exit(1);
  }

  const program = new FakeCommand();

  // Invoke the callback exactly as OpenClaw would: `cb({ program })`.
  // PRE-FIX: rejects with `ReferenceError: tr is not defined` when the
  //          import/upgrade wiring dereferences the undeclared `tr`.
  // POST-FIX: resolves cleanly.
  let threw = false;
  let errMessage = '';
  try {
    await cb({ program });
  } catch (err) {
    threw = true;
    errMessage = err instanceof Error ? err.message : String(err);
  }

  assert(
    !threw,
    threw
      ? `registerCli callback resolves (regressed: ${errMessage})`
      : 'registerCli callback resolves without throwing',
  );

  // The specific pre-fix failure fingerprint — a bare undeclared `tr`.
  assert(
    !/\btr is not defined\b/.test(errMessage),
    "registerCli callback does NOT throw 'tr is not defined' (the #402 ReferenceError)",
  );

  // Post-fix: the `totalreclaw` command group must exist with all five
  // subcommands wired. This is what a fresh user's `openclaw totalreclaw ...`
  // depends on.
  const group = (program.commands as FakeCommand[]).find((c) => c.name() === 'totalreclaw');
  assert(group !== undefined, 'a `totalreclaw` command group is registered on the program');

  if (group) {
    const subNames = new Set(group.commands.map((c) => c.name()));
    for (const expected of ['onboard', 'status', 'pair', 'import', 'upgrade']) {
      assert(
        subNames.has(expected),
        `totalreclaw group wires the '${expected}' subcommand`,
      );
    }
  } else {
    // Group missing (pre-fix path aborts before/at the import wiring) — mark
    // the five subcommand checks as failures so the count is stable.
    for (const expected of ['onboard', 'status', 'pair', 'import', 'upgrade']) {
      assert(false, `totalreclaw group wires the '${expected}' subcommand`);
    }
  }

  console.log(`\n# ${passed} passed, ${failed} failed`);
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error('test harness error:', err);
  process.exit(1);
});
