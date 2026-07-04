import { request, ApiError } from "./client";

export function listBackendPlatforms() {
  return request("/automation/platforms");
}

export function listBrowserProviders() {
  return request("/automation/browser/providers");
}

export function getAdsPowerConfig() {
  return request("/automation/browser/providers/adspower/config");
}

export function saveAdsPowerConfig(payload) {
  return request("/automation/browser/providers/adspower/config", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function checkAdsPowerHealth() {
  return request("/automation/browser/providers/adspower/health");
}

export function getBackendPlatform(platformId) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}`);
}

export function listPlatformAccounts(platformId) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}/accounts`);
}

export function listAccountGroups() {
  return request("/automation/account-groups");
}

export function listPlatformAccountGroups(platformId) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}/account-groups`);
}

export function createAccountGroup(payload) {
  return request("/automation/account-groups", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateAccountGroup(groupId, payload) {
  return request(`/automation/account-groups/${encodeURIComponent(groupId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function listBulkMessageSources() {
  return request("/automation/bulk/message-sources");
}

export function createBulkMessageSource(payload) {
  return request("/automation/bulk/message-sources", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateBulkMessageSource(sourceId, payload) {
  return request(`/automation/bulk/message-sources/${encodeURIComponent(sourceId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function listBulkContactLists() {
  return request("/automation/bulk/contact-lists");
}

export function createBulkContactList(payload) {
  return request("/automation/bulk/contact-lists", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateBulkContactList(contactListId, payload) {
  return request(`/automation/bulk/contact-lists/${encodeURIComponent(contactListId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function importBulkContactList(formData) {
  return request("/automation/bulk/contact-lists/import", {
    method: "POST",
    body: formData,
    headers: {},
  });
}

export function listBulkContacts(contactListId) {
  return request(`/automation/bulk/contact-lists/${encodeURIComponent(contactListId)}/contacts`);
}

export function getBulkContactListSummary(contactListId) {
  return request(`/automation/bulk/contact-lists/${encodeURIComponent(contactListId)}/summary`);
}

export function listBulkCampaigns() {
  return request("/automation/bulk/campaigns");
}

export function createBulkCampaign(payload) {
  return request("/automation/bulk/campaigns", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateBulkCampaign(campaignId, payload) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function createBulkCampaignRoute(campaignId, payload) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/routes`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateBulkCampaignRoute(campaignId, routeId, payload) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/routes/${encodeURIComponent(routeId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function planBulkCampaign(campaignId) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/plan`, {
    method: "POST",
  });
}

export function assignBulkCampaign(campaignId, payload) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/assign`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listBulkAssignments(campaignId) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/assignments`);
}

export function getBulkAssignmentSummary(campaignId) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/assignments/summary`);
}

export function createBulkExecutionQueue(campaignId, payload) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/queue`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listBulkExecutionQueue(campaignId) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/queue`);
}

export function getBulkExecutionQueueSummary(campaignId) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/queue/summary`);
}

export function dryRunBulkExecutionQueue(campaignId, payload) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/queue/dry-run`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function runBaleExecutionQueue(campaignId, payload) {
  return request(`/automation/bulk/campaigns/${encodeURIComponent(campaignId)}/queue/bale/run`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createPlatformAccount(platformId, payload) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}/accounts`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateBaleAccount(accountId, payload) {
  return request(`/automation/platforms/bale/accounts/${encodeURIComponent(accountId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function deleteBaleAccount(accountId) {
  return request(`/automation/platforms/bale/accounts/${encodeURIComponent(accountId)}`, {
    method: "DELETE",
  });
}

export function listBaleProfileGroups() {
  return request("/automation/platforms/bale/profile-groups");
}

export function createBaleProfileGroup(payload) {
  return request("/automation/platforms/bale/profile-groups", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateBaleProfileGroup(deviceGroupId, payload) {
  return request(`/automation/platforms/bale/profile-groups/${encodeURIComponent(deviceGroupId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function deleteBaleProfileGroup(deviceGroupId) {
  return request(`/automation/platforms/bale/profile-groups/${encodeURIComponent(deviceGroupId)}`, {
    method: "DELETE",
  });
}

export function assignBaleProfileGroup(accountId, payload) {
  return request(`/automation/platforms/bale/accounts/${encodeURIComponent(accountId)}/assign-profile-group`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listPlatformTasks(platformId) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}/tasks`);
}

export function listPlatformLogs(platformId) {
  return request(`/automation/platforms/${encodeURIComponent(platformId)}/logs`);
}

export function openBaleAccount(accountId) {
  return request("/automation/platforms/bale/open-account", {
    method: "POST",
    body: JSON.stringify({ account_id: accountId }),
  });
}

export function sendBaleTestMessage(payload) {
  return request("/automation/platforms/bale/send-test", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getBaleMessageConfig() {
  return request("/automation/platforms/bale/message-config");
}

export function saveBaleMessageConfig(payload) {
  return request("/automation/platforms/bale/message-config", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function listBaleScenarios() {
  return request("/automation/platforms/bale/scenarios");
}

export function testBaleForward(payload) {
  return request("/automation/platforms/bale/test-forward", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function dryRunBaleSchedule(payload) {
  return request("/automation/platforms/bale/schedule/dry-run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getBalePreparation() {
  return request("/automation/platforms/bale/preparation");
}

export function saveBalePreparation(payload) {
  return request("/automation/platforms/bale/preparation", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function dryRunBalePreparation() {
  return request("/automation/platforms/bale/preparation/dry-run", {
    method: "POST",
  });
}

export async function openAccountBrowser(accountId) {
  try {
    return await request(`/automation/accounts/${encodeURIComponent(accountId)}/open-browser`, {
      method: "POST",
      body: JSON.stringify({ account_id: accountId }),
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return {
        unavailable: true,
        message: "Open browser endpoint is not implemented yet",
      };
    }
    if (error instanceof TypeError && error.message === "Failed to fetch") {
      return {
        unavailable: true,
        message: "Open browser endpoint is not implemented yet",
      };
    }
    throw error;
  }
}
