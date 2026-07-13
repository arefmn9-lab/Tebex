import { request } from "./client";

export function getGlobalSettings() {
  return request("/automation/settings/global");
}

export function updateGlobalSettings(payload) {
  return request("/automation/settings/global", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function getEffectivePolicy(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  }
  return request(`/automation/policy/effective?${query.toString()}`);
}

export function getResourceStatus() {
  return request("/automation/resources/status");
}

export function getArchitectureCapabilities() {
  return request("/automation/architecture/capabilities");
}
