import { request } from "./client";

export function getSchedulerStatus() {
  return request("/automation/scheduler/status");
}

export function startScheduler() {
  return request("/automation/scheduler/start", { method: "POST" });
}

export function pauseScheduler() {
  return request("/automation/scheduler/pause", { method: "POST" });
}

export function resumeScheduler() {
  return request("/automation/scheduler/resume", { method: "POST" });
}

export function stopScheduler() {
  return request("/automation/scheduler/stop", { method: "POST" });
}

export function runSchedulerOnce(payload = {}) {
  return request("/automation/scheduler/run-once", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
