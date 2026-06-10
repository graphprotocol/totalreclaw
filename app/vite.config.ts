/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

export default defineConfig({
  // basicSsl → HTTPS so WebCrypto (crypto.subtle) works over the tailnet host
  // (WebCrypto requires a secure context: HTTPS or localhost).
  plugins: [react(), basicSsl()],
  server: {
    host: true,
    // Allow Tailscale MagicDNS + .local hosts for cross-device dev preview.
    allowedHosts: [".ts.net", ".local", "localhost"],
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
