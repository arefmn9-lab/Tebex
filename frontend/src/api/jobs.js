import { request } from "./client";

function queryString(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  }
  return query.toString();
}

export function listJobs(params = {}) {
  return request(`/automation/jobs?${queryString(params)}`);
}

export function getJob(jobId) {
  return request(`/automation/jobs/${encodeURIComponent(jobId)}`);
}

export function listJobEvents(jobId, params = {}) {
  return request(`/automation/jobs/${encodeURIComponent(jobId)}/events?${queryString(params)}`);
}
