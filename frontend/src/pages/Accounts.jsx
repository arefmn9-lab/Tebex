import StatusCard from "../components/StatusCard.jsx";
import { useAccounts } from "../hooks/useAccounts";

export default function Accounts() {
  const { accounts, activeAccounts, error } = useAccounts();

  return (
    <>
      <div className="page-header">
        <div>
          <h2 className="page-title">Accounts</h2>
          <p className="page-copy">Account isolation and activity indicators derived from automation runtime state.</p>
        </div>
      </div>

      <section className="grid metrics">
        <StatusCard label="Total Accounts" value={accounts.length} note="Detected accounts" tone="neutral" />
        <StatusCard label="Active Accounts" value={activeAccounts} note="Running tasks" tone="success" />
        <StatusCard label="Idle Accounts" value={Math.max(accounts.length - activeAccounts, 0)} note="No active task" tone="warning" />
        <StatusCard label="Isolation" value="On" note="Per-account task grouping" tone="success" />
      </section>

      <section className="panel" style={{ marginTop: 16 }}>
        <div className="panel-header">
          <h3 className="panel-title">Account Status</h3>
          <span className="pill">Polling</span>
        </div>
        {error ? <div className="error-state">{error}</div> : null}
        <table className="table">
          <thead>
            <tr>
              <th>Account</th>
              <th>Platform</th>
              <th>Status</th>
              <th>Active Tasks</th>
              <th>Total Tasks</th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((account) => (
              <tr key={account.account_id}>
                <td>{account.account_id}</td>
                <td>{account.platform || "automation"}</td>
                <td>
                  <span className="pill">
                    <span className={`status-dot ${account.status === "active" ? "success" : "neutral"}`} />
                    {account.status || "idle"}
                  </span>
                </td>
                <td>{account.active_tasks ?? 0}</td>
                <td>{account.total_tasks ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {accounts.length === 0 && !error ? <div className="empty-state">No account activity detected.</div> : null}
      </section>
    </>
  );
}

