/**
 * QA driver for the vault SPA — reads the dummy recovery phrase from the
 * macOS keychain, walks the paste-and-derive flow, and emits a structured
 * report of what the browser saw (console + network + final URL + screenshot).
 *
 * The phrase NEVER leaves this process: it's pulled from the keychain via
 * `security`, held in process memory only, typed into the page via
 * Playwright's keyboard, and never logged. The script's stdout / stderr
 * are safe to surface back to the operator.
 *
 * Usage:
 *   node tools/qa-vault.mjs <url> [--headed]
 *
 * Examples:
 *   node tools/qa-vault.mjs http://localhost:5173
 *   node tools/qa-vault.mjs https://pr-237.totalreclaw-app.pages.dev --headed
 *
 * Prereqs:
 *   security add-generic-password -a totalreclaw -s totalreclaw-qa-phrase -U -w
 *   (run this once in Terminal; -w with no value reads the phrase via prompt)
 */
import { execFileSync } from "node:child_process";
import { writeFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

const TARGET_URL = process.argv[2];
const HEADED = process.argv.includes("--headed");
const KEYCHAIN_SERVICE = process.env.QA_PHRASE_SERVICE || "totalreclaw-qa-phrase";
const KEYCHAIN_ACCOUNT = process.env.QA_PHRASE_ACCOUNT || "totalreclaw";
const REPORT_DIR = path.resolve(path.dirname(new URL(import.meta.url).pathname), "qa-output");

if (!TARGET_URL) {
  console.error("usage: node tools/qa-vault.mjs <url> [--headed]");
  process.exit(2);
}

function loadPhrase() {
  // CI / non-macOS path: env var wins if present. Set as a GH Actions
  // secret and exported into the workflow step that invokes this driver.
  const fromEnv = (process.env.QA_RECOVERY_PHRASE || "").trim();
  if (fromEnv) return fromEnv;

  // Local dev path: macOS keychain (`security add-generic-password ...`).
  try {
    return execFileSync(
      "security",
      [
        "find-generic-password",
        "-a", KEYCHAIN_ACCOUNT,
        "-s", KEYCHAIN_SERVICE,
        "-w",
      ],
      { stdio: ["ignore", "pipe", "ignore"] },
    ).toString().trim();
  } catch {
    console.error(
      `\n[qa] no phrase available.\n` +
      `[qa]   - local: security add-generic-password -a ${KEYCHAIN_ACCOUNT} -s ${KEYCHAIN_SERVICE} -U -w\n` +
      `[qa]   - CI:    export QA_RECOVERY_PHRASE='<phrase>'\n`,
    );
    process.exit(3);
  }
}

function redactPhrase(text, phrase) {
  if (!text || !phrase) return text;
  // Redact full phrase and any 3+-word contiguous slice.
  let out = text.replaceAll(phrase, "[REDACTED_PHRASE]");
  const words = phrase.split(/\s+/);
  for (let i = 0; i <= words.length - 3; i++) {
    const slice = words.slice(i, i + 3).join(" ");
    out = out.replaceAll(slice, "[REDACTED_PHRASE_SLICE]");
  }
  return out;
}

async function run() {
  mkdirSync(REPORT_DIR, { recursive: true });
  const phrase = loadPhrase();
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");

  const browser = await chromium.launch({ headless: !HEADED });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await ctx.newPage();

  const consoleEvents = [];
  const pageErrors = [];
  const networkEvents = [];
  const networkFailures = [];

  page.on("console", (msg) => {
    consoleEvents.push({
      type: msg.type(),
      text: redactPhrase(msg.text(), phrase),
    });
  });
  page.on("pageerror", (err) => {
    pageErrors.push({ message: redactPhrase(err.message, phrase), stack: redactPhrase(err.stack || "", phrase) });
  });
  page.on("requestfailed", (req) => {
    networkFailures.push({
      url: req.url(),
      method: req.method(),
      failure: req.failure()?.errorText,
    });
  });
  page.on("response", async (res) => {
    const url = res.url();
    if (!/totalreclaw\.xyz|localhost:5173|pages\.dev/.test(url)) return;
    if (url.includes(".vite/deps") || url.includes("/@react-refresh") || url.endsWith(".js") || url.endsWith(".css")) {
      return; // ignore static assets, focus on API + page nav
    }
    let body = null;
    try {
      const ct = res.headers()["content-type"] || "";
      if (ct.includes("json") || ct.includes("text")) {
        const t = await res.text();
        body = redactPhrase(t.slice(0, 2000), phrase);
      }
    } catch { /* response body unavailable */ }
    networkEvents.push({
      url,
      method: res.request().method(),
      status: res.status(),
      body,
    });
  });

  let navError = null;
  try {
    await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  } catch (err) {
    navError = err.message;
  }

  // Wait for the paste form to render.
  let formReady = false;
  try {
    await page.waitForSelector('input[placeholder="word 1"]', { timeout: 10000 });
    formReady = true;
  } catch {
    formReady = false;
  }

  if (formReady) {
    // Paste the full phrase into word-1; handler splits to all 12 words.
    await page.fill('input[placeholder="word 1"]', phrase);
    // Tap submit.
    await page.click('button[type="submit"]');
    // Allow up to 20s for either /vault navigation or visible error to settle.
    try {
      await Promise.race([
        page.waitForURL(/\/vault/, { timeout: 20000 }),
        page.waitForSelector("text=Failed to fetch", { timeout: 20000 }),
        page.waitForSelector("text=API ", { timeout: 20000 }),
        page.waitForSelector("text=Invalid", { timeout: 20000 }),
      ]);
    } catch {
      /* timeout — capture whatever state the page is in */
    }
  }

  // Give a beat for React Query / paginated subgraph calls to settle.
  await page.waitForTimeout(2000);

  const finalUrl = page.url();
  const visibleError = await page
    .locator("p.text-red-600, [role='alert']")
    .first()
    .textContent()
    .catch(() => null);

  const screenshotPath = path.join(REPORT_DIR, `screenshot-${stamp}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: true });

  await browser.close();

  const report = {
    target: TARGET_URL,
    stamp,
    navError,
    formReady,
    finalUrl,
    reachedVault: /\/vault/.test(finalUrl),
    visibleError: redactPhrase(visibleError, phrase),
    pageErrors,
    consoleErrors: consoleEvents.filter((e) => e.type === "error"),
    consoleWarnings: consoleEvents.filter((e) => e.type === "warning"),
    networkFailures,
    networkEvents,
    screenshotPath,
  };

  const reportPath = path.join(REPORT_DIR, `report-${stamp}.json`);
  writeFileSync(reportPath, JSON.stringify(report, null, 2));

  // Compact stdout summary so the operator sees the headline without grepping.
  const summary = {
    target: report.target,
    reachedVault: report.reachedVault,
    finalUrl: report.finalUrl,
    visibleError: report.visibleError,
    pageErrorCount: report.pageErrors.length,
    consoleErrorCount: report.consoleErrors.length,
    networkFailureCount: report.networkFailures.length,
    fullReport: reportPath,
    screenshot: report.screenshotPath,
  };
  console.log(JSON.stringify(summary, null, 2));

  // Non-zero exit when CI should fail / open an issue.
  const failed =
    !!report.navError ||
    !report.formReady ||
    !report.reachedVault ||
    !!report.visibleError ||
    report.pageErrors.length > 0 ||
    report.consoleErrors.length > 0 ||
    report.networkFailures.length > 0;
  process.exit(failed ? 1 : 0);
}

run().catch((err) => {
  console.error("[qa] uncaught:", err.message);
  process.exit(1);
});
