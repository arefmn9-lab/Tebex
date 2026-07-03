import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  define: {
    "process.env.UI_ENABLED": JSON.stringify(
      process.env.UI_ENABLED ?? process.env.VITE_UI_ENABLED ?? "true"
    )
  },
  server: {
    port: 5173,
    strictPort: false
  }
});
