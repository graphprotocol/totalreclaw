/**
 * @jest-environment node
 *
 * NanoClaw 3.1.1 first-run onboarding tests.
 *
 * Covers:
 *   - detectFirstRun: missing / empty / invalid / partial / valid credentials
 *   - buildWelcomeMessage: contains brand + recovery-phrase terminology
 *   - Terminology parity: no legacy `seed phrase` / `recovery code` /
 *     `recovery key` leak in user-facing strings
 *   - Session-scoped sentinel: welcome emits at most once per process
 *   - SessionStart source filter: `compact` does not trigger welcome
 *
 * Module under test is TypeScript (`src/onboarding/first-run.ts`) — we load
 * the compiled output from `dist/`. Running `npm run build` before `npm test`
 * is part of the normal publish pipeline.
 */

const fs = require('fs');
const path = require('path');
const os = require('os');

describe('src/onboarding/first-run — module exports', () => {
  let onboarding;

  beforeAll(() => {
    // Compiled output — `npm run build` must have run at least once.
    onboarding = require(path.join(__dirname, '..', 'dist', 'onboarding', 'first-run.js'));
  });

  it('exports the canonical welcome copy constants', () => {
    expect(typeof onboarding.WELCOME_MESSAGE).toBe('string');
    expect(onboarding.WELCOME_MESSAGE).toContain('Welcome to TotalReclaw');
    expect(onboarding.WELCOME_MESSAGE).toContain('encrypted');
    expect(onboarding.WELCOME_MESSAGE).toContain('recovery phrase');

    expect(typeof onboarding.BRANCH_QUESTION).toBe('string');
    expect(onboarding.BRANCH_QUESTION).toContain('recovery phrase');

    expect(typeof onboarding.NANOCLAW_INSTRUCTIONS).toBe('string');
    expect(onboarding.NANOCLAW_INSTRUCTIONS).toContain('BIP39');
    expect(onboarding.NANOCLAW_INSTRUCTIONS).toContain('credentials.json');

    expect(typeof onboarding.STORAGE_GUIDANCE).toBe('string');
    expect(onboarding.STORAGE_GUIDANCE).toContain('12 words');
  });

  it('cross-client welcome message matches the shared contract', () => {
    // Contract — these exact phrases are shipped by plugin 3.3.0 + Hermes 2.3.1.
    expect(onboarding.WELCOME_MESSAGE).toContain(
      'Welcome to TotalReclaw — encrypted, agent-portable memory.'
    );
    expect(onboarding.BRANCH_QUESTION).toContain(
      'Do you already have a recovery phrase, or should we generate a new one?'
    );
  });
});

