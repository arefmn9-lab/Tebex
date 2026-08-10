import { request } from "./client";

const root = "/automation/platforms/bale/onboarding";

export const listBaleOnboardingAccounts = () => request(`${root}/accounts`);
export const getBaleOnboardingAccount = (accountId) => request(`${root}/accounts/${encodeURIComponent(accountId)}`);
export const reconcileBaleAccount = (accountId) => request(`${root}/accounts/${encodeURIComponent(accountId)}/reconcile`);
export const resetBaleAccountProfile = (accountId, confirmation) => request(`${root}/accounts/${encodeURIComponent(accountId)}/reset-profile`, { method: "POST", body: JSON.stringify({ confirmation }) });
export const deleteBaleAccount = (accountId) => request(`/automation/platforms/bale/accounts/${encodeURIComponent(accountId)}?delete_profile=true`, { method: "DELETE" });
export const preflightBaleAccount = (payload) => request(`${root}/preflight`, { method: "POST", body: JSON.stringify(payload) });
export const provisionBaleAccount = (payload) => request(`${root}/provision`, { method: "POST", body: JSON.stringify(payload) });
export const setBaleScheduling = (accountId, enabled) => request(`${root}/accounts/${encodeURIComponent(accountId)}/scheduling`, { method: "PUT", body: JSON.stringify({ enabled }) });
export const disableBaleAccount = (accountId) => request(`${root}/accounts/${encodeURIComponent(accountId)}/disable`, { method: "POST" });
export const retireBaleAccount = (accountId, reason) => request(`${root}/accounts/${encodeURIComponent(accountId)}/retire`, { method: "POST", body: JSON.stringify({ reason }) });
export const listBaleOnboardingBatches = () => request(`${root}/batches`);
export const createBaleOnboardingBatch = (payload) => request(`${root}/batches`, { method: "POST", body: JSON.stringify(payload) });
export const getBaleOnboardingOperation = (operationId) => request(`${root}/operations/${encodeURIComponent(operationId)}`);
export const listBaleAuditEvents = (accountId = "") => request(`${root}/audit-events${accountId ? `?account_id=${encodeURIComponent(accountId)}` : ""}`);
export const recoverStaleBaleSessions = () => request(`${root}/recover-stale-sessions`, { method: "POST" });
export const getBaleOperationalConfiguration = () => request(`${root}/configuration`);
export const updateBaleOperationalConfiguration = (configuration) => request(`${root}/configuration`, { method: "PUT", body: JSON.stringify({ configuration, actor: "operator-ui" }) });

export const auditBaleAuthentication = (accountId) => request(`/automation/platforms/bale/authentication/audit/${encodeURIComponent(accountId)}`);
export const openBaleAuthentication = (accountId, purpose = "login") => request("/automation/platforms/bale/authentication/open", { method: "POST", body: JSON.stringify({ account_id: accountId, purpose }) });
export const recheckBaleAuthentication = (accountId) => request("/automation/platforms/bale/authentication/session-recheck", { method: "POST", body: JSON.stringify({ account_id: accountId, purpose: "session_recheck" }) });
export const getBaleAccountOperation = (operationId) => request(`/automation/platforms/bale/account-operations/${encodeURIComponent(operationId)}`);
export const getBaleAuthenticationStatus = (sessionId) => request(`/automation/platforms/bale/authentication/status/${encodeURIComponent(sessionId)}`);
export const verifyBaleAuthentication = (sessionId) => request(`/automation/platforms/bale/authentication/verify/${encodeURIComponent(sessionId)}`, { method: "POST" });
export const closeBaleAuthentication = (sessionId) => request(`/automation/platforms/bale/authentication/close/${encodeURIComponent(sessionId)}`, { method: "POST" });
