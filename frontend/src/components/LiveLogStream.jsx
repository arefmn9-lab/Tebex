export default function LiveLogStream({ logs, error, compact = false }) {
  if (error) {
    return <div className="error-state">{error}</div>;
  }

  const visibleLogs = compact ? logs.slice(-12) : logs.slice(-80);

  if (visibleLogs.length === 0) {
    return <div className="empty-state">No logs available yet.</div>;
  }

  return (
    <div className="logs" aria-live="polite">
      {visibleLogs.map((entry, index) => {
        const message = typeof entry === "string" ? entry : entry.message;
        const prefix = typeof entry === "string" ? "" : `[${entry.account_id || "default"}] `;
        return (
          <pre className="log-line" key={`${message}-${index}`}>
            {prefix}{message}
          </pre>
        );
      })}
    </div>
  );
}

