import { request } from "./client";

export function listEvents(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  }
  return request(`/automation/events?${query.toString()}`);
}
