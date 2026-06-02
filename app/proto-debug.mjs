import { chromium } from "playwright";
const browser = await chromium.launch();
const page = await browser.newPage();
const errs = [];
page.on("console", (m) => {
  if (m.type() === "error") errs.push("CONSOLE: " + m.text());
});
page.on("pageerror", (e) => errs.push("PAGEERROR: " + (e.stack || e.message)));
for (const url of [
  "http://localhost:5173/proto/kg?engine=flow",
  "http://localhost:5173/proto/kg?engine=reagraph",
]) {
  errs.length = 0;
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  const rootLen = await page.evaluate(
    () => document.getElementById("root")?.innerText?.length ?? -1,
  );
  console.log("\n==", url, "==");
  console.log("rootTextLen:", rootLen);
  console.log(errs.length ? errs.join("\n").slice(0, 1500) : "(no console/page errors)");
}
await browser.close();
