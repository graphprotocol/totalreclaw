/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  optimizeDeps: {
    // The web-target WASM package resolves its .wasm via
    // `new URL('…_bg.wasm', import.meta.url)`; esbuild pre-bundling would
    // break that in dev. Build (Rollup) handles it natively as an asset.
    exclude: ["@totalreclaw/core"],
  },
  build: {
    target: "es2022",
    sourcemap: true,
  },
  define: {
    global: "globalThis",
  },
  test: {
    environment: "node",
  },
});
