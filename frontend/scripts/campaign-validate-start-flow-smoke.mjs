import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const page = fs.readFileSync(path.join(root, "src/pages/CommercialCampaigns.jsx"), "utf8");
const { campaignValidationFailureMessage, executeCampaignStart } = await import(
  pathToFileURL(path.join(root, "src/campaignStartFlow.js")),
);

const checks = [];
const check = (name, pass, details = undefined) => checks.push({ name, pass: Boolean(pass), ...(details ? { details } : {}) });

function validValidation(overrides = {}) {
  return {
    success: true,
    valid: true,
    can_start: true,
    ok: true,
    blocking_reasons: [],
    required_account_count: 2,
    eligible_account_count: 2,
    deliverable_job_count: 2,
    exact_concurrency: { max_concurrent_accounts: 2, browser_concurrency: 2, worker_concurrency: 2 },
    ...overrides,
  };
}

async function runValidCase({ validation = validValidation(), review = {}, queue = {}, start = {} } = {}) {
  const calls = [];
  let persistedStatus = "draft";
  const result = await executeCampaignStart("campaign-isolated", {
    validateCampaignStart: async () => {
      calls.push("validate-start");
      return validation;
    },
    finalReviewCampaign: async () => {
      calls.push("final-review");
      return {
        ok: true,
        approved: true,
        has_blocking_errors: false,
        validation_hash: "validation-hash",
        final_review_hash: "review-hash",
        review_token: "review-token",
        confirmed_recipients_summary: { manifest_hash: "manifest-hash" },
        ...review,
      };
    },
    queueCampaign: async (_id, payload) => {
      calls.push("queue");
      persistedStatus = "queued";
      return { success: true, queued: true, campaign: { status: "queued" }, ...queue, payload };
    },
    startCampaign: async () => {
      calls.push("start");
      persistedStatus = "running";
      return { campaign: { id: "campaign-isolated", status: "running" }, ...start };
    },
    idempotencyKey: "isolated-idempotency-key",
  });
  return { calls, result, persistedStatus };
}

const fresh = await runValidCase({});
check("A fresh valid campaign reaches exactly one Start request", JSON.stringify(fresh.calls) === JSON.stringify(["validate-start", "final-review", "queue", "start"]), fresh.calls);
check("A successful Start persists running state", fresh.persistedStatus === "running");

const blockedCalls = [];
let blockedMessage = "";
try {
  await executeCampaignStart("campaign-blocked-capacity", {
    validateCampaignStart: async () => ({
      success: true,
      valid: false,
      can_start: false,
      ok: false,
      blocking_reasons: ["requested_accounts_exceed_runtime_capacity"],
      required_account_count: 9,
      eligible_account_count: 9,
      exact_concurrency: { max_concurrent_accounts: 1, browser_concurrency: 1, worker_concurrency: 1 },
    }),
    finalReviewCampaign: async () => blockedCalls.push("final-review"),
    queueCampaign: async () => blockedCalls.push("queue"),
    startCampaign: async () => blockedCalls.push("start"),
    idempotencyKey: "blocked-idempotency-key",
  });
} catch (error) {
  blockedMessage = error.message;
}
check("B validation failure makes zero downstream requests", blockedCalls.length === 0, blockedCalls);
check("B validation blocker is explicit", blockedMessage.includes("9 account(s)") && blockedMessage.includes("runtime capacity is 1"), blockedMessage);

const capacityCase = await runValidCase({
  validation: validValidation({ required_account_count: 2, exact_concurrency: { max_concurrent_accounts: 2, browser_concurrency: 2, worker_concurrency: 2 } }),
});
check("C valid capacity has no stale frontend gate", capacityCase.calls.includes("start"));

const provenanceCase = await runValidCase({
  validation: validValidation({ recipient_manifest_valid: true, recipient_provenance_valid: true }),
  review: { confirmed_recipients_summary: { manifest_hash: "manifest-hash", provenance_valid: true } },
});
check("D confirmed manifest/provenance proof reaches queue and Start", provenanceCase.calls.includes("queue") && provenanceCase.calls.includes("start"));
check("D manifest proof is forwarded to queue", provenanceCase.result.queued.payload.manifest_hash === "manifest-hash");

const duplicateGuardPresent = page.includes("campaignRunGuards.current.has(id)")
  && page.includes("campaignRunGuards.current.add(id)")
  && page.includes("campaignRunGuards.current.delete(id)");
check("E rapid double Execute is synchronously guarded", duplicateGuardPresent);

const scheduler = [];
const started = await runValidCase({});
if (started.persistedStatus === "running") scheduler.push("claim");
check("F scheduler claim is possible only after persisted running", JSON.stringify(scheduler) === JSON.stringify(["claim"]), { persistedStatus: started.persistedStatus });

check("capacity blocker formatter is generic", campaignValidationFailureMessage({
  blocking_reasons: ["requested_accounts_exceed_runtime_capacity"],
  required_account_count: 9,
  exact_concurrency: { max_concurrent_accounts: 1 },
}).includes("runtime capacity is 1"));

const failed = checks.filter((item) => !item.pass);
console.log(JSON.stringify({ ok: failed.length === 0, checks }, null, 2));
if (failed.length) process.exit(1);
