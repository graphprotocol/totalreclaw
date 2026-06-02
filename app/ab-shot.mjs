import { chromium } from "playwright";
const base = "http://localhost:5173";
const OUT = "/Users/pdiogo/Documents/code/totalreclaw/app/proto-shots";
const browser = await chromium.launch();

const shots = [
  { url: "/proto/session/s1?view=source", name: "session-source-desktop", w: 1280, h: 1000, dpr: 1 },
  { url: "/proto/timeline?view=source", name: "timeline-source-desktop", w: 1280, h: 1000, dpr: 1 },
  { url: "/proto/session/s1?view=source", name: "session-source-mobile", w: 390, h: 844, dpr: 2 },
];
for (const s of shots) {
  const ctx = await browser.newContext({ viewport: { width: s.w, height: s.h }, deviceScaleFactor: s.dpr });
  const p = await ctx.newPage();
  await p.goto(base + s.url, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1300);
  await p.screenshot({ path: `${OUT}/${s.name}.png`, fullPage: true });
  console.log("shot", s.name);
  await ctx.close();
}
await browser.close();
console.log("done");
