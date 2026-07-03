import { request } from "./client";
import { listTasks } from "./automation";

export async function listLogs() {
  try {
    return await request("/automation/logs");
  } catch (error) {
    if (error.status !== 404) {
      throw error;
    }

    const tasks = await listTasks();
    return tasks.flatMap((task) =>
      (task.logs || []).map((message) => ({
        task_id: task.task_id,
        account_id: task.account_id || "default",
        status: task.status,
        message
      }))
    );
  }
}

