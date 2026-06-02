import { chromium } from "playwright";

const base = "http://localhost:5173";
const OUT = "/Users/pdiogo/Documents/code/totalreclaw/app/proto-shots";
const browser = await chromium.launch();

// Desktop — default, then pinned + curation menu open
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  await p.goto(`${base}/proto/session/s1`, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1400);
  await p.screenshot({ path: `${OUT}/session-desktop.png`, fullPage: true });

  try {
    await p.locator('button[aria-label="Pin memory"]').first().click({ timeout: 3000 });
    await p.waitForTimeout(300);
    await p.locator('button[aria-label="More actions"]').nth(1).click({ timeout: 3000 });
  } catch (e) {
    console.log("action miss", String(e).slice(0, 60));
  }
  await p.waitForTimeout(500);
  await p.screenshot({ path: `${OUT}/session-actions-desktop.png`, fullPage: true });
  await ctx.close();
}

// Mobile — default
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const p = await ctx.newPage();
  await p.goto(`${base}/proto/session/s1`, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1400);
  await p.screenshot({ path: `${OUT}/session-mobile.png`, fullPage: true });
  await ctx.close();
}

await browser.close();
console.log("done");
