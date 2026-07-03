import { useCallback, useEffect, useState } from "react";
import { createTask, listTasks, runTask, stopTask } from "../api/automation";

export function useTasks(intervalMs = 3000) {
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const data = await listTasks();
      setTasks(Array.isArray(data) ? data : []);
      setError("");
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, refresh]);

  const create = useCallback(async (scenarioPath, runAt = null) => {
    const task = await createTask({ scenario_path: scenarioPath, run_at: runAt });
    await refresh();
    return task;
  }, [refresh]);

  const run = useCallback(async (taskId) => {
    const task = await runTask(taskId);
    await refresh();
    return task;
  }, [refresh]);

  const stop = useCallback(async (taskId) => {
    const task = await stopTask(taskId);
    await refresh();
    return task;
  }, [refresh]);

  return { tasks, loading, error, refresh, create, run, stop };
}

