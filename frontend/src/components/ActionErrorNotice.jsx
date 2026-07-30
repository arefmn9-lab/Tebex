import { useEffect, useState } from "react";
import { readableReport } from "../diagnostics";

export default function ActionErrorNotice() {
  const [event, setEvent] = useState(null);
  const [details, setDetails] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const receive = (message) => {
      setEvent(message.detail);
      setDetails(false);
      setCopied(false);
    };
    window.addEventListener("clinicos:action-error", receive);
    return () => window.removeEventListener("clinicos:action-error", receive);
  }, []);

  if (!event) return null;
  async function copy() {
    await navigator.clipboard.writeText(readableReport([event], { source: "failed_ui_action" }));
    setCopied(true);
  }

  return (
    <aside className="action-error-notice" role="alert" dir="rtl">
      <button className="modal-close" onClick={() => setEvent(null)} aria-label="بستن">×</button>
      <strong>عملیات انجام نشد</strong>
      <span>{event.error_message || "خطای غیرمنتظره رخ داد."}</span>
      <div className="diagnostics-preview-actions">
        <button className="secondary-button" onClick={() => setDetails(!details)}>View Details</button>
        <button className="secondary-button" onClick={copy}>{copied ? "Copied" : "Copy Diagnostics"}</button>
      </div>
      {details ? <pre className="diagnostics-file-preview" dir="ltr">{JSON.stringify(event, null, 2)}</pre> : null}
    </aside>
  );
}
