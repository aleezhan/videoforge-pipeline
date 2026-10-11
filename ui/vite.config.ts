import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

const here = import.meta.dirname;

// Разработка: `npm run dev` поднимает Vite на 5173 и проксирует /api и /files
// в `videoforge ui` (8765). Сборка: `npm run build` → dist/, его раздаёт тот же
// сервер, прокси не нужен.
const backend = process.env.VF_UI_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(here, "src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
      "/files": backend,
    },
  },
  // один бандл для локального пульта — нормально; предупреждение о размере глушим
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1200 },
});
