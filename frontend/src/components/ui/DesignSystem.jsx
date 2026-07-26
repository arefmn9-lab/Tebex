import { ChevronDown, Loader2, X } from "lucide-react";

export function PageHeader({ title, description, actions }) {
  return (
    <div className="page-header ds-page-header">
      <div>
        <h1 className="page-title">{title}</h1>
        {description ? <p className="page-copy">{description}</p> : null}
      </div>
      {actions ? <div className="toolbar">{actions}</div> : null}
    </div>
  );
}

function ButtonBase({ className, children, ...props }) {
  return <button className={className} type="button" {...props}>{children}</button>;
}

export function PrimaryButton(props) {
  return <ButtonBase className="primary-button" {...props} />;
}

export function SecondaryButton(props) {
  return <ButtonBase className="secondary-button" {...props} />;
}

export function SuccessButton(props) {
  return <ButtonBase className="success-button" {...props} />;
}

export function DangerButton(props) {
  return <ButtonBase className="danger-button" {...props} />;
}

export function IconButton({ label, children, ...props }) {
  return (
    <button className="icon-button" type="button" aria-label={label} title={label} {...props}>
      {children}
    </button>
  );
}

export function StatCard({ label, value, description, icon }) {
  return (
    <article className="stat-card">
      {icon ? <span className="stat-card-icon">{icon}</span> : null}
      <span>{label}</span>
      <strong>{value}</strong>
      {description ? <small>{description}</small> : null}
    </article>
  );
}

export function ContentCard({ title, description, children, actions }) {
  return (
    <section className="content-card">
      {(title || actions) ? (
        <div className="panel-header">
          <div>
            {title ? <h2 className="panel-title">{title}</h2> : null}
            {description ? <p className="page-copy">{description}</p> : null}
          </div>
          {actions ? <div className="toolbar">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function TabBar({ tabs, activeTab, onChange }) {
  return (
    <div className="tab-bar" role="tablist">
      {tabs.map((tab) => (
        <button
          aria-selected={activeTab === tab.id}
          className={activeTab === tab.id ? "active" : ""}
          key={tab.id}
          onClick={() => onChange(tab.id)}
          role="tab"
          type="button"
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function StatusBadge({ tone = "neutral", children }) {
  return <span className={`status-badge ${tone}`}>{children}</span>;
}

export function FormField({ label, hint, error, children }) {
  return (
    <label className="form-field">
      <span>{label}</span>
      {children}
      {hint ? <small>{hint}</small> : null}
      {error ? <InlineError>{error}</InlineError> : null}
    </label>
  );
}

export function TextInput(props) {
  return <input className="text-input" type="text" {...props} />;
}

export function NumberInput(props) {
  return <input className="text-input" type="number" {...props} />;
}

export function TextArea(props) {
  return <textarea className="text-area" {...props} />;
}

export function Select({ children, ...props }) {
  return (
    <span className="select-wrap">
      <select className="text-input" {...props}>{children}</select>
      <ChevronDown size={16} />
    </span>
  );
}

export function Checkbox({ label, ...props }) {
  return (
    <label className="checkbox-row">
      <input type="checkbox" {...props} />
      <span>{label}</span>
    </label>
  );
}

export function PlatformCard({ platform, selected, disabled, onClick }) {
  const Icon = platform.icon;
  return (
    <button
      aria-pressed={selected}
      className={`platform-card ${selected ? "selected" : ""}`}
      disabled={disabled}
      onClick={onClick}
      type="button"
    >
      <span className="platform-icon"><Icon size={22} /></span>
      <span className="platform-card-body">
        <strong>{platform.name}</strong>
        <small>{platform.summary}</small>
      </span>
      <StatusBadge tone={platform.ready ? "success" : platform.statusTone}>{platform.statusLabel}</StatusBadge>
    </button>
  );
}

export function PlatformSelector({ children }) {
  return <div className="platform-grid">{children}</div>;
}

export function Modal({ title, onClose, children }) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section aria-modal="true" className="modal-panel" dir="rtl" role="dialog" aria-label={title}>
        <div className="modal-header">
          <h2>{title}</h2>
          <IconButton label="بستن" onClick={onClose}><X size={17} /></IconButton>
        </div>
        {children}
      </section>
    </div>
  );
}

export function ConfirmDialog({ title, children, onConfirm, onCancel, confirmLabel = "تأیید" }) {
  return (
    <Modal title={title} onClose={onCancel}>
      <div className="confirm-dialog-body">{children}</div>
      <div className="modal-actions">
        <DangerButton onClick={onConfirm}>{confirmLabel}</DangerButton>
        <SecondaryButton onClick={onCancel}>انصراف</SecondaryButton>
      </div>
    </Modal>
  );
}

export function EmptyState({ title = "موردی برای نمایش وجود ندارد", description, action }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      {description ? <p>{description}</p> : null}
      {action}
    </div>
  );
}

export function DataTable({ columns, rows, rowKey }) {
  return (
    <div className="table-scroll">
      <table className="table data-table">
        <thead><tr>{columns.map((column) => <th key={column.key}>{column.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={rowKey ? rowKey(row) : index}>
              {columns.map((column) => <td key={column.key}>{column.render ? column.render(row) : row[column.key]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function MobileDataCard({ title, rows }) {
  return (
    <article className="mobile-data-card">
      <strong>{title}</strong>
      {rows.map((row) => <p key={row.label}><span>{row.label}</span><b>{row.value}</b></p>)}
    </article>
  );
}

export function CollapsibleAdvancedSettings({ title = "تنظیمات پیشرفته", children }) {
  return (
    <details className="advanced-settings">
      <summary>{title}</summary>
      <div>{children}</div>
    </details>
  );
}

export function LoadingState({ label = "در حال بارگذاری" }) {
  return <div className="loading-state"><Loader2 size={18} className="spin" />{label}</div>;
}

export function InlineError({ children }) {
  return <span className="inline-error">{children}</span>;
}
