import { request } from "./client";

export function listTasks() {
  return request("/automation/tasks");
}

export function createTask(payload) {
  return request("/automation/task/create", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function runTask(taskId) {
  return request("/automation/task/run", {
    method: "POST",
    body: JSON.stringify({ task_id: taskId })
  });
}

export function stopTask(taskId) {
  return request("/automation/task/stop", {
    method: "POST",
    body: JSON.stringify({ task_id: taskId })
  });
}

export function getTaskStatus(taskId) {
  return request(`/automation/task/status/${encodeURIComponent(taskId)}`);
}

export function listDiagnosticRuns() {
  return request("/automation/diagnostics/runs");
}

export function listDiagnosticRunFiles(runName) {
  return request(`/automation/diagnostics/runs/${encodeURIComponent(runName)}/files`);
}

export function previewDiagnosticFile(relativePath) {
  return request(`/automation/diagnostics/preview?path=${encodeURIComponent(relativePath)}`);
}

export function diagnosticDownloadUrl(relativePath) {
  const base = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
  return `${base}/automation/diagnostics/download?path=${encodeURIComponent(relativePath)}`;
}