describe('detectFirstRun', () => {
  let onboarding;
  let tmpDir;

  beforeAll(() => {
    onboarding = require(path.join(__dirname, '..', 'dist', 'onboarding', 'first-run.js'));
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-onboarding-'));
  });

  afterAll(() => {
    try {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    } catch { /* best-effort */ }
  });

  it('returns true when the credentials file is missing', async () => {
    const missingPath = path.join(tmpDir, 'nonexistent.json');
    await expect(onboarding.detectFirstRun(missingPath)).resolves.toBe(true);
  });

  it('returns true when the credentials file is empty', async () => {
    const emptyPath = path.join(tmpDir, 'empty.json');
    fs.writeFileSync(emptyPath, '', 'utf-8');
    await expect(onboarding.detectFirstRun(emptyPath)).resolves.toBe(true);
  });

  it('returns true when the credentials file contains only whitespace', async () => {
    const wsPath = path.join(tmpDir, 'whitespace.json');
    fs.writeFileSync(wsPath, '   \n\n\t   ', 'utf-8');
    await expect(onboarding.detectFirstRun(wsPath)).resolves.toBe(true);
  });

  it('returns true when the credentials file is invalid JSON', async () => {
    const badPath = path.join(tmpDir, 'bad.json');
    fs.writeFileSync(badPath, 'not json {{{', 'utf-8');
    await expect(onboarding.detectFirstRun(badPath)).resolves.toBe(true);
  });

  it('returns true when credentials JSON lacks the mnemonic field', async () => {
    const noMnemPath = path.join(tmpDir, 'no-mnem.json');
    fs.writeFileSync(noMnemPath, JSON.stringify({ scope_address: '0xabc' }), 'utf-8');
    await expect(onboarding.detectFirstRun(noMnemPath)).resolves.toBe(true);
  });

  it('returns true when mnemonic is empty string', async () => {
    const emptyMnemPath = path.join(tmpDir, 'empty-mnem.json');
    fs.writeFileSync(emptyMnemPath, JSON.stringify({ mnemonic: '' }), 'utf-8');
    await expect(onboarding.detectFirstRun(emptyMnemPath)).resolves.toBe(true);
  });

  it('returns true when mnemonic has wrong word count', async () => {
    const shortPath = path.join(tmpDir, 'short-mnem.json');
    fs.writeFileSync(shortPath, JSON.stringify({ mnemonic: 'one two three' }), 'utf-8');
    await expect(onboarding.detectFirstRun(shortPath)).resolves.toBe(true);
  });

  it('returns false for a structurally valid 12-word credentials file', async () => {
    const validPath = path.join(tmpDir, 'valid.json');
    // 12 placeholder words — no BIP-39 checksum check at this layer
    // (deep validation is @totalreclaw/core's job at key derivation).
    const mnemonic = 'abandon ability able about above absent absorb abstract absurd abuse access accident';
    fs.writeFileSync(
      validPath,
      JSON.stringify({ mnemonic, scope_address: '0x1234567890abcdef1234567890abcdef12345678' }),
      'utf-8',
    );
    await expect(onboarding.detectFirstRun(validPath)).resolves.toBe(false);
  });

  it('returns false for a 24-word credentials file', async () => {
    const validPath = path.join(tmpDir, 'valid-24.json');
    const mnemonic = Array(24).fill('abandon').join(' ');
    fs.writeFileSync(validPath, JSON.stringify({ mnemonic }), 'utf-8');
    await expect(onboarding.detectFirstRun(validPath)).resolves.toBe(false);
  });

  it('returns true when the mnemonic is non-string garbage', async () => {
    const garbagePath = path.join(tmpDir, 'garbage.json');
    fs.writeFileSync(garbagePath, JSON.stringify({ mnemonic: 12345 }), 'utf-8');
    await expect(onboarding.detectFirstRun(garbagePath)).resolves.toBe(true);
  });
});

describe('buildWelcomeMessage', () => {
  let onboarding;

  beforeAll(() => {
    onboarding = require(path.join(__dirname, '..', 'dist', 'onboarding', 'first-run.js'));
  });

  it('includes all four sections (welcome, branch, instructions, guidance)', () => {
    const msg = onboarding.buildWelcomeMessage();
    expect(msg).toContain('Welcome to TotalReclaw');
    expect(msg).toContain('Do you already have a recovery phrase');
    expect(msg).toContain('BIP39');
    expect(msg).toContain('Store it somewhere safe');
  });

  it('uses "recovery phrase" terminology exclusively', () => {
    const msg = onboarding.buildWelcomeMessage();
    // Must be present
    expect(msg).toMatch(/recovery phrase/i);
    // Must NOT use legacy terms in the rendered welcome
    expect(msg).not.toMatch(/\bseed phrase\b/i);
    expect(msg).not.toMatch(/\brecovery code\b/i);
    expect(msg).not.toMatch(/\brecovery key\b/i);
  });

  it('mentions cross-client portability (OpenClaw + Hermes + NanoClaw)', () => {
    const msg = onboarding.buildWelcomeMessage();
    expect(msg).toContain('OpenClaw');
    expect(msg).toContain('Hermes');
    expect(msg).toContain('NanoClaw');
  });

  it('documents the NanoClaw-specific "no wizard" path', () => {
    const msg = onboarding.buildWelcomeMessage();
    expect(msg).toContain('credentials.json');
    expect(msg).toContain('OpenClaw or Hermes CLI');
  });
});

