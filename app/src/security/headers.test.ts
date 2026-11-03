/**
 * Guards the CSP shipped in `public/_headers` (Cloudflare Pages).
 *
 * The write path (delete / pin / retype / scope) lazy-loads the
 * `@totalreclaw/core` WASM (`lib/wasm.ts`). Chromium and Safari refuse to
 * compile WebAssembly unless `script-src` allows `'wasm-unsafe-eval'`, so a
 * plain `script-src 'self'` silently breaks every vault write in those
 * browsers. This test pins the allowance while asserting `script-src` stays
 * otherwise locked to `'self'` (no `'unsafe-eval'`, no `'unsafe-inline'`, no
 * remote hosts).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const headersPath = fileURLToPath(new URL("../../public/_headers", import.meta.url));

function csp(): Map<string, string[]> {
  const raw = readFileSync(headersPath, "utf8");
  const line = raw
    .split("\n")
    .map((l) => l.trim())
    .find((l) => l.toLowerCase().startsWith("content-security-policy:"));
  if (!line) throw new Error("no Content-Security-Policy in public/_headers");
  const value = line.slice(line.indexOf(":") + 1);
  const directives = new Map<string, string[]>();
  for (const part of value.split(";")) {
    const [name, ...sources] = part.trim().split(/\s+/);
    if (name) directives.set(name.toLowerCase(), sources);
  }
  return directives;
}

describe("public/_headers CSP", () => {
  it("allows WebAssembly compilation via 'wasm-unsafe-eval'", () => {
    expect(csp().get("script-src")).toContain("'wasm-unsafe-eval'");
  });

  it("keeps script-src otherwise locked to 'self'", () => {
    expect([...(csp().get("script-src") ?? [])].sort()).toEqual(
      ["'self'", "'wasm-unsafe-eval'"].sort(),
    );
  });

  it("never allows 'unsafe-eval' or 'unsafe-inline' scripts", () => {
    const d = csp();
    for (const name of ["script-src", "default-src"]) {
      expect(d.get(name) ?? []).not.toContain("'unsafe-eval'");
      expect(d.get(name) ?? []).not.toContain("'unsafe-inline'");
    }
  });

  it("keeps object-src and base-uri at 'none'", () => {
    const d = csp();
    expect(d.get("object-src")).toEqual(["'none'"]);
    expect(d.get("base-uri")).toEqual(["'none'"]);
  });
});
