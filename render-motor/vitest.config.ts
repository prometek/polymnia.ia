import { defineConfig } from "vitest/config";

// Unit layer: pure logic only (theme resolution, cosmetic merge, layout routing).
// No browser — components that render props->frames belong to the future
// still-snapshot layer (@remotion/renderer + pixelmatch), not here.
export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    environment: "node",
  },
});
