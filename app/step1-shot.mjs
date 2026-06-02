import { chromium } from "playwright";

const base = "http://localhost:5173";
const OUT = "/Users/pdiogo/Documents/code/totalreclaw/app/proto-shots";
const browser = await chromium.launch();

// Timeline desktop — all, then filtered by Health scope
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  await p.goto(`${base}/proto/timeline`, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1500);
  await p.screenshot({ path: `${OUT}/timeline2-desktop.png`, fullPage: true });
  try {
    await p.locator('button:has-text("Health")').first().click({ timeout: 3000 });
  } catch (e) {
    console.log("health miss", String(e).slice(0, 50));
  }
  await p.waitForTimeout(700);
  await p.screenshot({ path: `${OUT}/timeline2-filtered-desktop.png`, fullPage: true });
  await ctx.close();
}

// Timeline mobile
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const p = await ctx.newPage();
  await p.goto(`${base}/proto/timeline`, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1500);
  await p.screenshot({ path: `${OUT}/timeline2-mobile.png`, fullPage: true });
  await ctx.close();
}

// Explore graph-first, node selected (ring + cohesive panel)
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  await p.goto(`${base}/proto/explore?mode=graph`, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1800);
  try {
    await p.locator('.react-flow__node:has-text("TotalReclaw")').first().click({ timeout: 4000 });
  } catch (e) {
    console.log("node miss", String(e).slice(0, 50));
  }
  await p.waitForTimeout(800);
  await p.screenshot({ path: `${OUT}/explore2-graph-desktop.png`, fullPage: false });
  await ctx.close();
}

await browser.close();
console.log("done");
