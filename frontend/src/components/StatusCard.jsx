export default function StatusCard({ label, value, note, tone = "neutral" }) {
  return (
    <section className="status-card">
      <div className="status-label">
        <span className={`status-dot ${tone}`} />
        {label}
      </div>
      <p className="status-value">{value}</p>
      {note ? <p className="status-note">{note}</p> : null}
    </section>
  );
}

