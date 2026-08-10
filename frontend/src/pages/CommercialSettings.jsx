import { useEffect, useState } from "react";
import { RotateCw, Save } from "lucide-react";
import { getGlobalSettings, updateGlobalSettings } from "../api/settings";
import { ErrorState, LoadingState, PageHeader } from "../components/commercial/CommercialUi.jsx";

// These are the few sending limits an operator can meaningfully manage. Account
// allocation belongs to an individual campaign, not to this global screen.
const operatorFields = [
  ["deliveries_per_account_round", "ارسال در هر نوبت", "number"],
  ["delay_between_deliveries_seconds", "فاصله بین ارسال‌ها (ثانیه)", "number"],
  ["round_cooldown_seconds", "فاصله بین نوبت‌ها (ثانیه)", "number"],
  ["default_daily_limit_per_account", "سقف روزانه هر اکانت", "number"],
];

// Kept available to support staff without presenting scheduler implementation
// choices as part of the normal operator workflow.
const advancedFields = [
  ["concurrency_mode", "حالت همزمانی", "select"],
  ["operator_defined_max_concurrent_accounts", "حداکثر اکانت همزمان", "number"],
  ["browser_concurrency", "همزمانی مرورگر", "number"],
  ["worker_concurrency", "همزمانی Worker", "number"],
  ["default_source_channel_uid", "کانال منبع پیش‌فرض", "text"],
  ["account_assignment_strategy", "راهبرد تخصیص", "select"],
  ["max_job_duration_seconds", "حداکثر زمان کار", "number"],
  ["job_timeout_seconds", "مهلت کار", "number"],
  ["auto_pause_on_auth_error", "توقف خودکار در خطای ورود", "checkbox"],
  ["auto_pause_on_selector_error", "توقف خودکار در خطای انتخاب‌گر", "checkbox"],
  ["send_method", "روش ارسال", "text"],
  ["operation_order_json", "ترتیب عملیات", "text"],
  ["link_open_delay_seconds", "تاخیر بازشدن پیوند", "number"],
  ["browser_start_batch_size", "اندازه دسته شروع مرورگر", "number"],
  ["browser_start_stagger_ms", "فاصله شروع مرورگر", "number"],
  ["max_system_memory_percent", "حداکثر حافظه سیستم", "number"],
  ["max_system_cpu_percent", "حداکثر پردازنده سیستم", "number"],
  ["session_reuse_enabled", "استفاده مجدد از نشست", "checkbox"],
  ["resource_guard_enabled", "کنترل ظرفیت منابع", "checkbox"],
  ["campaign_overrides_enabled", "فعال بودن تنظیمات ویژه کمپین", "checkbox"],
  ["automatic_retry_enabled", "تلاش مجدد خودکار", "checkbox"],
  ["live_campaign_execution_enabled", "اجرای زنده کمپین", "checkbox"],
];

const allFields = [...operatorFields, ...advancedFields];
const strategies = ["round_robin", "least_daily_sent", "priority_then_least_sent"];

function payloadFromForm(form) {
  const payload = {};
  for (const [key, , type] of allFields) {
    if (type === "checkbox") payload[key] = !!form[key];
    else if (type === "number") payload[key] = Number(form[key] || 0);
    else if (key === "operation_order_json") {
      const value = form[key] ?? "";
      payload[key] = value.trim().startsWith("[") ? value : JSON.stringify(value.split(",").map((item) => item.trim()).filter(Boolean));
    } else payload[key] = form[key] ?? "";
  }
  return payload;
}

function SettingsField({ field, form, setForm }) {
  const [key, label, type] = field;
  if (type === "checkbox") {
    return <label className="checkbox-row"><input type="checkbox" checked={!!form[key]} onChange={(event) => setForm({ ...form, [key]: event.target.checked })} />{label}</label>;
  }
  if (type === "select") {
    const choices = key === "concurrency_mode" ? ["operator_defined", "unrestricted"] : strategies;
    return <label>{label}<select value={form[key] || choices[0]} onChange={(event) => setForm({ ...form, [key]: event.target.value })}>{choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}</select></label>;
  }
  return <label>{label}<input type={type} value={form[key] ?? ""} onChange={(event) => setForm({ ...form, [key]: event.target.value })} /></label>;
}

export default function CommercialSettings() {
  const [form, setForm] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState("");

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setForm(await getGlobalSettings());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }

  async function save() {
    setSaving(true);
    setError(null);
    setMessage("");
    try {
      await updateGlobalSettings(payloadFromForm(form));
      await load();
      setMessage("تنظیمات ذخیره شد.");
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => { load(); }, []);

  return (
    <section className="rtl-page commercial-page">
      <PageHeader title="تنظیمات ارسال" description="محدودیت‌های ساده و مشترک ارسال را مشخص کنید. تعداد اکانت هر کمپین در خود همان کمپین تعیین می‌شود.">
        <button className="secondary-button" type="button" onClick={load}><RotateCw size={16} />تازه‌سازی</button>
      </PageHeader>
      {error ? <ErrorState error={error} /> : null}
      {message ? <div className="toast">{message}</div> : null}
      {loading || !form ? <LoadingState /> : (
        <section className="panel">
          <div className="toast">سامانه ارسال به‌صورت امن مدیریت می‌شود.</div>
          <div className="settings-grid">
            {operatorFields.map((field) => <SettingsField key={field[0]} field={field} form={form} setForm={setForm} />)}
          </div>
          <div className="modal-actions">
            <button className="primary-button" type="button" disabled={saving} onClick={save}><Save size={16} />ذخیره</button>
          </div>
          <details className="advanced-settings">
            <summary>تنظیمات پیشرفته (فقط پشتیبانی فنی)</summary>
            <p className="page-copy">این گزینه‌ها برای اجرای روزمره کمپین لازم نیستند و تغییر آن‌ها باید با هماهنگی پشتیبانی انجام شود.</p>
            <div className="settings-grid">
              {advancedFields.map((field) => <SettingsField key={field[0]} field={field} form={form} setForm={setForm} />)}
            </div>
          </details>
        </section>
      )}
    </section>
  );
}
