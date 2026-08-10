import fs from "node:fs";

const page = fs.readFileSync(new URL("../src/pages/CommercialCampaigns.jsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../src/api/campaigns.js", import.meta.url), "utf8");
const client = fs.readFileSync(new URL("../src/api/client.js", import.meta.url), "utf8");
const startFlow = fs.readFileSync(new URL("../src/campaignStartFlow.js", import.meta.url), "utf8");
const checks = [];

function check(name, condition) {
  checks.push({ name, pass: Boolean(condition) });
}

const draftRunBlock = page.match(/campaign\.status === "draft"\) \{([\s\S]*?)\n\s+\} else \{/)?.[1] || "";
check(
  "draft run validates, queues, then starts",
  draftRunBlock.includes("await executeCampaignStart(id,")
    && startFlow.indexOf("await validateCampaignStart(campaignId)") < startFlow.indexOf("await queueCampaign(campaignId,")
    && startFlow.indexOf("await queueCampaign(campaignId,") < startFlow.indexOf("await startCampaign(campaignId)"),
);
check("production campaign run has no dry-run step", !draftRunBlock.includes("checkCampaignWithoutSending") && !draftRunBlock.includes("dry_run"));
check("queued campaigns start directly", page.includes('campaign.status === "queued"') && page.includes("await startCampaign(id)"));
check("paused campaigns resume", page.includes('campaign.status === "paused"') && page.includes("await resumeCampaign(id)"));
check("pause refreshes state", page.includes("await pauseCampaign(id)") && page.includes("await load()"));
check("delete handler calls API", page.includes("async function removeCampaign") && page.includes("await deleteCampaign(id)"));
check("delete API exists", api.includes('method: "DELETE"'));
check("campaign source uses backend field", page.includes("source_channel_uid: sourceUidFromValue(primarySource)"));
check("backend error code is visible", client.includes("detail?.error_code") && client.includes("detail?.error_message"));
check("campaign diagnostics exist", page.includes("[CAMPAIGN_ACTION]") && page.includes("[CAMPAIGN_TRANSITION]") && page.includes("[CAMPAIGN_DELETE]"));
check("campaign save waits for recipient upload and confirmation", page.includes("await uploadRecipientImport(savedId, draft.numberFile)")
  && page.includes("await confirmRecipientImport(")
  && page.indexOf("await uploadRecipientImport(savedId, draft.numberFile)") < page.indexOf("await load()"));
check("failed import preserves campaign history", !page.includes("await deleteCampaign(savedId).catch"));
check("new campaign is selected immediately", page.includes('localStorage.setItem("clinicos:selected-bale-campaign", savedId)')
  && page.includes('stage: creating ? "new_campaign_selected"'));
check("new campaign is not hidden by its name", !page.includes("internalCampaignNamePatterns")
  && page.includes("campaign.hidden") && page.includes("campaign.deleted_at"));
check("draft creation does not require recipient file", !page.includes("برای ساخت کمپین باید فایل CSV یا Excel مخاطبان را انتخاب کنید"));
check("campaign create response accepts direct or nested payload", page.includes("const savedCampaign = saved?.campaign || saved"));

const failed = checks.filter((item) => !item.pass);
console.log(JSON.stringify({ ok: failed.length === 0, checks }, null, 2));
if (failed.length) process.exit(1);
