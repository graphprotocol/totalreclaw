import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  PasskeyAdvisoryNotice,
  PasskeyUnsupportedNotice,
  HERMES_GUIDE_URL,
  UNSUPPORTED_PLATFORM_LINE,
} from "./PasskeySupportNotice";
import type { PasskeyPlatform } from "../lib/auth/prf-support";

// renderToStaticMarkup HTML-escapes apostrophes; compare on unescaped text.
const text = (html: string) => html.replace(/&#x27;/g, "'").replace(/&amp;/g, "&");

const PLATFORMS: PasskeyPlatform[] = ["windows", "firefox-android", "apple-old", "safari", "unknown"];

describe("PasskeyUnsupportedNotice (support matrix §5.3)", () => {
  it.each(PLATFORMS)("shows the %s guidance line and the Hermes link", (platform) => {
    const html = text(renderToStaticMarkup(<PasskeyUnsupportedNotice platform={platform} />));
    expect(html).toContain("Your browser can’t secure a vault yet");
    expect(html).toContain(UNSUPPORTED_PLATFORM_LINE[platform]);
    expect(html).toContain(`href="${HERMES_GUIDE_URL}"`);
    expect(html).not.toContain("Continue anyway");
  });

  it("no longer recommends Safari 17", () => {
    for (const p of PLATFORMS) {
      expect(renderToStaticMarkup(<PasskeyUnsupportedNotice platform={p} />)).not.toMatch(/Safari 17/);
    }
  });

  it("Windows copy names the 25H2 update requirement", () => {
    expect(UNSUPPORTED_PLATFORM_LINE.windows).toMatch(/25H2/);
  });
});

describe("PasskeyAdvisoryNotice (security-key advisory instead of a block)", () => {
  it("lets the user continue and links to Hermes", () => {
    const html = text(renderToStaticMarkup(<PasskeyAdvisoryNotice platform="unknown" onContinue={() => {}} />));
    expect(html).toContain("You may need a security key");
    expect(html).toContain("Continue anyway");
    expect(html).toContain(`href="${HERMES_GUIDE_URL}"`);
  });

  it("adds the Safari security-key caveat on Safari", () => {
    const html = text(renderToStaticMarkup(<PasskeyAdvisoryNotice platform="safari" onContinue={() => {}} />));
    expect(html).toContain(UNSUPPORTED_PLATFORM_LINE.safari);
  });

  it("omits the generic browser line when the platform is unknown", () => {
    const html = text(renderToStaticMarkup(<PasskeyAdvisoryNotice platform="unknown" onContinue={() => {}} />));
    expect(html).not.toContain(UNSUPPORTED_PLATFORM_LINE.unknown);
  });
});
