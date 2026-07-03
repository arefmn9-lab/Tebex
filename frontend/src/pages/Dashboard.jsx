import StatusCard from "../components/StatusCard.jsx";
import LiveLogStream from "../components/LiveLogStream.jsx";
import { useAccounts } from "../hooks/useAccounts";
import { useLogs } from "../hooks/useLogs";
import { useTasks } from "../hooks/useTasks";

function latestDecision(tasks) {
  const task = [...tasks].reverse().find((item) => item.ai_decision || item.decision);
  return task?.ai_decision || task?.decision || null;
}

export default function Dashboard() {
  const { tasks, error: taskError } = useTasks();
  const { accounts, activeAccounts } = useAccounts();
  const { logs, error: logError } = useLogs();
  const running = tasks.filter((task) => task.status === "running").length;
  const done = tasks.filter((task) => ["done", "success"].includes(task.status)).length;
  const failed = tasks.filter((task) => task.status === "failed").length;
  const decision = latestDecision(tasks);

  return (
    <>
      <div className="page-header">
        <div>
          <h2 className="page-title">Dashboard</h2>
          <p className="page-copy">Monitor automation runtime health, task flow, accounts, and AI decisions.</p>
        </div>
      </div>

      <section className="grid metrics">
        <StatusCard label="Running" value={running} note="Active tasks" tone="warning" />
        <StatusCard label="Completed" value={done} note="Successful executions" tone="success" />
        <StatusCard label="Failed" value={failed} note="Needs review" tone="danger" />
        <StatusCard label="Accounts" value={accounts.length} note={`${activeAccounts} active`} tone="neutral" />
      </section>

      <section className="grid two" style={{ marginTop: 16 }}>
        <div className="panel">
          <div className="panel-header">
            <h3 className="panel-title">Live Logs</h3>
            <span className="pill">2.5s refresh</span>
          </div>
          <LiveLogStream logs={logs} error={logError || taskError} compact />
        </div>

        <div className="panel">
          <div className="panel-header">
            <h3 className="panel-title">AI Decision</h3>
            <span className="pill">Read-only</span>
          </div>
          {decision ? (
            <div className="decision-panel">
              <div className="decision-row"><span>Intent</span><strong>{decision.intent || "unknown"}</strong></div>
              <div className="decision-row"><span>Action</span><strong>{decision.action || "unknown"}</strong></div>
              <div className="decision-row"><span>Confidence</span><strong>{decision.confidence ?? "n/a"}</strong></div>
              <div className="decision-row"><span>Response</span><strong>{decision.response || "No response"}</strong></div>
            </div>
          ) : (
            <div className="empty-state">No AI decision data exposed by the API yet.</div>
          )}
        </div>
      </section>
    </>
  );
}

