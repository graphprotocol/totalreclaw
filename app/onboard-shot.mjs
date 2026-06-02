import { chromium } from "playwright";
const base = "http://localhost:5173";
const OUT = "/Users/pdiogo/Documents/code/totalreclaw/app/proto-shots";
const browser = await chromium.launch();

async function shot(name, w, h, dpr, fn) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: dpr });
  const p = await ctx.newPage();
  await fn(p);
  await p.screenshot({ path: `${OUT}/${name}.png` });
  console.log("shot", name);
  await ctx.close();
}
const go = (p, u, t = 900) => p.goto(base + u, { waitUntil: "domcontentloaded" }).then(() => p.waitForTimeout(t));
const click = async (p, sel) => {
  try {
    await p.locator(sel).first().click({ timeout: 3000 });
  } catch (e) {
    console.log("miss", sel, String(e).slice(0, 40));
  }
};

await shot("onboarding-welcome", 1280, 900, 1, (p) => go(p, "/proto/onboarding"));
await shot("onboarding-phrase", 1280, 900, 1, async (p) => {
  await go(p, "/proto/onboarding");
  await click(p, 'button:has-text("Create a recovery phrase")');
  await p.waitForTimeout(500);
});
await shot("unlock-desktop", 1280, 900, 1, (p) => go(p, "/proto/pair"));
await shot("unlock-phrase-desktop", 1280, 900, 1, async (p) => {
  await go(p, "/proto/pair");
  await click(p, 'button:has-text("Use your recovery phrase")');
  await p.waitForTimeout(400);
});
await shot("onboarding-phrase-mobile", 390, 844, 2, async (p) => {
  await go(p, "/proto/onboarding");
  await click(p, 'button:has-text("Create a recovery phrase")');
  await p.waitForTimeout(500);
});

await browser.close();
console.log("done");
