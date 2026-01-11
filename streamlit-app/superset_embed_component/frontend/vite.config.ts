import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "build",     // Python이 frontend/build 를 바라보게
    emptyOutDir: true
  },
  server: {
    port: 3001,
    strictPort: true
  }
});