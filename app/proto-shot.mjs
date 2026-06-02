import { chromium } from "playwright";

const base = "http://localhost:5173";
const shots = [
  { name: "kg-flow", path: "/proto/kg?engine=flow" },
  { name: "kg-force", path: "/proto/kg?engine=force" },
];
const viewports = [
  { label: "mobile", width: 390, height: 844, dpr: 2 },
  { label: "desktop", width: 1280, height: 900, dpr: 1 },
];

const browser = await chromium.launch();
for (const vp of viewports) {
  const ctx = await browser.newContext({
    viewport: { width: vp.width, height: vp.height },
    deviceScaleFactor: vp.dpr,
  });
  const page = await ctx.newPage();
  for (const s of shots) {
    await page.goto(base + s.path, { waitUntil: "networkidle" });
    await page.waitForTimeout(2400); // force layout + WebGL warm-up settle
    await page.screenshot({
      path: `proto-shots/${s.name}-${vp.label}.png`,
      fullPage: true,
    });
    console.log("shot", s.name, vp.label);
  }
  await ctx.close();
}
await browser.close();
console.log("done");
