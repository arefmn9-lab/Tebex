import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), "utf8");
const app = read("src/App.jsx");
const page = read("src/pages/CommercialCampaigns.jsx");
const { executeCampaignStart } = await import(pathToFileURL(path.join(root, "src/campaignStartFlow.js")));

const requests = [];
const campaignId = "campaign-visible-route-smoke";
const result = await executeCampaignStart(campaignId, {
  validateCampaignStart: async (id) => {
    requests.push(`${id}/validate-start`);
    return { success: true, valid: true, can_start: true, ok: true, blocking_reasons: [], validation_hash: "validation-hash" };
  },
  finalReviewCampaign: async () => ({
    ok: true,
    approved: true,
    has_blocking_errors: false,
    validation_hash: "review-validation-hash",
    final_review_hash: "review-hash",
    review_token: "review-token",
    confirmed_recipients_summary: { manifest_hash: "manifest-hash" },
  }),
  queueCampaign: async (id, payload) => {
    requests.push(`${id}/queue`);
    if (payload.validation_hash !== "review-validation-hash") throw new Error("final-review validation hash not forwarded");
    if (payload.review_token !== "review-token") throw new Error("review token not forwarded");
    return { success: true, queued: true };
  },
  startCampaign: async (id) => {
    requests.push(`${id}/start`);
    return { campaign: { id, status: "running" } };
  },
  idempotencyKey: "routed-smoke-key",
});

const expected = [
  `${campaignId}/validate-start`,
  `${campaignId}/queue`,
  `${campaignId}/start`,
];
const blockedRequests = [];
let blockedMessage = "";
try {
  await executeCampaignStart("campaign-blocked-review", {
    validateCampaignStart: async () => ({ success: true, valid: true, can_start: true, ok: true, blocking_reasons: [], validation_hash: "validation-hash" }),
    finalReviewCampaign: async () => ({
      ok: false,
      approved: false,
      has_blocking_errors: true,
      blocking_errors: [
        { error_code: "configuration_revision_mismatch", field: "configuration_revision_id", field_label_fa: "نسخه تأییدشده تنظیمات" },
      ],
      final_review_hash: "blocked-review-hash",
      review_token: "blocked-review-token",
    }),
    queueCampaign: async () => blockedRequests.push("queue"),
    startCampaign: async () => blockedRequests.push("start"),
    idempotencyKey: "blocked-review-key",
  });
} catch (error) {
  blockedMessage = error.message;
}
const checks = [
  ["#/campaigns routes to CommercialCampaigns", app.includes("campaigns: CommercialCampaigns")],
  ["visible campaign button uses runCampaign", /onClick=\{\(\) => runCampaign\(campaign\)\}/.test(page)],
  ["active draft handler awaits complete start transaction", page.includes("await executeCampaignStart(id,")],
  ["network lifecycle order is validate, queue, start", JSON.stringify(requests) === JSON.stringify(expected)],
  ["transaction returns running start result", result.started?.campaign?.status === "running"],
  ["blocking HTTP 200 review does not queue", blockedRequests.length === 0],
  ["blocking review displays exact Persian field reason", blockedMessage.includes("نسخه تأییدشده تنظیمات") && blockedMessage.includes("configuration_revision_mismatch")],
];

const failed = checks.filter(([, pass]) => !pass);
console.log(JSON.stringify({ ok: failed.length === 0, requests, checks: checks.map(([name, pass]) => ({ name, pass })) }, null, 2));
if (failed.length) process.exit(1);