describe('maybeBuildFirstRunContext', () => {
  let onboarding;
  let tmpDir;

  beforeAll(() => {
    onboarding = require(path.join(__dirname, '..', 'dist', 'onboarding', 'first-run.js'));
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-onboarding-hook-'));
  });

  afterAll(() => {
    try {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    } catch { /* best-effort */ }
  });

  beforeEach(() => {
    onboarding._resetWelcomeSentinel();
  });

  it('returns welcome when credentials missing on startup', async () => {
    const missing = path.join(tmpDir, 'missing.json');
    const result = await onboarding.maybeBuildFirstRunContext({
      credentialsPath: missing,
      source: 'startup',
    });
    expect(typeof result).toBe('string');
    expect(result).toContain('Welcome to TotalReclaw');
  });

  it('returns undefined when credentials are valid', async () => {
    const validPath = path.join(tmpDir, 'valid-hook.json');
    const mnemonic = 'abandon ability able about above absent absorb abstract absurd abuse access accident';
    fs.writeFileSync(validPath, JSON.stringify({ mnemonic }), 'utf-8');
    const result = await onboarding.maybeBuildFirstRunContext({
      credentialsPath: validPath,
      source: 'startup',
    });
    expect(result).toBeUndefined();
  });

  it('emits welcome at most once per process (session sentinel)', async () => {
    const missing = path.join(tmpDir, 'missing-sentinel.json');
    const first = await onboarding.maybeBuildFirstRunContext({
      credentialsPath: missing,
      source: 'startup',
    });
    expect(typeof first).toBe('string');

    const second = await onboarding.maybeBuildFirstRunContext({
      credentialsPath: missing,
      source: 'startup',
    });
    expect(second).toBeUndefined();
  });

  it('skips compaction events (never re-inject on mid-session compact)', async () => {
    const missing = path.join(tmpDir, 'missing-compact.json');
    const result = await onboarding.maybeBuildFirstRunContext({
      credentialsPath: missing,
      source: 'compact',
    });
    expect(result).toBeUndefined();
  });

  it('emits on resume if credentials still missing (first-contact on new resume)', async () => {
    // Resume is treated as a startup-like event for first-run purposes —
    // if credentials never arrived, the user needs to see the welcome.
    const missing = path.join(tmpDir, 'missing-resume.json');
    const result = await onboarding.maybeBuildFirstRunContext({
      credentialsPath: missing,
      source: 'resume',
    });
    expect(typeof result).toBe('string');
  });
});

