/**
 * Tests for 3.3.1 pair-cli JSON mode.
 *
 * Covers:
 *   - outputMode: 'json' emits a single line of valid JSON to stdout
 *   - JSON payload contains sid, url, pin, qr_ascii, expires_at_ms
 *   - JSON mode never prints the human-readable intro / security warning
 *   - ttlSeconds is propagated to the pair session's expiresAtMs
 *
 * Run with: npx tsx pair-cli-json.test.ts
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  transitionPairSession,
  getPairSession,
} from './pair-session-store.js';
import { runPairCli, type PairCliIo, type PairCliJsonPayload } from './pair-cli.js';

let passed = 0;
let failed = 0;

function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) { console.log(`ok ${n} - ${name}`); passed++; }
  else { console.log(`not ok ${n} - ${name}`); failed++; }
}

function mkTmp(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'tr-pair-json-'));
}

class CaptureStream {
  buf: string[] = [];
  write(data: string | Uint8Array): boolean {
    this.buf.push(data.toString());
    return true;
  }
  end() { /* noop */ }
  text(): string { return this.buf.join(''); }
}

function buildTestIo(): { stdout: CaptureStream; stderr: CaptureStream; io: PairCliIo; triggerInterrupt: () => void } {
  const stdout = new CaptureStream();
  const stderr = new CaptureStream();
  let interruptCb: (() => void) | null = null;
  const io: PairCliIo = {
    stdout: stdout as unknown as NodeJS.WritableStream,
    stderr: stderr as unknown as NodeJS.WritableStream,
    onInterrupt(cb) {
      interruptCb = cb;
      return () => { interruptCb = null; };
    },
  };
  return {
    stdout,
    stderr,
    io,
    triggerInterrupt: () => { interruptCb?.(); },
  };
}

function mkFakeQrRenderer(): (payload: string, cb: (ascii: string) => void) => void {
  return (payload, cb) => cb(`[QR:${payload.length}]`);
}

// ---------------------------------------------------------------------------
// JSON mode emits a single well-formed JSON line
// ---------------------------------------------------------------------------

{
  const sessionsPath = path.join(mkTmp(), 'pair-sessions.json');
  const t = buildTestIo();

  // Start the CLI + immediately drive the session to 'completed' so it
  // terminates deterministically.
  const runPromise = runPairCli('generate', {
    sessionsPath,
    renderPairingUrl: (s) => `http://test/pair/finish?sid=${s.sid}#pk=${s.pkGatewayB64}`,
    renderQr: mkFakeQrRenderer(),
    pollIntervalMs: 50,
    io: t.io,
    outputMode: 'json',
    ttlSeconds: 600,
  });

  // The first emitted line should be JSON with the payload. Wait a tick,
  // then mark the session completed so the poll loop returns.
  await new Promise<void>((r) => setTimeout(r, 100));

  // Parse the first stdout chunk as JSON (the payload line).
  const firstOutput = t.stdout.text().split('\n')[0];
  let payload: PairCliJsonPayload | null = null;
  try {
    payload = JSON.parse(firstOutput) as PairCliJsonPayload;
  } catch {
    /* payload stays null */
  }
  assert(payload !== null, 'json: first stdout chunk parses as JSON');
  assert(payload?.v === 1, 'json: payload.v === 1');
  assert(typeof payload?.sid === 'string' && /^[0-9a-f]{32}$/.test(payload.sid), 'json: sid is 32-hex');
  assert(
    typeof payload?.url === 'string' && payload.url.startsWith('http://test/pair/finish?sid='),
    'json: url is the rendered pairing URL',
  );
  assert(typeof payload?.pin === 'string' && /^\d{6}$/.test(payload.pin), 'json: pin is 6 digits');
  assert(typeof payload?.expires_at_ms === 'number' && payload.expires_at_ms > Date.now(), 'json: expires_at_ms in future');
  assert(typeof payload?.qr_ascii === 'string' && payload.qr_ascii.includes('[QR:'), 'json: qr_ascii populated');
  assert(payload?.mode === 'generate', 'json: mode echoed');

  // ttlSeconds=600 means expiresAtMs - now() should be close to 600_000.
  const delta = (payload?.expires_at_ms ?? 0) - Date.now();
  assert(delta > 590_000 && delta < 610_000, `json: ttlSeconds=600 honoured (delta=${delta}ms)`);

  // Verify human-readable copy is NOT on stdout.
  assert(
    !t.stdout.text().includes('TotalReclaw — Remote pairing'),
    'json: no human-readable intro leaked to stdout',
  );
  assert(
    !t.stdout.text().includes('Security:'),
    'json: no security warning copy leaked to stdout',
  );

  // Drive the session to completed so runPairCli can return.
  await transitionPairSession(sessionsPath, payload!.sid, 'device_connected', Date.now);
  await transitionPairSession(sessionsPath, payload!.sid, 'consumed', Date.now);
  await transitionPairSession(sessionsPath, payload!.sid, 'completed', Date.now);

  const outcome = await runPromise;
  assert(outcome.status === 'completed', 'json: runPairCli returns completed');
}

// ---------------------------------------------------------------------------
// Human mode still emits the banner (regression guard — do not break pre-3.3.1 UX)
// ---------------------------------------------------------------------------

{
  const sessionsPath = path.join(mkTmp(), 'pair-sessions.json');
  const t = buildTestIo();
  const runPromise = runPairCli('generate', {
    sessionsPath,
    renderPairingUrl: (s) => `http://test/pair/finish?sid=${s.sid}`,
    renderQr: mkFakeQrRenderer(),
    pollIntervalMs: 50,
    io: t.io,
    outputMode: 'human',
  });
  await new Promise<void>((r) => setTimeout(r, 100));

  const text = t.stdout.text();
  assert(text.includes('TotalReclaw — Remote pairing'), 'human: intro copy present');
  assert(text.includes('Secondary code (type this into the browser):'), 'human: code label present');
  assert(text.includes('Security:'), 'human: security warning present');

  // Find session by reading sessions file.
  const raw = fs.readFileSync(sessionsPath, 'utf-8');
  const parsed = JSON.parse(raw) as { sessions?: Array<{ sid: string }> };
  const sid = parsed.sessions?.[0]?.sid;
  assert(typeof sid === 'string', 'human: session exists in store');

  // Cancel via Ctrl+C to terminate the poll loop.
  t.triggerInterrupt();
  await new Promise<void>((r) => setTimeout(r, 200));
  const outcome = await runPromise;
  assert(outcome.status === 'canceled', 'human: canceled outcome on interrupt');
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`# fail: ${failed}`);
console.log(`# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('SOME TESTS FAILED');
  process.exit(1);
}
console.log('ALL TESTS PASSED');
