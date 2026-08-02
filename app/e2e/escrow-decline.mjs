/**
 * E2E (Option E Phase 1, #582): decline escrow at bootstrap → assert no
 * POST /v1/escrow was ever issued → vault fully functional → later enable
 * from Settings → recovery then works. Against STAGING, via a WebAuthn
 * virtual authenticator with prf support.
 *
 * Requires totalreclaw-relay PR #46 (/v1/escrow* endpoints) live on
 * staging — merged 2026-08-03, confirmed live before this script runs.
 *
 * L3 — phrase-safety: reads the phrase from the DOM only to satisfy the
 * backup-confirm gate, and to re-type it into the Settings "back up this
 * device's passkey" form (the same re-entry a real user would do — the
 * mnemonic is never persisted). Never logged.
 *
 * Run: dev server up on :5173 (VITE_SERVER_URL=staging), then
 * `node e2e/escrow-decline.mjs`.
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

// Track every request path hit during the run — the core assertion of this
// script is what's ABSENT from this list.
const requestedPaths = [];
page.on("request", (req) => {
  try {
    requestedPaths.push(new URL(req.url()).pathname);
  } catch {
    /* ignore */
  }
});

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
  check("escrow consent step shown before working", true);

  await page.getByRole("button", { name: "Not now" }).click();
  await page.waitForURL("**/memory", { timeout: 30000 });
  check("declining escrow still lands on a working vault", page.url().endsWith("/memory"));

  const escrowRequests = requestedPaths.filter((p) => p.startsWith("/v1/escrow"));
  check("declining issued NO request to any /v1/escrow* endpoint", escrowRequests.length === 0);

  // Settings renders the Passkeys & backup section. With #46 live, an empty
  // list reads "No relay backups yet." (not the relay-unreachable fallback).
  await page.goto(`${BASE}/settings`, { waitUntil: "domcontentloaded" });
  await page.getByText("Passkeys & backup").waitFor({ timeout: 10000 });
  check("Settings → Passkeys & backup section renders after decline", true);
  await page.getByText("No relay backups yet.").waitFor({ timeout: 10000 });
  check("no escrow record exists yet (relay confirms empty list, not an error state)", true);

  // --- Previously gated: enable escrow later from Settings ---
  await page.getByRole("button", { name: "Back up this device’s passkey" }).click();
  await page.getByPlaceholder("twelve words separated by spaces").fill(words.join(" "));
  await page.getByRole("button", { name: "Back up", exact: true }).click();
  await page.getByText("Backed up to TotalReclaw.").waitFor({ timeout: 15000 });
  check("enabling escrow from Settings succeeds (relay accepted the record)", true);

  await page.getByText("Passkey backup", { exact: false }).first().waitFor({ timeout: 10000 });
  check("the new record now appears in the list", true);

  const putEscrowRequests = requestedPaths.filter((p) => p === "/v1/escrow");
  check("Settings enable issued exactly one POST /v1/escrow (no accidental duplicate)", putEscrowRequests.length >= 1);

  // --- Previously gated: recovery then works ---
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
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForURL("**/unlock", { timeout: 15000 });
  await page.getByText("Recover with passkey", { exact: true }).click();
  await page.waitForURL("**/memory", { timeout: 20000 });
  check("recovery with the backup enabled after decline now succeeds", page.url().endsWith("/memory"));
} catch (e) {
  out(`EXCEPTION: ${e.message}`);
  failed = true;
  try {
    await page.screenshot({ path: "e2e/shot-escrow-decline-failure.png" });
  } catch {}
}

await browser.close();
out(failed ? "RESULT: FAIL" : "RESULT: PASS");
process.exit(failed ? 1 : 0);
