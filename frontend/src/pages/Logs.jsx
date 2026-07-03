import LiveLogStream from "../components/LiveLogStream.jsx";
import { useLogs } from "../hooks/useLogs";

export default function Logs() {
  const { logs, error } = useLogs();

  return (
    <>
      <div className="page-header">
        <div>
          <h2 className="page-title">Logs</h2>
          <p className="page-copy">Live execution stream with task and account context where available.</p>
        </div>
      </div>

      <section className="panel">
        <div className="panel-header">
          <h3 className="panel-title">Runtime Stream</h3>
          <span className="pill">{logs.length} entries</span>
        </div>
        <LiveLogStream logs={logs} error={error} />
      </section>
    </>
  );
}

