import { chromium } from "playwright";
const base = "http://localhost:5173";
const OUT = "/Users/pdiogo/Documents/code/totalreclaw/app/proto-shots";
const b = await chromium.launch();
const cap = async (name, url, { w = 1100, h = 1000, full = true, clearAll = false } = {}) => {
  const ctx = await b.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 2 });
  const p = await ctx.newPage();
  await p.goto(base + url, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1100);
  if (clearAll) {
    for (let i = 0; i < 8; i++) {
      const btn = p.locator("main article button").first();
      if ((await btn.count()) === 0) break;
      try { await btn.click({ timeout: 1500 }); } catch { break; }
      await p.waitForTimeout(300);
    }
    await p.waitForTimeout(500);
  }
  await p.screenshot({ path: `${OUT}/${name}.png`, fullPage: full });
  console.log("shot", name);
  await ctx.close();
};
await cap("review-feed", "/proto/review");
await cap("lineage-conflict", "/proto/lineage/where-pedro-works");
await cap("lineage-trip", "/proto/lineage/july-trip");
await cap("gallery-v2", "/proto");
// Review zero-state: resolve every card by clicking the first action pill repeatedly.
await cap("review-empty", "/proto/review", { clearAll: true });
await b.close();
console.log("done");
