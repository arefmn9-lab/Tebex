import { request } from "./client";

export function listAccountRuntimeStatus(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  }
  return request(`/automation/accounts/runtime-status?${query.toString()}`);
}

export function listAccountSettings(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  }
  return request(`/automation/accounts/settings?${query.toString()}`);
}

export function getAccountSettings(accountId) {
  return request(`/automation/accounts/${encodeURIComponent(accountId)}/settings`);
}

export function updateAccountSettings(accountId, payload) {
  return request(`/automation/accounts/${encodeURIComponent(accountId)}/settings`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function applyGlobalAccountSettings() {
  return request("/automation/accounts/settings/apply-global", { method: "POST" });
}

export function listRuntimeSessions() {
  return request("/automation/runtime-sessions");
}

export function getRuntimeSession(accountId) {
  return request(`/automation/runtime-sessions/${encodeURIComponent(accountId)}`);
}

export function closeRuntimeSession(accountId) {
  return request(`/automation/runtime-sessions/${encodeURIComponent(accountId)}/close`, { method: "POST" });
}

export function listBrowserIdentities() {
  return request("/automation/browser-identities");
}

export function getBrowserIdentity(accountId) {
  return request(`/automation/browser-identities/${encodeURIComponent(accountId)}`);
}

export function updateBrowserIdentity(accountId, payload) {
  return request(`/automation/browser-identities/${encodeURIComponent(accountId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function validateBrowserIdentity(accountId) {
  return request(`/automation/browser-identities/${encodeURIComponent(accountId)}/validate`, { method: "POST" });
}

export function migrateBrowserIdentities(payload = { dry_run: true }) {
  return request("/automation/browser-identities/migrate-existing", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listAccountHealth() {
  return request("/automation/accounts/health");
}

export function requireAccountReview(accountId) {
  return request(`/automation/accounts/${encodeURIComponent(accountId)}/health/require-review`, { method: "POST" });
}

export function resetAccountWarning(accountId) {
  return request(`/automation/accounts/${encodeURIComponent(accountId)}/health/reset-warning`, { method: "POST" });
}

export function enableAccountHealth(accountId) {
  return request(`/automation/accounts/${encodeURIComponent(accountId)}/health/enable`, { method: "POST" });
}

export function disableAccountHealth(accountId) {
  return request(`/automation/accounts/${encodeURIComponent(accountId)}/health/disable`, { method: "POST" });
}

export function openBaleAuthentication(accountId) {
  return request("/automation/platforms/bale/authentication/open", {
    method: "POST",
    body: JSON.stringify({ account_id: accountId }),
  });
}

export function getBaleAuthenticationStatus(maintenanceSessionId) {
  return request(`/automation/platforms/bale/authentication/status/${encodeURIComponent(maintenanceSessionId)}`);
}

export function verifyBaleAuthentication(maintenanceSessionId) {
  return request(`/automation/platforms/bale/authentication/verify/${encodeURIComponent(maintenanceSessionId)}`, { method: "POST" });
}

export function closeBaleAuthentication(maintenanceSessionId) {
  return request(`/automation/platforms/bale/authentication/close/${encodeURIComponent(maintenanceSessionId)}`, { method: "POST" });
}
