import { chromium } from "playwright";
const base = "http://localhost:5173";
const OUT = "/Users/pdiogo/Documents/code/totalreclaw/app/proto-shots";
const b = await chromium.launch();
const cap = async (name, url, w, h, full) => {
  const ctx = await b.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  await p.goto(base + url, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1300);
  await p.screenshot({ path: `${OUT}/${name}.png`, fullPage: full });
  console.log("shot", name);
  await ctx.close();
};
await cap("polish-timeline", "/proto/timeline", 1280, 1000, true);
await cap("polish-session", "/proto/session/s3", 1280, 1000, true); // s3 has "1 thread"
await b.close();
console.log("done");
