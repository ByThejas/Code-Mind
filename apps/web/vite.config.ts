import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API server the dev server forwards /api and /health to.
const apiTarget = process.env.CODEMIND_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
    plugins: [react()],
    server: {
        host: true,
        port: 5173,
        strictPort: true,
        proxy: {
            "/api": apiTarget,
            "/health": apiTarget,
        },
    },
});
