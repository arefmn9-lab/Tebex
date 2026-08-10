import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const sourceFiles = ["src/pages/BaleAccounts.jsx", "src/pages/CommercialCampaigns.jsx"];
const sourceRevision = crypto.createHash("sha256")
  .update(sourceFiles.map((file) => `${file}:${fs.readFileSync(path.join(frontendRoot, file))}`).join("\n"))
  .digest("hex").slice(0, 20);

export default defineConfig({
  plugins: [react()],
  define: {
    __CLINICOS_SOURCE_REVISION__: JSON.stringify(sourceRevision),
    __CLINICOS_FRONTEND_ROOT__: JSON.stringify(frontendRoot),
    "process.env.UI_ENABLED": JSON.stringify(
      process.env.UI_ENABLED ?? process.env.VITE_UI_ENABLED ?? "true"
    )
  },
  server: {
    port: 5173,
    strictPort: false
  }
});
