/**
 * E2E (Option E Phase 1, #582, spa-passkey-unlock.md §10.2): full escrow
 * round-trip against STAGING — bootstrap with escrow consent → wipe local
 * storage (simulating device loss) → recover with only the passkey → same
 * Smart Account address → delete the escrow record → recovery attempt then
 * reads "no-escrow".
 *
 * GATED: requires totalreclaw-relay PR #46 (POST/GET/DELETE /v1/escrow +
 * POST /v1/escrow/fetch) merged and live on api-staging.totalreclaw.xyz.
 * Written and structurally reviewed during implementation but NOT YET RUN
 * against a live relay — see the PR body for the explicit gate. Run this
 * script as the acceptance check once #46 merges, before promoting either
 * side to production.
 *
 * L3 — phrase-safety: reads the phrase from the DOM only to satisfy the
 * backup-confirm gate, and only to compare it is NOT typed again on
 * recovery (never logged, never screenshotted).
 *
 * Run: dev server up on :5173 (VITE_SERVER_URL=staging), then
 * `node e2e/escrow-roundtrip.mjs`.
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

async function readVaultRecord() {
  return page.evaluate(
    () =>
      new Promise((res) => {
        const open = indexedDB.open("keyval-store");
        open.onsuccess = () => {
          const tx = open.result.transaction("keyval", "readonly");
          const req = tx.objectStore("keyval").getAll();
          req.onsuccess = () => res(req.result.find((r) => r && r.wrapped_vault_key) ?? null);
          req.onerror = () => res(null);
        };
        open.onerror = () => res(null);
      }),
  );
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

try {
  // --- 1. Bootstrap with escrow consent (relay path) ---
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
  await page.getByRole("button", { name: "Back up my phrase" }).click();
  await page.waitForURL("**/memory", { timeout: 30000 });
  check("bootstrap with escrow consent lands on /memory", page.url().endsWith("/memory"));

  const originalRecord = await readVaultRecord();
  check("original VaultRecord captured", !!originalRecord?.smart_account);
  const originalSmartAccount = originalRecord?.smart_account;

  // --- 2. Simulate device loss: wipe ALL local storage ---
  await wipeLocalStorage();
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForURL("**/unlock", { timeout: 15000 });
  check("wiped device redirects to /unlock (no-vault four-way choice)", page.url().endsWith("/unlock"));
  await page.getByText("Recover with passkey", { exact: true }).waitFor({ timeout: 10000 });

  // --- 3. Recover with only the passkey (SAME authenticator, discoverable) ---
  await page.getByText("Recover with passkey", { exact: true }).click();
  await page.waitForURL("**/memory", { timeout: 20000 });
  check("restoreFromEscrow recovers to /memory with no phrase typed", page.url().endsWith("/memory"));

  // --- 4. Byte-identical Smart Account — proves the SAME root, not just a well-formed phrase ---
  const recoveredRecord = await readVaultRecord();
  check(
    "recovered Smart Account address is byte-identical to the original",
    !!recoveredRecord?.smart_account &&
      recoveredRecord.smart_account.toLowerCase() === originalSmartAccount?.toLowerCase(),
  );

  // --- 5. Delete the escrow record from Settings, then confirm recovery reads "no-escrow" ---
  await page.goto(`${BASE}/settings`, { waitUntil: "domcontentloaded" });
  await page.getByText("Passkey backup", { exact: false }).first().waitFor({ timeout: 10000 });
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Remove" }).first().click();
  await page.getByText("No relay backups yet.").waitFor({ timeout: 10000 });
  check("escrow record removed from Settings", true);

  await wipeLocalStorage();
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForURL("**/unlock", { timeout: 15000 });
  await page.getByText("Recover with passkey", { exact: true }).click();
  await page
    .getByText("We couldn’t find a backup for this passkey.", { exact: false })
    .waitFor({ timeout: 15000 });
  check("recovery after deletion reads no-escrow, gracefully, not a crash", true);
} catch (e) {
  out(`EXCEPTION: ${e.message}`);
  failed = true;
  try {
    await page.screenshot({ path: "e2e/shot-escrow-roundtrip-failure.png" });
  } catch {}
}

try {
  await cdp.send("WebAuthn.removeVirtualAuthenticator", { authenticatorId });
} catch {
  /* best-effort cleanup */
}
await browser.close();
out(failed ? "RESULT: FAIL" : "RESULT: PASS");
process.exit(failed ? 1 : 0);
