import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), "utf8");
const app = read("src/App.jsx");
const sidebar = read("src/components/Sidebar.jsx");
const campaignsApi = read("src/api/campaigns.js");
const removedPages = [
  "src/pages/BaleWorkspace.jsx",
  "src/pages/PlatformWorkspace.jsx",
  "src/pages/PlatformSelector.jsx",
  "src/pages/BaleBulkCampaigns.jsx",
  "src/api/baleWorkspace.js",
];

const checks = [
  ["old platform hashes redirect to campaigns", app.includes('hash.startsWith("platform/")') && app.includes('return "campaigns"')],
  ["no dynamic platform page rendering", !app.includes("PlatformWorkspace") && !app.includes("BaleWorkspace")],
  ["no direct platform navigation", !sidebar.includes("platform:") && !sidebar.includes('id: "platforms"')],
  ["direct execution pages removed", removedPages.every((file) => !fs.existsSync(path.join(root, file)))],
  ["page-only Bale bulk APIs removed", !campaignsApi.includes("/automation/platforms/bale/bulk-campaigns")],
  ["account management remains routed", app.includes("accounts: BaleAccounts") && sidebar.includes('id: "accounts"')],
];

const failed = checks.filter(([, pass]) => !pass);
console.log(JSON.stringify({ ok: failed.length === 0, checks: checks.map(([name, pass]) => ({ name, pass })) }, null, 2));
if (failed.length) process.exit(1);
