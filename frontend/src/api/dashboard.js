import { request } from "./client";

export function getDashboardSummary() {
  return request("/automation/dashboard/summary");
}
