// scanner-sim: allow — dev-only browser test, excluded from the npm tarball by "!**/*.test.ts" in package.json "files"; it reads PLAYWRIGHT_* env vars and drives a local Chromium against a loopback server, and never reaches the OpenClaw runtime.
/**
 * Strict-CSP browser walk for the OpenClaw plugin's local pair page.
 *
 * Serves the page through the REAL route handlers (buildPairRoutes) so the
 * response headers are the real ones, loads it in headless Chromium and walks
 * the whole offline flow for both modes (code entry, generate + copy + retype
 * probes + submit, import + invalid phrase + submit, crypto failure). Asserts
 * zero `securitypolicyviolation` events and zero CSP console errors across the
 * whole walk. The ciphertext POST is intercepted in the browser and never
 * reaches the server; nothing pairs.
 *
 * Skipped (exit 0) when playwright-core or a Chromium build is unavailable.
 * Set PLAYWRIGHT_CHROMIUM_EXECUTABLE to point at a Chromium binary.
 *
 * Run with: npx tsx pairing/pair-page-csp-browser.test.ts
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import type { AddressInfo } from 'node:net';

import { createPairSession } from './pair-session-store.js';
import { generateGatewayKeypair } from './pair-crypto.js';
import { buildPairRoutes, type PairLogger } from './pair-http.js';

let passed = 0;
let failed = 0;
function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) { console.log(`ok ${n} - ${name}`); passed++; }
  else { console.log(`not ok ${n} - ${name}`); failed++; }
}

function findChromium(): string | null {
  const env = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  if (env && fs.existsSync(env)) return env;
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(os.homedir(), '.cache', 'ms-playwright');
  try {
    const dirs = fs.readdirSync(root).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse();
    for (const d of dirs) {
      const p = path.join(root, d, 'chrome-linux64', 'chrome');
      if (fs.existsSync(p)) return p;
    }
  } catch { /* no cache dir */ }
  return null;
}

const silent: PairLogger = { info() {}, warn() {}, error() {} };
const CODE = '482914';
// The public BIP-39 spec test vector (a documented dummy, never a real account).
const DUMMY_PHRASE = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

