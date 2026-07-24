import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, process.cwd(), "");
  const apiOrigin =
    environment.HUB_API_ORIGIN ??
    `http://127.0.0.1:${environment.HUB_API_PORT ?? "8767"}`;

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 8765,
      strictPort: true,
      proxy: {
        "/api": {
          target: apiOrigin,
          changeOrigin: false,
        },
      },
    },
    preview: {
      host: "127.0.0.1",
      port: 8765,
      strictPort: true,
    },
  };
});

