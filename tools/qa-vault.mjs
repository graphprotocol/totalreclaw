/**
 * QA driver for the vault SPA — flow-adaptive.
 *
 * The SPA is mid-migration from a Phase-1 recovery-phrase entry flow to a
 * passkey-based "Keeper" flow (PR #329, feat/spa-functional). Production and PR
 * previews can therefore present EITHER surface. This driver detects which one
 * the app lands on at runtime and drives it accordingly:
 *
 *   - LEGACY (phrase-entry): the current production flow. Paste-and-derive form
 *     with `input[placeholder="word 1"]`, submit, land on /vault. UNCHANGED.
 *   - PASSKEY (#329 "Keeper"): app routes to /bootstrap. A fresh browser context
 *     has no local vault, so the app shows the choose screen. We attach a CDP
 *     WebAuthn virtual authenticator (mirroring the config in the SPA's own E2E,
 *     app/e2e/bootstrap-unlock.mjs — ctap2/internal/PRF) and drive the
 *     "I have a recovery phrase" → restore flow: it derives keys from the SAME
 *     known QA phrase (so it lands on the real vault with real memories, exactly
 *     like the legacy flow) and enrols a passkey via the virtual authenticator.
 *     Post-restore the SPA navigates to /vault which its router redirects to the
 *     real post-auth surface (/memory).
 *
 * Either way the driver asserts the SAME downstream invariants: it reached the
 * vault, the memory surface rendered, and there were no console/page/network
 * errors. `reachedVault` in the report is true for BOTH /vault (legacy) and
 * /memory (passkey) so the internal workflow's gate is unchanged.
 *
 * The phrase NEVER leaves this process: it's pulled from the keychain via
 * `security` (or QA_RECOVERY_PHRASE in CI), held in process memory only, typed
 * into the page via Playwright, and never logged. stdout / stderr are safe to
 * surface back to the operator.
 *
 * Usage:
 *   node tools/qa-vault.mjs <url> [--headed]
 *
 * Examples:
 *   node tools/qa-vault.mjs http://localhost:5173
 *   node tools/qa-vault.mjs https://pr-329.totalreclaw-app.pages.dev --headed
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

/**
 * Attach a CDP WebAuthn virtual authenticator so the passkey flow can enrol /
 * assert credentials headlessly. Config mirrors the SPA's own E2E
 * (app/e2e/bootstrap-unlock.mjs) EXACTLY, including hasPrf — PRF-wrap is the
 * crux of the Keeper flow, so the authenticator must advertise it.
 * Returns the authenticatorId (informational; never contains secret material).
 */
async function attachVirtualAuthenticator(context, page) {
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
  return authenticatorId;
}

/**
 * Decide which surface the app rendered. Races the two known entry markers:
 *   - legacy phrase-entry form: input[placeholder="word 1"]
 *   - passkey Keeper: /bootstrap or /unlock URL, or the choose-screen copy.
 * Returns "legacy" | "passkey" | "unknown".
 */
async function detectSurface(page) {
  // URL is the fastest signal for the passkey build (router lands on
  // /bootstrap for a fresh context, /unlock if a wrapped vault is cached).
  const url = page.url();
  if (/\/(bootstrap|unlock)(\b|\/|$)/.test(url)) return "passkey";

  try {
    const marker = await Promise.race([
      page
        .waitForSelector('input[placeholder="word 1"]', { timeout: 12000 })
        .then(() => "legacy"),
      // Keeper choose-screen / restore entry points.
      page
        .waitForSelector(
          'text=Create a new vault, text=I have a recovery phrase, text=Unlock with passkey',
          { timeout: 12000 },
        )
        .then(() => "passkey"),
    ]);
    return marker;
  } catch {
    // Neither marker within the window — re-check URL in case a late redirect
    // moved us onto a passkey route.
    if (/\/(bootstrap|unlock)(\b|\/|$)/.test(page.url())) return "passkey";
    return "unknown";
  }
}

/**
 * LEGACY path: the current production flow. Paste phrase into word-1 (handler
 * splits to all 12), submit, wait for /vault or a visible error. Unchanged
 * from the pre-#329 driver.
 */