async function main(): Promise<void> {
  let chromium: any;
  try {
    ({ chromium } = await import('playwright-core'));
  } catch {
    console.log('# skipped: playwright-core not installed');
    return;
  }
  const exe = findChromium();
  if (!exe && !process.env.CI) {
    console.log('# skipped: no Chromium available');
    return;
  }

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-pair-csp-'));
  const sessionsPath = path.join(tmp, 'pair.json');
  const routes = buildPairRoutes({
    sessionsPath,
    apiBase: '/plugin/totalreclaw/pair',
    completePairing: async () => ({ state: 'active', accountId: 'acct_test' }),
    logger: silent,
  });
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', 'http://localhost');
    const h =
      url.pathname === routes.finishPath ? routes.handlers.finish
      : url.pathname === routes.startPath ? routes.handlers.start
      : url.pathname === routes.respondPath ? routes.handlers.respond
      : url.pathname === routes.statusPath ? routes.handlers.status
      : null;
    if (!h) { res.statusCode = 404; res.end('no route'); return; }
    await h(req, res);
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const browser = await chromium.launch({ executablePath: exe ?? undefined, args: ['--no-sandbox'] });

  async function open(mode: 'generate' | 'import', cryptoFail = false) {
    const kp = generateGatewayKeypair();
    const s = await createPairSession(sessionsPath, {
      mode,
      operatorContext: { channel: 'cli' },
      rngPrivateKey: () => Buffer.from(kp.skB64, 'base64url'),
      rngPublicKey: () => Buffer.from(kp.pkB64, 'base64url'),
      rngSecondaryCode: () => CODE,
    } as any);
    const ctx = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
    const page = await ctx.newPage();
    const violations: string[] = [];
    const errors: string[] = [];
    const posts: string[] = [];
    await page.exposeFunction('__reportViolation', (v: string) => violations.push(v));
    await page.addInitScript(() => {
      document.addEventListener('securitypolicyviolation', (e) => {
        (window as any).__reportViolation(`${e.violatedDirective} ${e.blockedURI} ${e.sample || ''}`);
      });
    });
    if (cryptoFail) {
      // A string script: tsx's keepNames would inject a helper into a function.
      await page.addInitScript(`(() => {
        const boom = () => { throw new Error('stubbed webcrypto failure'); };
        Object.defineProperty(window.crypto.subtle, 'generateKey', { value: boom });
        Object.defineProperty(window.crypto.subtle, 'deriveBits', { value: boom });
      })();`);
    }
    page.on('console', (m: any) => {
      if (/content security policy|trusted type|refused to/i.test(m.text())) errors.push(m.text());
    });
    page.on('pageerror', (e: Error) => errors.push('pageerror: ' + e.message));
    await page.route('**/plugin/totalreclaw/pair/respond', (route: any) => {
      posts.push(route.request().url());
      route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' });
    });
    await page.goto(`${origin}/plugin/totalreclaw/pair/finish?sid=${s.sid}#pk=${kp.pkB64}`);
    return { ctx, page, violations, errors, posts };
  }

  try {
    // ---- generate flow -------------------------------------------------
    {
      const r = await open('generate');
      await r.page.waitForSelector('#code-in');
      await r.page.fill('#code-in', 'abc12'); // non-digits stripped, too short
      await r.page.click('#code-continue');
      await r.page.waitForSelector('#code-error:not(.hidden)');
      await r.page.fill('#code-in', '000000'); // wrong code: server says 403
      await r.page.click('#code-continue');
      await r.page.waitForSelector('#code-error:not(.hidden)');
      await r.page.fill('#code-in', CODE);
      await r.page.click('#code-continue');
      await r.page.waitForSelector('.mnemonic-word');
      const delay = await r.page.evaluate(() => (document.querySelectorAll('.mnemonic-word')[3] as HTMLElement).style.animationDelay);
      assert(delay === '90ms', 'generate: stagger set through the CSSOM');
      await r.page.click('#copy');
      await r.page.waitForFunction(() => /Copied/.test(document.getElementById('copy')!.textContent || ''));
      // wrong probe first (error banner), then correct words read from the page
      await r.page.click('#ack-submit');
      await r.page.waitForSelector('#ack-error:not(.hidden)');
      const words: string[] = await r.page.evaluate(() =>
        Array.from(document.querySelectorAll('.mnemonic-word .word')).map((e) => e.textContent || ''));
      const inputs = await r.page.$$('#ack-probes input[data-idx]');
      for (const inp of inputs) {
        const idx = Number(await inp.getAttribute('data-idx'));
        await inp.fill(words[idx]);
      }
      await r.page.click('#ack-submit');
      await r.page.waitForFunction(() => /all set|paired|done|complete/i.test(document.body.innerText), null, { timeout: 15000 });
      assert(r.posts.length === 1, 'generate: one ciphertext POST (intercepted)');
      assert(r.violations.length === 0, `generate: zero CSP violations (${r.violations.join(' | ')})`);
      assert(r.errors.length === 0, `generate: zero CSP console errors (${r.errors.join(' | ')})`);
      await r.ctx.close();
    }

    // ---- import flow ---------------------------------------------------
    {
      const r = await open('import');
      await r.page.waitForSelector('#code-in');
      await r.page.keyboard.type(''); // no-op, keeps focus semantics simple
      await r.page.fill('#code-in', CODE);
      await r.page.press('#code-in', 'Enter');
      await r.page.waitForSelector('#phrase');
      await r.page.fill('#phrase', 'zoo zoo zoo');
      await r.page.click('#import-submit');
      await r.page.waitForSelector('#phrase-error:not(.hidden)');
      await r.page.fill('#phrase', DUMMY_PHRASE);
      await r.page.click('#import-submit');
      await r.page.waitForFunction(() => /all set|paired|done|complete/i.test(document.body.innerText), null, { timeout: 15000 });
      assert(r.posts.length === 1, 'import: one ciphertext POST (intercepted)');
      assert(r.violations.length === 0, `import: zero CSP violations (${r.violations.join(' | ')})`);
      assert(r.errors.length === 0, `import: zero CSP console errors (${r.errors.join(' | ')})`);
      await r.ctx.close();
    }

    // ---- crypto-failure path -------------------------------------------
    {
      const r = await open('generate', true);
      await r.page.waitForSelector('#code-in');
      await r.page.fill('#code-in', CODE);
      await r.page.click('#code-continue');
      await r.page.waitForSelector('.mnemonic-word, .callout.danger:not(.hidden)');
      // Fill the probes with arbitrary text; submit hits the stubbed failure.
      const words: string[] = await r.page.evaluate(() =>
        Array.from(document.querySelectorAll('.mnemonic-word .word')).map((e) => e.textContent || ''));
      const inputs = await r.page.$$('#ack-probes input[data-idx]');
      for (const inp of inputs) {
        const idx = Number(await inp.getAttribute('data-idx'));
        await inp.fill(words[idx] ?? '');
      }
      await r.page.click('#ack-submit');
      await r.page.waitForTimeout(500);
      assert(r.posts.length === 0, 'crypto failure: nothing was posted');
      assert(r.violations.length === 0, `crypto failure: zero CSP violations (${r.violations.join(' | ')})`);
      assert(r.errors.length === 0, `crypto failure: zero CSP console errors (${r.errors.join(' | ')})`);
      await r.ctx.close();
    }

    // ---- negative control: an injected inline script is blocked and reported
    {
      const r = await open('generate');
      await r.page.waitForSelector('#code-in');
      await r.page.evaluate(() => {
        const s = document.createElement('script');
        s.textContent = 'window.__pwned = 1';
        document.body.appendChild(s);
      });
      const pwned = await r.page.evaluate(() => (window as any).__pwned);
      await r.page.waitForTimeout(150);
      assert(pwned === undefined, 'negative control: injected inline script did not run');
      assert(r.violations.some((v) => v.startsWith('script-src')), 'negative control: violation was reported');
      await r.ctx.close();
    }
  } finally {
    await browser.close();
    await new Promise<void>((r) => server.close(() => r()));
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

main()
  .then(() => {
    console.log(`# ${passed} passed, ${failed} failed`);
    if (failed > 0) process.exit(1);
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
