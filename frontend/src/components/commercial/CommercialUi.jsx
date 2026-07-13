import { X } from "lucide-react";

export function statusTone(value) {
  const status = String(value || "").toLowerCase();
  if (["succeeded", "success", "running", "active", "started"].includes(status)) return status === "running" ? "info" : "success";
  if (["failed", "error", "cancelled", "stopped"].includes(status)) return "danger";
  if (["paused", "skipped", "cooling_down", "assigned", "queued"].includes(status)) return "warning";
  if (status === "idle" || status === "draft") return "neutral";
  return "neutral";
}

export function StatusBadge({ value }) {
  return <span className={`status-badge ${statusTone(value)}`}>{value ?? "-"}</span>;
}

export function fmt(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

export function shortId(value, length = 12) {
  const text = fmt(value);
  return text.length > length ? `${text.slice(0, length)}...` : text;
}

export function PageHeader({ title, description, children }) {
  return (
    <div className="page-header commercial-header">
      <div>
        <h2 className="page-title">{title}</h2>
        {description ? <p className="page-copy">{description}</p> : null}
      </div>
      {children ? <div className="toolbar">{children}</div> : null}
    </div>
  );
}

export function LoadingState() {
  return <div className="empty-state">در حال بارگذاری...</div>;
}

export function EmptyState({ children = "داده‌ای برای نمایش وجود ندارد." }) {
  return <div className="empty-state">{children}</div>;
}

export function ErrorState({ error }) {
  return <div className="error-state">{error?.message || String(error || "خطا در دریافت داده")}</div>;
}

export function Modal({ title, onClose, children, wide = false }) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className={`modal-panel commercial-modal ${wide ? "wide-modal" : ""}`} dir="rtl">
        <div className="modal-header">
          <h3>{title}</h3>
          <button className="icon-button" type="button" onClick={onClose} aria-label="بستن">
            <X size={16} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

export function KeyValueGrid({ data }) {
  return (
    <dl className="kv-grid">
      {Object.entries(data || {}).map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{typeof value === "object" && value !== null ? JSON.stringify(value) : fmt(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Pager({ limit, offset, onPage }) {
  return (
    <div className="pager">
      <button className="secondary-button" type="button" disabled={offset <= 0} onClick={() => onPage(Math.max(0, offset - limit))}>
        قبلی
      </button>
      <span>از ردیف {offset + 1}</span>
      <button className="secondary-button" type="button" onClick={() => onPage(offset + limit)}>
        بعدی
      </button>
    </div>
  );
}
