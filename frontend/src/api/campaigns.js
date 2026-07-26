import { request } from "./client";

function queryString(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  }
  return query.toString();
}

export function listCampaigns(params = {}) {
  return request(`/automation/campaigns?${queryString(params)}`);
}

export function getCampaign(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}`);
}

export function createCampaign(payload) {
  return request("/automation/campaigns", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateCampaign(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function validateCampaignStart(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/validate-start`, { method: "POST" });
}

export function queueCampaign(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/queue`, { method: "POST" });
}

export function startCampaign(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/start`, { method: "POST" });
}

export function pauseCampaign(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/pause`, { method: "POST" });
}

export function resumeCampaign(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/resume`, { method: "POST" });
}

export function cancelCampaign(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/cancel`, { method: "POST" });
}

export function runCampaignDryRound(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/run-dry-round`, { method: "POST" });
}

export function checkCampaignWithoutSending(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/check-without-sending`, { method: "POST" });
}

export function previewCampaignRecipients(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/recipients/preview`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function confirmCampaignRecipients(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/recipients/confirm`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function materializeCampaign(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/materialize`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function configureCampaignPlatformSettings(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/platform-settings`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listRecipientScenarios(campaignId, params = {}) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/recipient-scenarios?${queryString(params)}`);
}

export function prepareCampaignContacts(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/contacts/prepare`, { method: "POST" });
}

export function finalReviewCampaign(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/final-review`, { method: "POST" });
}

export function requestSendApproval(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/request-send-approval`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function livePreflightCampaign(campaignId, payload = {}) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/live-preflight`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getCampaignConfiguration(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/configuration`);
}

export function updateCampaignConfigurationDraft(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/configuration/draft`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function validateCampaignConfiguration(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/configuration/validate`, { method: "POST" });
}

export function getCampaignEffectiveConfiguration(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/configuration/effective`);
}

export function listCampaignConfigurationRevisions(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/configuration/revisions`);
}

export function createCampaignConfigurationRevision(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/configuration/revisions`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function approveCampaignConfigurationRevision(campaignId, revisionId, payload = {}) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/configuration/revisions/${encodeURIComponent(revisionId)}/approve`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function checkCampaignConfigurationDrift(campaignId) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/configuration/check-drift`, { method: "POST" });
}

export function validateLiveReadiness(campaignId, payload = {}) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/validate-live-readiness`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createLiveApproval(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/live-approvals`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listLiveApprovals(params = {}) {
  return request(`/automation/live-approvals?${queryString(params)}`);
}

export function listCampaignRecipients(campaignId, params = {}) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/recipients?${queryString(params)}`);
}

export function getRecipientAuthorization(recipientId) {
  return request(`/automation/recipients/${encodeURIComponent(recipientId)}/authorization`);
}

export function authorizeRecipientLive(recipientId, payload) {
  return request(`/automation/recipients/${encodeURIComponent(recipientId)}/authorize-live`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function revokeRecipientLive(recipientId, payload) {
  return request(`/automation/recipients/${encodeURIComponent(recipientId)}/revoke-live`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listBaleBulkCampaigns() {
  return request("/automation/platforms/bale/bulk-campaigns");
}

export function saveBaleBulkCampaign(payload) {
  return request("/automation/platforms/bale/bulk-campaigns", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getBaleBulkCampaign(campaignId) {
  return request(`/automation/platforms/bale/bulk-campaigns/${encodeURIComponent(campaignId)}`);
}

export function validateBaleBulkCampaign(campaignId, payload) {
  return request(`/automation/platforms/bale/bulk-campaigns/${encodeURIComponent(campaignId)}/validate`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function runBaleBulkDryPreflight(campaignId, payload = {}) {
  return request(`/automation/platforms/bale/bulk-campaigns/${encodeURIComponent(campaignId)}/dry-preflight`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getBaleBulkResults(campaignId) {
  return request(`/automation/platforms/bale/bulk-campaigns/${encodeURIComponent(campaignId)}/results`);
}

export function getBaleBulkResumeState(campaignId) {
  return request(`/automation/platforms/bale/bulk-campaigns/${encodeURIComponent(campaignId)}/resume-state`);
}

export function prepareBaleBulkLiveRun(campaignId, payload) {
  return request(`/automation/platforms/bale/bulk-campaigns/${encodeURIComponent(campaignId)}/live-run`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
