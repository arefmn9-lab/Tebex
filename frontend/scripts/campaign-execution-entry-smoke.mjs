import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), "utf8");
const page = read("src/pages/CampaignExecutionEntry.jsx");
const api = read("src/api/campaigns.js");
const checks = [];

function check(name, pass) {
  checks.push({ name, pass: Boolean(pass) });
}

check("no first campaign auto-selection", !page.includes("campaignId(rows[0]"));
const mountEffect = page.match(/useEffect\(\(\) => \{\s*([\s\S]*?)\s*\}, \[\]\);/)?.[1] || "";
check("only GET list runs on mount", mountEffect.includes("load({ preserveSelection: false })") && !mountEffect.includes("runValidation") && !mountEffect.includes("runCheck") && !mountEffect.includes("confirmQueue") && !mountEffect.includes("queueCampaign"));
check("explicit campaign selection clears evidence", page.includes("function selectCampaign") && page.includes("clearEvidence()"));
check("queue disabled before all gates", page.includes("queueDisabledReasons") && page.includes("const canQueue = queueDisabledReasons.length === 0"));
check("validation evidence required", page.includes("Run validation.") && page.includes("Validation hash or ID was not returned."));
check("check evidence required", page.includes("Run check without sending.") && page.includes("Check evidence is missing non-mutating proof."));
check("final review evidence required", page.includes("Run final review.") && page.includes("Final-review hash was not returned."));
check("confirmation modal required", page.includes("confirmationOpen") && page.includes("<Modal title=\"Confirm Queue Request\""));
check("confirmation checkbox default is false", page.includes("setOperatorConfirmed(false)") && page.includes("checked={operatorConfirmed}"));
check("queue payload is hardened", page.includes("validation_hash: validation?.validation_hash") && page.includes("dry_run_id: checkEvidence?.audit?.dry_run_id") && page.includes("final_review_hash: finalReviewHash") && page.includes("manifest_hash: manifestHash"));
check("confirmation controls explicit operator flag", page.includes("explicit_operator_confirmation: true") && page.indexOf("explicit_operator_confirmation: true") > page.indexOf("async function confirmQueue"));
check("idempotency generated at final confirmation", page.includes("idempotency_key: createIdempotencyKey(selectedId)") && page.indexOf("idempotency_key: createIdempotencyKey(selectedId)") > page.indexOf("async function confirmQueue"));
check("queue api sends JSON payload", api.includes("export function queueCampaign(campaignId, payload)") && api.includes("body: JSON.stringify(payload)"));
check("queue call only exists in final confirmation flow", (page.match(/queueCampaign\(/g) || []).length === 1 && page.indexOf("queueCampaign(selectedId, payload)") > page.indexOf("async function confirmQueue"));

const failed = checks.filter((item) => !item.pass);
console.log(JSON.stringify({ ok: failed.length === 0, checks }, null, 2));
if (failed.length) process.exit(1);
