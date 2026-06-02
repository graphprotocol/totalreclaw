import { chromium } from "playwright";

const base = "http://localhost:5173";
const cases = [
  { mode: "graph", click: "TotalReclaw" },
  { mode: "workspace", click: "TotalReclaw" },
];
const viewports = [
  { label: "desktop", width: 1280, height: 900, dpr: 1, full: false },
  { label: "mobile", width: 390, height: 844, dpr: 2, full: true },
];

const browser = await chromium.launch();
for (const vp of viewports) {
  const ctx = await browser.newContext({
    viewport: { width: vp.width, height: vp.height },
    deviceScaleFactor: vp.dpr,
  });
  const page = await ctx.newPage();
  for (const c of cases) {
    await page.goto(`${base}/proto/explore?mode=${c.mode}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1800);
    try {
      await page
        .locator(`.react-flow__node:has-text("${c.click}")`)
        .first()
        .click({ timeout: 4000 });
    } catch (e) {
      console.log("click miss", c.mode, vp.label, String(e).slice(0, 60));
    }
    await page.waitForTimeout(900);
    await page.screenshot({
      path: `proto-shots/explore-${c.mode}-${vp.label}.png`,
      fullPage: vp.full,
    });
    console.log("shot", c.mode, vp.label);
  }
  await ctx.close();
}
await browser.close();
console.log("done");
