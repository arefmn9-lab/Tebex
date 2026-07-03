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

