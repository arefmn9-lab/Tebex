import { Play, Plus, Square } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../api/client";
import { useTasks } from "../hooks/useTasks";

export default function Tasks() {
  const { tasks, loading, error, create, run, stop } = useTasks();
  const [scenarioPath, setScenarioPath] = useState("backend/scenarios/example.json");
  const [actionError, setActionError] = useState("");

  async function handleCreate() {
    try {
      setActionError("");
      await create(scenarioPath);
    } catch (requestError) {
      setActionError(requestError.message);
    }
  }

  async function handleRun(taskId) {
    try {
      setActionError("");
      await run(taskId);
    } catch (requestError) {
      setActionError(requestError.message);
    }
  }

  async function handleStop(taskId) {
    try {
      setActionError("");
      await stop(taskId);
    } catch (requestError) {
      const unavailable = requestError instanceof ApiError && requestError.status === 404;
      setActionError(unavailable ? "Stop endpoint is not available in this backend." : requestError.message);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h2 className="page-title">Tasks</h2>
          <p className="page-copy">Create, run, and monitor automation tasks through the FastAPI control layer.</p>
        </div>
      </div>

      <section className="panel">
        <div className="toolbar">
          <input
            className="input"
            value={scenarioPath}
            onChange={(event) => setScenarioPath(event.target.value)}
            aria-label="Scenario path"
          />
          <button className="primary-button" onClick={handleCreate} type="button">
            <Plus size={17} />
            Create
          </button>
        </div>
        {actionError ? <p className="error-state">{actionError}</p> : null}
      </section>

      <section className="panel" style={{ marginTop: 16 }}>
        <div className="panel-header">
          <h3 className="panel-title">Task Queue</h3>
          <span className="pill">{loading ? "Loading" : `${tasks.length} tasks`}</span>
        </div>
        {error ? <div className="error-state">{error}</div> : null}
        <table className="table">
          <thead>
            <tr>
              <th>Task</th>
              <th>Account</th>
              <th>Status</th>
              <th>Scenario</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.task_id}>
                <td className="truncate">{task.task_id}</td>
                <td>{task.account_id || "default"}</td>
                <td><span className="pill">{task.status}</span></td>
                <td className="truncate">{task.scenario_path}</td>
                <td>
                  <div className="toolbar">
                    <button className="secondary-button" onClick={() => handleRun(task.task_id)} type="button">
                      <Play size={15} />
                      Run
                    </button>
                    <button className="danger-button" onClick={() => handleStop(task.task_id)} type="button">
                      <Square size={15} />
                      Stop
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {tasks.length === 0 && !error ? <div className="empty-state">No tasks found.</div> : null}
      </section>
    </>
  );
}

