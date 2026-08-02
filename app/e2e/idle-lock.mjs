/**
 * E2E: idle auto-lock (Option E Phase 1, #582, spa-passkey-unlock.md §7)
 * against STAGING, via a WebAuthn virtual authenticator with prf support.
 *
 * Uses Playwright's clock API to fast-forward past IDLE_LOCK_MS (30 min)
 * without actually waiting — the clock is installed before the app's first
 * script runs so the idle timer in CryptoContext is virtualized from the
 * start.
 *
 * L3 — phrase-safety: reads the generated phrase from the DOM only to
 * satisfy the backup-confirm gate. Never logs it. No screenshot of any
 * secret-bearing screen.
 *
 * Run: dev server up on :5173 (VITE_SERVER_URL=staging), then
 * `node e2e/idle-lock.mjs`.
 */
import { chromium } from "@playwright/test";

const BASE = "http://localhost:5173";
const out = (m) => console.log(`[e2e] ${m}`);
let failed = false;
function check(name, cond) {
  out(`${cond ? "PASS" : "FAIL"} — ${name}`);
  if (!cond) failed = true;
}

const browser = await chromium.launch();
const context = await browser.newContext();
const page = await context.newPage();

page.on("pageerror", (e) => out(`PAGEERROR: ${e.message}`));

// Install the virtual clock BEFORE any navigation so every setTimeout the
// app creates (including the idle-lock timer) is virtualized from the start.
await page.clock.install({ time: new Date() });

const cdp = await context.newCDPSession(page);
await cdp.send("WebAuthn.enable");
await cdp.send("WebAuthn.addVirtualAuthenticator", {
  options: {
    protocol: "ctap2",
    transport: "internal",
    hasResidentKey: true,
    hasUserVerification: true,
    hasPrf: true,
    automaticPresenceSimulation: true,
    isUserVerified: true,
  },
});

try {
  // --- Bootstrap a fresh vault (decline escrow — irrelevant to this test) ---
  await page.goto(`${BASE}/bootstrap`, { waitUntil: "domcontentloaded" });
  await page.getByText("Create a new vault", { exact: true }).waitFor({ timeout: 15000 });
  await page.getByText("Create a new vault", { exact: true }).click();
  await page.getByText("Write this down").waitFor({ timeout: 10000 });

  const words = await page.$$eval("ol li", (lis) =>
    lis.map((li) => li.querySelectorAll("span")[1]?.textContent?.trim() ?? ""),
  );
  await page.getByText("I’ve written it down").click();
  await page.getByText("Confirm your backup").waitFor({ timeout: 10000 });
  const labels = await page.$$("label");
  for (const label of labels) {
    const span = await label.$("span");
    const txt = (await span?.textContent()) ?? "";
    const m = txt.match(/#(\d+)/);
    if (!m) continue;
    const n = parseInt(m[1], 10);
    const input = await label.$("input");
    await input?.fill(words[n - 1]);
  }
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByText("Back up your recovery phrase?").waitFor({ timeout: 10000 });
  await page.getByRole("button", { name: "Not now" }).click();
  await page.waitForURL("**/memory", { timeout: 30000 });
  check("bootstrap landed on /memory (unlocked)", page.url().endsWith("/memory"));

  // --- Fast-forward 29 minutes: still unlocked (well under the 30m threshold) ---
  await page.clock.fastForward("29:00");
  await page.waitForTimeout(50); // let any pending microtasks settle
  check("still unlocked at 29 minutes idle", page.url().endsWith("/memory"));

  // --- Fast-forward past the 30-minute idle threshold ---
  await page.clock.fastForward("01:30");
  await page.waitForURL("**/unlock", { timeout: 10000 });
  check("idle-locked to /unlock after 30 minutes of inactivity", page.url().endsWith("/unlock"));

  // No decrypted vault content should remain in the DOM after lock — the
  // /unlock screen has no billing/vault-address strings a memory/settings
  // screen would.
  const bodyText = await page.evaluate(() => document.body.innerText);
  check(
    "no decrypted vault content remains in the DOM",
    !bodyText.includes("Memories this month") && !/0x[0-9a-fA-F]{10,}/.test(bodyText),
  );

  // sessionStorage was cleared by lock() — the tab no longer silently resumes.
  const sessionState = await page.evaluate(() => sessionStorage.length);
  check("sessionStorage cleared by lock()", sessionState === 0);

  // --- Confirm passkey unlock still works after an idle lock ---
  await page.getByRole("button", { name: "Unlock with passkey" }).click();
  await page.waitForURL("**/memory", { timeout: 20000 });
  check("passkey unlock works again after idle lock", page.url().endsWith("/memory"));
} catch (e) {
  out(`EXCEPTION: ${e.message}`);
  failed = true;
  try {
    await page.screenshot({ path: "e2e/shot-idle-lock-failure.png" });
  } catch {}
}

await browser.close();
out(failed ? "RESULT: FAIL" : "RESULT: PASS");
process.exit(failed ? 1 : 0);
