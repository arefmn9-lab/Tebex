import { useEffect, useState } from "react";
import { readableReport } from "../diagnostics";

const operatorMessages = {
  no_eligible_bale_accounts: "اکانت آماده‌ای برای این کمپین پیدا نشد. ابتدا وضعیت اکانت‌ها را بررسی کنید.",
  requested_accounts_exceed_eligible: "تعداد اکانت درخواستی بیش از اکانت‌های آماده است.",
  campaign_capacity_pool_insufficient: "تعداد اکانت درخواستی در حال حاضر در دسترس نیست.",
  fake_session_recheck_failure: "بررسی نشست ناموفق بود. دوباره وارد اکانت شوید و سپس تلاش کنید.",
  authentication_required: "برای این اکانت ابتدا ورود را کامل کنید.",
};

function operatorErrorMessage(event) {
  return operatorMessages[event?.error_code] || event?.error_message || "خطای غیرمنتظره رخ داد.";
}

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
      <span>{operatorErrorMessage(event)}</span>
      <div className="diagnostics-preview-actions">
        <button className="secondary-button" onClick={() => setDetails(!details)}>جزئیات فنی</button>
        <button className="secondary-button" onClick={copy}>{copied ? "کپی شد" : "کپی گزارش"}</button>
      </div>
      {details ? <pre className="diagnostics-file-preview" dir="ltr">{JSON.stringify(event, null, 2)}</pre> : null}
    </aside>
  );
}
