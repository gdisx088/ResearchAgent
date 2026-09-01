import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/testSetup.ts"
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://127.0.0.1:8100"
    }
  }
});