describe('resolveCredentialsPath', () => {
  let onboarding;
  const originalEnv = { ...process.env };

  beforeAll(() => {
    onboarding = require(path.join(__dirname, '..', 'dist', 'onboarding', 'first-run.js'));
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it('honours TOTALRECLAW_CREDENTIALS_PATH override', () => {
    process.env.TOTALRECLAW_CREDENTIALS_PATH = '/custom/path/creds.json';
    expect(onboarding.resolveCredentialsPath()).toBe('/custom/path/creds.json');
  });

  it('falls back to WORKSPACE_DIR/.totalreclaw/credentials.json', () => {
    delete process.env.TOTALRECLAW_CREDENTIALS_PATH;
    process.env.WORKSPACE_DIR = '/workspace';
    expect(onboarding.resolveCredentialsPath()).toBe('/workspace/.totalreclaw/credentials.json');
  });

  it('falls back to HOME/.totalreclaw/credentials.json when no workspace', () => {
    delete process.env.TOTALRECLAW_CREDENTIALS_PATH;
    delete process.env.WORKSPACE_DIR;
    process.env.HOME = '/home/someone';
    expect(onboarding.resolveCredentialsPath()).toBe('/home/someone/.totalreclaw/credentials.json');
  });
});

describe('Terminology parity — no legacy phrase leaks in user-facing strings', () => {
  it('src/onboarding/first-run.ts has no legacy terms in string literals', () => {
    const src = fs.readFileSync(
      path.join(__dirname, '..', 'src', 'onboarding', 'first-run.ts'),
      'utf-8',
    );
    // Extract only the quoted strings (rough but effective for this scale).
    // We DO allow "mnemonic" inside JSON examples and field-name mentions —
    // the credentials file still uses `mnemonic` as its field name, and
    // the welcome message literally references that field by name.
    const stringLiterals = src.match(/['"`]([^'"`]*)['"`]/g) || [];
    const joined = stringLiterals.join(' ');

    // Forbidden legacy terms — checked as whole words, case-insensitive.
    expect(joined).not.toMatch(/\bseed phrase\b/i);
    expect(joined).not.toMatch(/\brecovery code\b/i);
    expect(joined).not.toMatch(/\brecovery key\b/i);
  });

  it('mcp/nanoclaw-agent-runner.ts first-run welcome uses recovery phrase terminology', () => {
    const runner = fs.readFileSync(
      path.join(__dirname, '..', 'mcp', 'nanoclaw-agent-runner.ts'),
      'utf-8',
    );
    // The inlined welcome block
    const welcomeBlock = runner.substring(
      runner.indexOf('FIRST_RUN_WELCOME'),
      runner.indexOf('let firstRunWelcomeEmitted'),
    );
    expect(welcomeBlock).toContain('recovery phrase');
    expect(welcomeBlock).not.toMatch(/\bseed phrase\b/i);
    expect(welcomeBlock).not.toMatch(/\brecovery code\b/i);
    expect(welcomeBlock).not.toMatch(/\brecovery key\b/i);
  });

  it('SKILL.md uses recovery-phrase terminology', () => {
    const skillMd = fs.readFileSync(path.join(__dirname, '..', 'SKILL.md'), 'utf-8');
    expect(skillMd).not.toMatch(/\bseed phrase\b/i);
    expect(skillMd).not.toMatch(/\brecovery code\b/i);
    expect(skillMd).not.toMatch(/\brecovery key\b/i);
  });

  it('CHANGELOG.md uses recovery-phrase terminology in shipped copy', () => {
    const changelog = fs.readFileSync(path.join(__dirname, '..', 'CHANGELOG.md'), 'utf-8');
    expect(changelog).not.toMatch(/\bseed phrase\b/i);
    expect(changelog).not.toMatch(/\brecovery code\b/i);
    expect(changelog).not.toMatch(/\brecovery key\b/i);
  });

  it('README.md uses recovery-phrase terminology', () => {
    const readme = fs.readFileSync(path.join(__dirname, '..', 'README.md'), 'utf-8');
    expect(readme).not.toMatch(/\bseed phrase\b/i);
    expect(readme).not.toMatch(/\brecovery code\b/i);
    expect(readme).not.toMatch(/\brecovery key\b/i);
  });
});

describe('Runner integration — SessionStart hook is wired', () => {
  let runner;

  beforeAll(() => {
    runner = fs.readFileSync(
      path.join(__dirname, '..', 'mcp', 'nanoclaw-agent-runner.ts'),
      'utf-8',
    );
  });

  it('imports SessionStartHookInput from the SDK', () => {
    expect(runner).toMatch(/SessionStartHookInput/);
  });

  it('registers a SessionStart hook in the query() hooks config', () => {
    expect(runner).toMatch(/SessionStart:\s*\[\s*\{/);
  });

  it('invokes createFirstRunHook with the credentials path', () => {
    expect(runner).toMatch(/createFirstRunHook\(/);
  });

  it('emits hookEventName: SessionStart in the hook output', () => {
    expect(runner).toMatch(/hookEventName:\s*['"]SessionStart['"]/);
  });

  it('gates welcome emission on source === "startup"', () => {
    expect(runner).toMatch(/source\s*!==\s*['"]startup['"]/);
  });
});
