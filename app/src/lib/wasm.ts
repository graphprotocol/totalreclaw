/**
 * Lazy loader for the `@totalreclaw/core` WASM (browser / web target).
 *
 * `@totalreclaw/core@2.5.6+` publishes a wasm-pack **web**-target build as the
 * `./web` subpath export (#500) — same crate version, wasm binary byte-identical
 * to the nodejs build. Its default-export init fetches the `.wasm` via
 * `new URL('totalreclaw_core_bg.wasm', import.meta.url)`, which Vite rewrites
 * to a hashed asset URL in its own chunk.
 *
 * IMPORTANT (bundle discipline): this module — and everything it pulls in
 * (`bundler.ts`, `userop.ts`, the 2.3 MB `.wasm`) — must be reached ONLY via a
 * dynamic `import()` on the FIRST write path (see `api.ts` deleteFact). It must
 * NEVER be statically imported by a read-path module, or the WASM would land in
 * the initial `index-*.js` chunk. Verified against the Vite chunk graph.
 */

export type Core = typeof import("@totalreclaw/core/web");

let corePromise: Promise<Core> | null = null;

/**
 * Initialize and return the core WASM module (memoized). First call fetches +
 * instantiates the `.wasm`; subsequent calls resolve immediately.
 */
export function loadCore(): Promise<Core> {
  if (!corePromise) {
    corePromise = (async () => {
      const mod = await import("@totalreclaw/core/web");
      // Web-target init: `default()` fetches + instantiates the wasm from the
      // module-relative URL (Vite emits it as a hashed asset).
      await mod.default();
      return mod as unknown as Core;
    })();
  }
  return corePromise;
}
