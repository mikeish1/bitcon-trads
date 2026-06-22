import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// In dev, proxy /api to the FastAPI backend (default :8080) so the SPA talks to a
// same-origin URL and we avoid CORS entirely. In production the SPA is served BY
// FastAPI (StaticFiles), so /api is already same-origin. Override the backend with
// VITE_DEV_API_TARGET if the dashboard runs on a different port.
const API_TARGET = process.env.VITE_DEV_API_TARGET ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
        // SSE needs an un-buffered, long-lived connection.
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => proxyReq.setHeader("Accept-Encoding", "identity"));
        },
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    rollupOptions: {
      output: {
        // Code-split the heavy charting lib so the initial bundle stays light.
        manualChunks: {
          recharts: ["recharts"],
          vendor: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query", "@tanstack/react-table", "@tanstack/react-virtual"],
        },
      },
    },
  },
});
