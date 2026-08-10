import fs from "node:fs";

const page = fs.readFileSync(new URL("../src/pages/CommercialCampaigns.jsx", import.meta.url), "utf8");

const checks = [
  ["one account-count request field", page.includes("requested_account_count: Number(capacityInput)") && page.includes("value={capacityInput}")],
  ["eligible account count", page.includes("eligible_account_count")],
  ["requested and current allocation are visible", page.includes("requested_account_count") && page.includes("allocated_account_count")],
  ["message-volume capacity absent", !page.includes("total_available_sending_capacity") && !page.includes("remaining_free_capacity")],
  ["artificial effective capacity absent", !page.includes("roundCapacity") && !page.includes("systemCapacity") && !page.includes("hostCapacity")],
  ["internal empty-selection enum not rendered", !page.includes("<strong>{capacityState.status}</strong>")],
  ["neutral empty selection", page.includes("برای تخصیص اکانت، ابتدا کمپین را ذخیره یا از فهرست انتخاب کنید.")],
  ["internal round details hidden", !page.includes("round_observability") && !page.includes("پروفایل</th>") && !page.includes("PID</th>")],
  ["overlapping concurrency controls hidden", !page.includes("مرورگر همزمان") && !page.includes("کارگر همزمان") && !page.includes("حداکثر اکانت همزمان")],
  ["selection restored", page.includes('localStorage.getItem("clinicos:selected-bale-campaign")') && page.includes("getCampaignCapacity(expandedCampaignId)")],
  ["local input survives refresh", page.includes("!capacityInputDirty.current && capacityInitializedCampaign.current !== expandedCampaignId")],
  ["allocation sends exact draft", page.includes("requested_account_count: Number(capacityInput)") && !page.includes("Math.min")],
  ["exact shortage is operator-readable", page.includes("برای اجرای این کمپین ${requested.toLocaleString") && page.includes("اکانت آماده است.")],
  ["scheduled start absent", !page.includes("scheduledStartAt") && !page.includes("scheduled_start_at")],
  ["automatic retry absent", !page.includes("automaticRetryEnabled") && !page.includes("automatic_retry_enabled")],
  ["operator delay contract", page.includes("operation_delay_seconds")],
  ["Bale-only account request", page.includes('listPlatformAccounts("bale")') && !page.includes("platformOptions.map((platform) => listPlatformAccounts")],
];

const failed = checks.filter(([, passed]) => !passed);
for (const [name, passed] of checks) console.log(`${passed ? "PASS" : "FAIL"} ${name}`);
if (failed.length) process.exit(1);
