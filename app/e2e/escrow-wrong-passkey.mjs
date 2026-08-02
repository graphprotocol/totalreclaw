/**
 * E2E (Option E Phase 1, #582, spa-passkey-unlock.md §10.2): enrol
 * authenticator A (with escrow), lose the device, attempt recovery with a
 * DIFFERENT passkey (authenticator B) → "no-escrow" state, phrase entry
 * offered, no crash.
 *
 * GATED: requires totalreclaw-relay PR #46 live on staging (same gate as
 * escrow-roundtrip.mjs). Written and structurally reviewed during
 * implementation but NOT YET RUN against a live relay.
 *
 * Uses two separate CDP virtual authenticators so the platform can
 * complete a real (but PRF-mismatched) assertion rather than failing with
 * "no credential available" — that failure mode is a DIFFERENT UI state
 * (mapUnlockError's "cancelled"/no-credential path) than the one this test
 * targets. Authenticator A is removed before the recovery attempt so the
 * discoverable assertion resolves unambiguously to B's credential.
 *
 * Run: dev server up on :5173 (VITE_SERVER_URL=staging), then
 * `node e2e/escrow-wrong-passkey.mjs`.
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

const cdp = await context.newCDPSession(page);
await cdp.send("WebAuthn.enable");

async function addAuthenticator() {
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", {
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
  return authenticatorId;
}

async function wipeLocalStorage() {
  await page.evaluate(
    () =>
      new Promise((res) => {
        localStorage.clear();
        sessionStorage.clear();
        const del = indexedDB.deleteDatabase("keyval-store");
        del.onsuccess = () => res();
        del.onerror = () => res();
        del.onblocked = () => res();
      }),
  );
}

async function bootstrapVault({ escrow }) {
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
  await page.getByRole("button", { name: escrow ? "Back up my phrase" : "Not now" }).click();
  await page.waitForURL("**/memory", { timeout: 30000 });
}

try {
  // --- Authenticator A: bootstrap vault 1 WITH escrow ---
  const authA = await addAuthenticator();
  await bootstrapVault({ escrow: true });
  check("vault A bootstrapped with escrow enabled", page.url().endsWith("/memory"));

  // --- Authenticator B: bootstrap a SEPARATE vault (gives B its own resident credential on this rp.id) ---
  await wipeLocalStorage();
  // Chrome only supports one "internal"-transport virtual authenticator at a
  // time — remove A BEFORE adding B, not after (an earlier version of this
  // script had them attached simultaneously and hit
  // "WebAuthn.addVirtualAuthenticator: Chrome only supports one internal
  // authenticator per environment"). This also serves the test's own intent:
  // it must resolve unambiguously to B's credential.
  await cdp.send("WebAuthn.removeVirtualAuthenticator", { authenticatorId: authA });
  const authB = await addAuthenticator();
  await bootstrapVault({ escrow: false }); // vault B declines escrow — irrelevant here
  check("vault B bootstrapped with its own passkey (authenticator B)", page.url().endsWith("/memory"));

  // --- Simulate device loss, then attempt recovery — the ONLY attached
  //     authenticator now is B, whose PRF was never used to seal vault A's
  //     escrow record. ---
  await wipeLocalStorage();
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForURL("**/unlock", { timeout: 15000 });
  await page.getByText("Recover with passkey", { exact: true }).click();

  await page
    .getByText("We couldn’t find a backup for this passkey.", { exact: false })
    .waitFor({ timeout: 15000 });
  check('recovery with the WRONG passkey reads "no-escrow", not a crash or a silent wrong unlock', true);

  // Phrase entry must still be reachable from here.
  await page.getByText("Use recovery phrase instead", { exact: true }).waitFor({ timeout: 5000 });
  check("phrase entry is offered as the forward path", true);

  void authB;
} catch (e) {
  out(`EXCEPTION: ${e.message}`);
  failed = true;
  try {
    await page.screenshot({ path: "e2e/shot-escrow-wrong-passkey-failure.png" });
  } catch {}
}

await browser.close();
out(failed ? "RESULT: FAIL" : "RESULT: PASS");
process.exit(failed ? 1 : 0);
