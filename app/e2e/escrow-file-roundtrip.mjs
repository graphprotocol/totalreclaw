/**
 * E2E (Option E Phase 1, #582, D-1 file-export amendment): bootstrap with
 * the "download an encrypted file instead" choice, wipe the device, restore
 * from that file with the SAME passkey — no relay involved at any point, so
 * this runs fully against STAGING right now (unlike escrow-roundtrip.mjs /
 * escrow-wrong-passkey.mjs, which need totalreclaw-relay PR #46 live).
 *
 * L3 — phrase-safety: reads the phrase from the DOM only for the
 * backup-confirm gate. Never logs it. The downloaded file's bytes are read
 * back via Playwright's download API, not the OS filesystem UI.
 *
 * Run: dev server up on :5173 (VITE_SERVER_URL=staging), then
 * `node e2e/escrow-file-roundtrip.mjs`.
 */
import { chromium } from "@playwright/test";
import fs from "node:fs/promises";

const BASE = "http://localhost:5173";
const out = (m) => console.log(`[e2e] ${m}`);
let failed = false;
function check(name, cond) {
  out(`${cond ? "PASS" : "FAIL"} — ${name}`);
  if (!cond) failed = true;
}

const browser = await chromium.launch();
const context = await browser.newContext({ acceptDownloads: true });
const page = await context.newPage();
page.on("pageerror", (e) => out(`PAGEERROR: ${e.message}`));

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

let downloadedFilePath;

try {
  // --- 1. Bootstrap, choosing the file-export path ---
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
  await page.getByRole("button", { name: "Download an encrypted file instead" }).click();

  await page.getByText("Where you put this matters.").waitFor({ timeout: 10000 });
  check("mandatory file-export caveat shown at the moment of download", true);

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download backup" }).click(),
  ]);
  await page.waitForURL("**/memory", { timeout: 30000 });
  check("choosing file export still lands on a working vault", page.url().endsWith("/memory"));

  downloadedFilePath = await download.path();
  check("a file was actually downloaded", !!downloadedFilePath);

  const fileText = await fs.readFile(downloadedFilePath, "utf8");
  const fileJson = JSON.parse(fileText);
  check("file has format: totalreclaw-escrow, v: 1", fileJson.format === "totalreclaw-escrow" && fileJson.v === 1);
  check(
    "file carries no vault identifier (no smart_account/address/lookup_hash key)",
    !("smart_account" in fileJson) && !("address" in fileJson) && !("lookup_hash" in fileJson),
  );
  check("file has a non-empty note", typeof fileJson.note === "string" && fileJson.note.length > 20);

  const originalRecord = await readVaultRecord();
  const originalSmartAccount = originalRecord?.smart_account;

  // --- 2. No relay escrow request occurred on this path ---
  const requestedPaths = [];
  page.on("request", (req) => {
    try {
      requestedPaths.push(new URL(req.url()).pathname);
    } catch {
      /* ignore */
    }
  });

  // --- 3. Simulate device loss, restore from the file ---
  await wipeLocalStorage();
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForURL("**/unlock", { timeout: 15000 });
  await page.getByText("Open a backup file", { exact: true }).waitFor({ timeout: 10000 });

  const fileChooserPromise = page.waitForEvent("filechooser");
  await page.getByText("Open a backup file", { exact: true }).click();
  const fileChooser = await fileChooserPromise;
  await fileChooser.setFiles(downloadedFilePath);

  await page.waitForURL("**/memory", { timeout: 20000 });
  check("restoreFromFile recovers to /memory with no relay backup and no phrase typed", page.url().endsWith("/memory"));

  const recoveredRecord = await readVaultRecord();
  check(
    "recovered Smart Account address is byte-identical to the original",
    !!recoveredRecord?.smart_account &&
      recoveredRecord.smart_account.toLowerCase() === originalSmartAccount?.toLowerCase(),
  );

  const escrowRequests = requestedPaths.filter((p) => p.startsWith("/v1/escrow"));
  check("file restore issued NO request to any /v1/escrow* endpoint", escrowRequests.length === 0);
} catch (e) {
  out(`EXCEPTION: ${e.message}`);
  failed = true;
  try {
    await page.screenshot({ path: "e2e/shot-escrow-file-roundtrip-failure.png" });
  } catch {}
}

await browser.close();
out(failed ? "RESULT: FAIL" : "RESULT: PASS");
process.exit(failed ? 1 : 0);
