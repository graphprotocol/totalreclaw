import { chromium } from "playwright";
const base = "http://localhost:5173";
const OUT = "/Users/pdiogo/Documents/code/totalreclaw/app/proto-shots";
const browser = await chromium.launch();

async function shot(name, w, h, dpr, fn, full = false) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: dpr });
  const p = await ctx.newPage();
  await fn(p);
  await p.screenshot({ path: `${OUT}/${name}.png`, fullPage: full });
  console.log("shot", name);
  await ctx.close();
}

const goto = (p, url, t = 1300) => p.goto(base + url, { waitUntil: "domcontentloaded" }).then(() => p.waitForTimeout(t));
const click = async (p, sel, t = 4000) => {
  try {
    await p.locator(sel).first().click({ timeout: t });
  } catch (e) {
    console.log("click miss", sel, String(e).slice(0, 40));
  }
};

// Explore: node selected -> sessions list
await shot("explore-sessions-desktop", 1280, 900, 1, async (p) => {
  await goto(p, "/proto/explore", 1600);
  await click(p, '.react-flow__node:has-text("TotalReclaw")');
  await p.waitForTimeout(700);
});
// Explore: drilled into a session
await shot("explore-drill-desktop", 1280, 900, 1, async (p) => {
  await goto(p, "/proto/explore", 1600);
  await click(p, '.react-flow__node:has-text("TotalReclaw")');
  await p.waitForTimeout(500);
  await click(p, 'button:has-text("Designed the warm")');
  await p.waitForTimeout(500);
});
// Explore mobile (sessions)
await shot("explore-sessions-mobile", 390, 844, 2, async (p) => {
  await goto(p, "/proto/explore", 1600);
  await click(p, '.react-flow__node:has-text("TotalReclaw")');
  await p.waitForTimeout(700);
}, true);
// Pair (sample filled)
await shot("pair-desktop", 1280, 900, 1, async (p) => {
  await goto(p, "/proto/pair");
  await click(p, 'button:has-text("Use a sample phrase")');
  await p.waitForTimeout(400);
});
await shot("pair-mobile", 390, 844, 2, async (p) => {
  await goto(p, "/proto/pair");
  await click(p, 'button:has-text("Use a sample phrase")');
  await p.waitForTimeout(400);
}, true);
// Timeline (source default) + KG (flow)
await shot("timeline-source2-desktop", 1280, 1000, 1, async (p) => goto(p, "/proto/timeline"), true);
await shot("kg2-desktop", 1280, 900, 1, async (p) => goto(p, "/proto/kg", 2200));

await browser.close();
console.log("done");
