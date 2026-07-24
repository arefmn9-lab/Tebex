import { request } from "./client";

export function getAccountRegistrySummary() {
  return request("/automation/account-registry/summary");
}

export function listPlatformAccounts(platformId) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}/accounts`);
}

export function createPlatformAccount(platformId, payload) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}/accounts`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updatePlatformAccount(platformId, accountId, payload) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}/accounts/${encodeURIComponent(accountId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function deletePlatformAccount(platformId, accountId) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}/accounts/${encodeURIComponent(accountId)}`, {
    method: "DELETE",
  });
}