async function driveLegacy(page, phrase) {
  await page.fill('input[placeholder="word 1"]', phrase);
  await page.click('button[type="submit"]');
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

/**
 * PASSKEY path (#329): drive the Keeper restore-from-phrase flow. This derives
 * keys from the SAME known QA phrase, so it lands on the real vault with real
 * memories (equivalent to the legacy flow), while exercising passkey enrolment
 * via the virtual authenticator.
 *
 * Requires attachVirtualAuthenticator() to have run first.
 *
 * The phrase is typed into the restore textarea and never logged. We do NOT use
 * the "Create a new vault" branch: it would mint a throwaway vault + phrase each
 * run and land on an empty vault, which can't assert real memories render.
 */
async function drivePasskey(page, phrase) {
  // If the app cached a wrapped vault it may show /unlock instead of /bootstrap.
  // A fresh CI context won't, but handle it defensively: prefer the passkey
  // unlock button; fall back to the recovery-phrase route if offered.
  if (/\/unlock(\b|\/|$)/.test(page.url())) {
    const unlockBtn = page.getByRole("button", { name: "Unlock with passkey" });
    if (await unlockBtn.isVisible().catch(() => false)) {
      await unlockBtn.click();
      await page
        .waitForURL(/\/(memory|vault)(\b|\/|$)/, { timeout: 20000 })
        .catch(() => {});
      return;
    }
  }

  // Choose screen → "I have a recovery phrase" (restore). Wait for the choose
  // screen to settle (PRF gate resolves "checking" → "choose").
  await page
    .getByText("I have a recovery phrase", { exact: false })
    .waitFor({ timeout: 15000 });
  await page.getByText("I have a recovery phrase", { exact: false }).click();

  // Restore screen: paste phrase into the textarea, click Restore vault.
  await page.waitForSelector(
    'textarea[placeholder="twelve words separated by spaces"]',
    { timeout: 10000 },
  );
  await page.fill(
    'textarea[placeholder="twelve words separated by spaces"]',
    phrase,
  );
  await page.getByRole("button", { name: "Restore vault" }).click();

  // doBootstrap: derive keys → relay (/v1/smart-account, /v1/register) → enrol
  // passkey (virtual authenticator) → wrap → navigate /vault → router redirect
  // → /memory. Also watch for a visible error so we don't hang the full window.
  try {
    await Promise.race([
      page.waitForURL(/\/(memory|vault)(\b|\/|$)/, { timeout: 30000 }),
      page.waitForSelector("text=Failed to fetch", { timeout: 30000 }),
      page.waitForSelector("text=couldn’t", { timeout: 30000 }),
      page.waitForSelector("text=Invalid", { timeout: 30000 }),
    ]);
  } catch {
    /* timeout — capture whatever state the page is in */
  }
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

  // Attach the WebAuthn virtual authenticator BEFORE navigation so a passkey
  // build can enrol during bootstrap/restore. Harmless for the legacy flow
  // (no WebAuthn calls are made there). authenticatorId is informational only.
  let virtualAuthenticatorId = null;
  try {
    virtualAuthenticatorId = await attachVirtualAuthenticator(ctx, page);
  } catch (err) {
    // Non-fatal: the legacy flow doesn't need it. Record so a passkey failure
    // downstream is explicable.
    pageErrors.push({ message: `virtual authenticator setup failed: ${redactPhrase(err.message, phrase)}`, stack: "" });
  }

  let navError = null;
  try {
    await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  } catch (err) {
    navError = err.message;
  }

  // Detect which surface rendered, then drive it.
  const surface = await detectSurface(page);
  const formReady = surface === "legacy" || surface === "passkey";

  if (surface === "legacy") {
    await driveLegacy(page, phrase);
  } else if (surface === "passkey") {
    await drivePasskey(page, phrase);
  }

  // Give a beat for React Query / paginated subgraph calls to settle.
  await page.waitForTimeout(2000);

  const finalUrl = page.url();
  const visibleError = await page
    .locator("p.text-red-600, [role='alert'], p.text-clay-deep")
    .first()
    .textContent()
    .catch(() => null);

  const screenshotPath = path.join(REPORT_DIR, `screenshot-${stamp}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: true });

  await browser.close();

  // Both surfaces converge on a vault view: legacy → /vault, passkey → /memory.
  // Treat either as "reached the vault" so the internal workflow's gate is
  // surface-agnostic.
  const reachedVault = /\/(vault|memory)(\b|\/|$)/.test(finalUrl);

  const report = {
    target: TARGET_URL,
    stamp,
    surface,
    navError,
    formReady,
    finalUrl,
    reachedVault,
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
  // NOTE: the internal qa-autopilot workflow jq-parses these keys —
  // reachedVault, pageErrorCount, consoleErrorCount, networkFailureCount,
  // visibleError. Keep them; new fields (surface) are additive.
  const summary = {
    target: report.target,
    surface: report.surface,
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
