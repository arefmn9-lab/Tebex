import { request } from "./client";

function queryString(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  }
  return query.toString();
}

export function previewPasteImport(campaignId, payload) {
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/recipient-imports/preview`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function uploadRecipientImport(campaignId, file, options = {}) {
  const form = new FormData();
  form.append("file", file);
  for (const [key, value] of Object.entries(options)) {
    if (value !== undefined && value !== null && value !== "") form.append(key, value);
  }
  return request(`/automation/campaigns/${encodeURIComponent(campaignId)}/recipient-imports/upload`, {
    method: "POST",
    body: form,
    headers: {},
  });
}

export function getRecipientImport(batchId) {
  return request(`/automation/recipient-imports/${encodeURIComponent(batchId)}`);
}

export function listRecipientImportItems(batchId, params = {}) {
  return request(`/automation/recipient-imports/${encodeURIComponent(batchId)}/items?${queryString(params)}`);
}

export function confirmRecipientImport(batchId, payload) {
  return request(`/automation/recipient-imports/${encodeURIComponent(batchId)}/confirm`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteRecipientImport(batchId) {
  return request(`/automation/recipient-imports/${encodeURIComponent(batchId)}`, { method: "DELETE" });
}
