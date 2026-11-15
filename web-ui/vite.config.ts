/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `pnpm dev` serves the UI on http://<tenant>.localhost:5173 and forwards the API to
// the local platform (the edge on :8080). The Host header is kept, because the router
// takes the tenant from it.
const platform = "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/graphql": { target: platform, changeOrigin: false },
      "/auth": { target: platform, changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    // A tenant's own address, as in the browser.
    environmentOptions: { jsdom: { url: "http://acme.localhost:8080/" } },
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/gql/**", "src/main.tsx", "src/test/**", "src/**/*.test.{ts,tsx}"],
      thresholds: { lines: 90, branches: 90, functions: 90, statements: 90 },
    },
  },
});
