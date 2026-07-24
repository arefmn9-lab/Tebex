import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), "utf8");
const results = [];

function check(name, pass, detail = "") {
  results.push({ name, pass: Boolean(pass), detail });
}

const app = read("src/App.jsx");
const shell = read("src/components/ui/AppShell.jsx");
const sidebar = read("src/components/Sidebar.jsx");
const topbar = read("src/components/TopBar.jsx");
const designSystem = read("src/components/ui/DesignSystem.jsx");
const selector = read("src/pages/PlatformSelector.jsx");
const campaignEntry = read("src/pages/CampaignExecutionEntry.jsx");
const operations = read("src/pages/OperationsAndLogs.jsx");
const simpleAccounts = read("src/pages/SimpleAccounts.jsx");
const platforms = read("src/data/platforms.js");
const styles = read("src/styles.css");
const apiClient = read("src/api/client.js");
const campaignsApi = read("src/api/campaigns.js");

const navIds = ["dashboard", "campaigns", "accounts", "numberBank", "operations", "reports", "diagnostics"];
const platformNames = ["بله", "تلگرام", "واتساپ", "اینستاگرام", "روبیکا", "ایتا", "سروش پلاس"];
const forbiddenNormalUiTerms = [
  "user-data-dir",
  "profile-directory",
  "scenario hash",
  "click budget",
  "selector",
  "PID",
  "JSON payload",
];

check("New AppShell renders", shell.includes("export default function AppShell") && shell.includes("<Sidebar") && shell.includes("<TopHeader"));
check("RTL direction is correct", shell.includes('dir="rtl"') && sidebar.includes('dir="rtl"') && campaignEntry.includes('dir="rtl"'));
check("Sidebar primary navigation is campaign-centric", navIds.every((id) => sidebar.includes(`id: "${id}"`)));
check("Standalone send page is not a visible nav item", !sidebar.includes('id: "messaging"'));
check("Standalone settings, queue, and logs are not visible nav items", !sidebar.includes('id: "settings"') && !sidebar.includes('id: "jobs"') && !sidebar.includes('id: "logs"'));
check("Active navigation state works", sidebar.includes("aria-current") && sidebar.includes("normalizedActive") && sidebar.includes("operations"));
check("Mobile sidebar opens and closes", shell.includes("mobile-nav-open") && shell.includes("setDrawerOpen(true)") && shell.includes("setDrawerOpen(false)"));
check("Messaging compatibility route remains internal", app.includes("messaging: CampaignExecutionEntry") && campaignEntry.includes("listCampaigns") && campaignEntry.includes("queueCampaign"));
check("Campaigns remain the visible campaign workflow", app.includes("campaigns: CommercialCampaigns") && sidebar.includes('id: "campaigns"'));
check("Legacy campaign execution entry is not visible navigation", !sidebar.includes("CampaignExecutionEntry") && !sidebar.includes("ارسال پیام"));
check("Direct platform selector remains compatibility-only", app.includes("platforms: PlatformSelector") && selector.includes("platforms.map"));
check("Configured platform data remains intact", platformNames.every((name) => platforms.includes(name)));
check("Unsupported platforms show non-ready status", platforms.includes("نیازمند اتصال") && platforms.includes("به‌زودی") && platforms.includes("ready: false"));
check("Operations and logs page is merged visually", app.includes("operations: OperationsAndLogs") && operations.includes("listJobs") && operations.includes("listEvents") && operations.includes("getSchedulerStatus"));
check("Simplified accounts page is wired", app.includes("accounts: SimpleAccounts") && simpleAccounts.includes("getAccountRegistrySummary") && simpleAccounts.includes("listPlatformAccounts") && simpleAccounts.includes("openBaleLogin"));
check("Primary buttons use new visual variants", designSystem.includes("PrimaryButton") && styles.includes(".primary-button") && styles.includes("--primary"));
const darkTextareaRule = styles.indexOf(".commercial-modal .settings-grid textarea");
const lightInputOverride = styles.lastIndexOf("background: #ffffff");
check("Inputs and textareas use light readable surfaces", styles.includes(".text-area") && lightInputOverride > darkTextareaRule);
const normalUiText = [sidebar, topbar, campaignEntry, operations, simpleAccounts, designSystem].join("\n");
check("Normal UI does not expose backend filesystem paths", !/[A-Z]:\\|backend\/runtime|user-data-dir/.test(normalUiText));
check("Normal UI does not expose protected technical terms", forbiddenNormalUiTerms.every((term) => !normalUiText.includes(term)));
check("Existing API client remains compatible", apiClient.includes("export async function request") && apiClient.includes("API_BASE_URL"));
check("Existing Bale routes remain reachable", app.includes("baleBulk") && app.includes("BaleWorkspace") && campaignsApi.includes("prepareBaleBulkLiveRun"));
check("Diagnostics remains reachable", app.includes("diagnostics") && sidebar.includes('id: "diagnostics"'));
check("Desktop smoke passes", styles.includes("grid-template-columns: minmax(0, 1fr) 264px"));
check("Tablet smoke passes", styles.includes("@media (max-width: 940px)"));
check("Mobile smoke passes", styles.includes(".mobile-nav-open .sidebar") && styles.includes("grid-template-columns: 1fr"));

const failed = results.filter((result) => !result.pass);
console.log(JSON.stringify({ ok: failed.length === 0, results }, null, 2));
if (failed.length) process.exit(1);
