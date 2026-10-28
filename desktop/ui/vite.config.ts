import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build-time tooling only. The native shell serves frontendDist via custom protocol.
export default defineConfig({
  plugins: [react()],
});
