import assert from "node:assert/strict";
import { BALE_AUTH_LABELS, formatBaleAccountActionError, mapBaleAuthState } from "../src/baleAuthPresentation.js";
import { parseDiagnosticSources } from "../src/diagnosticSources.js";

const base = {
  account_id: "bale_09211690533", authentication_status: "authenticated",
  authentication_verified_at: "2026-08-02T08:00:00Z", session_persistence_status: "verified",
  lifecycle_status: "ready", queue_eligible: true, worker_eligible: true, eligibility_reasons: [], verification_expired: false,
  durable_identity_verified: true, session_health_acceptable: true,
};
assert.equal(mapBaleAuthState({ ...base, queue_eligible: false }, { auth: { auth_state: "unknown_auth_state" } }), BALE_AUTH_LABELS.unknown);
assert.equal(mapBaleAuthState({ ...base, verification_expired: true, eligibility_reasons: ["authentication_verification_required"] }), BALE_AUTH_LABELS.ready);
assert.equal(mapBaleAuthState({ ...base, authentication_status: "login_required", lifecycle_status: "login_required", queue_eligible: false }, { auth: { auth_state: "login_required" } }), BALE_AUTH_LABELS.login_required);
assert.equal(mapBaleAuthState(base, { auth: { auth_state: "authenticated", authenticated: true } }), BALE_AUTH_LABELS.ready);
assert.equal(mapBaleAuthState({ ...base, effective_auth_state: undefined, session_state: "authenticated" }, {}), BALE_AUTH_LABELS.ready);
assert.equal(mapBaleAuthState({ ...base, enabled: false, authentication_status: "unverified", session_state: "temporarily_inconclusive", worker_eligible: false }, {}), BALE_AUTH_LABELS.temporarily_inconclusive);
const recheckFailure = formatBaleAccountActionError("Current Bale Login/OTP screen is visible (fake_session_recheck_failure)");
assert.equal(recheckFailure.includes("Login/OTP"), false);
assert.equal(recheckFailure.includes("fake_session_recheck_failure"), false);
assert.match(recheckFailure, /بازبینی نشست/);

const fulfilled = (value) => ({ status: "fulfilled", value: Object.assign(value, { __http_status: 200 }) });
const parsed = parseDiagnosticSources([
  fulfilled({ items: [] }), fulfilled({ items: [] }), fulfilled({ logs: [{ id: 1 }] }),
  fulfilled({ items: [{ name: "probe" }] }), fulfilled({ providers: [{ id: "local" }] }),
  fulfilled({ items: [] }), fulfilled({ scheduler: { scheduler_status: "paused" } }), fulfilled({ items: [{ event: "button_click" }] }),
]);
assert.equal(parsed.values.scheduler.scheduler_status, "paused");
assert.equal(parsed.values.diagnostic_runs[0].name, "probe");
assert.equal(parsed.values.browser_providers[0].id, "local");
assert.equal(parsed.metadata.scheduler.http_status, 200);
assert.equal(parsed.metadata.browser_providers.value_path_used, "providers");
assert.equal(parsed.values.client_events[0].event, "button_click");
console.log("Auth mapping and diagnostic export regression tests passed");
