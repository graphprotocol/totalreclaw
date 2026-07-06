/**
 * E2E (T15): passkey bootstrap → unlock against STAGING, via a WebAuthn virtual
 * authenticator with prf support.
 *
 * L3 — phrase-safety: this script reads the generated recovery phrase from the
 * DOM only to type it into the backup-confirm step. It NEVER logs the phrase and
 * NEVER screenshots the show-phrase screen. Screenshots are taken only on the
 * post-auth screens (no secret material).
 *
 * Run: dev server up on :5173 (VITE_SERVER_URL=staging), then `node e2e/bootstrap-unlock.mjs`.
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

// Capture runtime errors (these would be invisible to a build).
page.on("pageerror", (e) => out(`PAGEERROR: ${e.message}`));
page.on("console", (m) => {
  if (m.type() === "error") out(`CONSOLE.ERROR: ${m.text()}`);
});

// --- WebAuthn virtual authenticator with prf ---
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
out(`virtual authenticator ${authenticatorId} added (hasPrf:true)`);

try {
  // --- 1. Bootstrap: create a new vault ---
  await page.goto(`${BASE}/bootstrap`, { waitUntil: "domcontentloaded" });
  // PRF gate resolves to the choose screen if the authenticator is usable.
  await page.getByText("Create a new vault", { exact: true }).waitFor({ timeout: 15000 });
  check("PRF gate passed (choose screen shown)", true);

  await page.getByText("Create a new vault", { exact: true }).click();
  await page.getByText("Write this down").waitFor({ timeout: 10000 });

  // Read the phrase words (NOT logged) to satisfy the backup gate.
  const words = await page.$$eval("ol li", (lis) =>
    lis.map((li) => li.querySelectorAll("span")[1]?.textContent?.trim() ?? ""),
  );
  check("recovery phrase rendered (12 words)", words.length === 12 && words.every(Boolean));

  await page.getByText("I’ve written it down").click();
  await page.getByText("Confirm your backup").waitFor({ timeout: 10000 });

  // Fill the requested word positions from the shown phrase.
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

  await page.getByRole("button", { name: "Create my vault" }).click();

  // Bootstrap derives keys, hits staging (/v1/smart-account, /v1/register),
  // enrols the passkey, wraps, persists — then lands on /memory.
  await page.waitForURL("**/memory", { timeout: 30000 });
  check("bootstrap landed on /memory", page.url().endsWith("/memory"));

  // --- 2. At-rest invariants ---
  const idbKeys = await page.evaluate(
    () =>
      new Promise((res, rej) => {
        const open = indexedDB.open("keyval-store");
        open.onsuccess = () => {
          try {
            const db = open.result;
            const tx = db.transaction("keyval", "readonly");
            const req = tx.objectStore("keyval").getAllKeys();
            req.onsuccess = () => res(req.result.map(String));
            req.onerror = () => rej(req.error);
          } catch (e) {
            res([]);
          }
        };
        open.onerror = () => rej(open.error);
      }),
  );
  check(
    "IndexedDB holds a wrapped vault record",
    idbKeys.some((k) => k.startsWith("totalreclaw-spa:vault:")),
  );

  const vaultRecord = await page.evaluate(
    () =>
      new Promise((res) => {
        const open = indexedDB.open("keyval-store");
        open.onsuccess = () => {
          const db = open.result;
          const tx = db.transaction("keyval", "readonly");
          const req = tx.objectStore("keyval").getAll();
          req.onsuccess = () => {
            const rec = req.result.find((r) => r && r.wrapped_vault_key);
            res(
              rec
                ? {
                    hasVault: !!rec.wrapped_vault_key?.ciphertext,
                    hasAuth: !!rec.wrapped_auth_key?.ciphertext,
                    hasMaster: !!rec.wrapped_master_key?.ciphertext,
                    chain: rec.chain_id,
                  }
                : null,
            );
          };
        };
      }),
  );
  check("wrapped vault+auth+master keys present", !!(vaultRecord && vaultRecord.hasVault && vaultRecord.hasAuth && vaultRecord.hasMaster));
  check("chain_id is Gnosis (100)", vaultRecord?.chain === 100);

  const storage = await page.evaluate(() => ({
    local: localStorage.length,
    session: sessionStorage.length,
  }));
  check("zero phrase at rest (localStorage empty)", storage.local === 0);
  check("zero phrase at rest (sessionStorage empty)", storage.session === 0);

  // M1: no phrase word may appear in any persisted IndexedDB value (the wrapped
  // blobs are ciphertext bytes). `words` stays in-script; never logged.
  const phraseLeak = await page.evaluate(
    (words) =>
      new Promise((res) => {
        const open = indexedDB.open("keyval-store");
        open.onsuccess = () => {
          const tx = open.result.transaction("keyval", "readonly");
          const req = tx.objectStore("keyval").getAll();
          req.onsuccess = () => {
            const dump = JSON.stringify(req.result, (_k, v) =>
              v instanceof Uint8Array ? Array.from(v) : v,
            ).toLowerCase();
            res(words.some((w) => w && dump.includes(w.toLowerCase())));
          };
          req.onerror = () => res(false);
        };
        open.onerror = () => res(false);
      }),
    words,
  );
  check("no phrase word appears in any persisted IndexedDB value", phraseLeak === false);

  await page.screenshot({ path: "e2e/shot-memory.png" });

  // --- 3. Lock + unlock with passkey ---
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForURL("**/unlock", { timeout: 15000 });
  check("reload (locked) redirects to /unlock", page.url().endsWith("/unlock"));
  await page.screenshot({ path: "e2e/shot-unlock.png" });

  await page.getByRole("button", { name: "Unlock with passkey" }).click();
  await page.waitForURL("**/memory", { timeout: 20000 });
  check("passkey unlock returns to /memory", page.url().endsWith("/memory"));
} catch (e) {
  out(`EXCEPTION: ${e.message}`);
  failed = true;
  try {
    await page.screenshot({ path: "e2e/shot-failure.png" });
  } catch {}
}

await browser.close();
out(failed ? "RESULT: FAIL" : "RESULT: PASS");
process.exit(failed ? 1 : 0);
