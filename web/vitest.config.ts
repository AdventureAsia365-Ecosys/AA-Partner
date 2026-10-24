import path from "path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: { alias: { "@": path.resolve(__dirname) } },
  // Node by default; a component test opts into jsdom with a `// @vitest-environment jsdom` first line.
  test: { include: ["tests/**/*.test.ts"], environment: "node", maxWorkers: 2 },
});
