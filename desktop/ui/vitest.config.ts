import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    // Bound simultaneous JSDOM runtimes without relaxing test timeouts.
    maxWorkers: 4,
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
    css: true,
  },
});
