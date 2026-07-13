import { useEffect, useState } from "react";
import { RotateCw, Save } from "lucide-react";
import { applyGlobalAccountSettings } from "../api/commercialAccounts";
import { getEffectivePolicy, getGlobalSettings, getResourceStatus, updateGlobalSettings } from "../api/settings";
import { ErrorState, LoadingState, PageHeader } from "../components/commercial/CommercialUi.jsx";

const fields = [
  ["max_concurrent_accounts", "حداکثر اکانت همزمان", "number"],
  ["deliveries_per_account_round", "ارسال در هر راند", "number"],
  ["delay_between_deliveries_seconds", "تاخیر بین ارسال‌ها", "number"],
  ["round_cooldown_seconds", "cooldown هر راند", "number"],
  ["default_daily_limit_per_account", "سقف روزانه پیش‌فرض", "number"],
  ["default_source_channel_uid", "کانال منبع پیش‌فرض", "text"],
  ["account_assignment_strategy", "استراتژی تخصیص", "select"],
  ["max_job_duration_seconds", "حداکثر مدت جاب", "number"],
  ["job_timeout_seconds", "timeout جاب", "number"],
  ["auto_pause_on_auth_error", "توقف خودکار خطای ورود", "checkbox"],
  ["auto_pause_on_selector_error", "توقف خودکار خطای selector", "checkbox"],
];

const strategies = ["round_robin", "least_daily_sent", "priority_then_least_sent"];

fields.push(
  ["send_method", "روش ارسال", "text"],
  ["operation_order_json", "ترتیب عملیات", "text"],
  ["link_open_delay_seconds", "تاخیر باز شدن لینک", "number"],
  ["browser_start_batch_size", "اندازه بچ شروع مرورگر", "number"],
  ["browser_start_stagger_ms", "فاصله شروع مرورگر", "number"],
  ["max_system_memory_percent", "حداکثر حافظه سیستم", "number"],
  ["max_system_cpu_percent", "حداکثر پردازنده سیستم", "number"],
  ["session_reuse_enabled", "استفاده مجدد از نشست", "checkbox"],
  ["resource_guard_enabled", "کنترل ظرفیت منابع", "checkbox"],
  ["campaign_overrides_enabled", "فعال بودن تنظیمات کمپین", "checkbox"],
  ["automatic_retry_enabled", "تلاش مجدد خودکار", "checkbox"],
  ["live_campaign_execution_enabled", "اجرای زنده کمپین", "checkbox"],
);

function payloadFromForm(form) {
  const payload = {};
  for (const [key, , type] of fields) {
    if (type === "checkbox") payload[key] = !!form[key];
    else if (type === "number") payload[key] = Number(form[key] || 0);
    else if (key === "operation_order_json") {
      const value = form[key] ?? "";
      payload[key] = value.trim().startsWith("[") ? value : JSON.stringify(value.split(",").map((item) => item.trim()).filter(Boolean));
    } else payload[key] = form[key] ?? "";
  }
  return payload;
}

export default function CommercialSettings() {
  const [form, setForm] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState("");
  const [policyPreview, setPolicyPreview] = useState(null);
  const [resourceStatus, setResourceStatus] = useState(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setForm(await getGlobalSettings());
      setPolicyPreview(await getEffectivePolicy());
      setResourceStatus(await getResourceStatus());
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

  async function applyGlobal() {
    setSaving(true);
    setError(null);
    setMessage("");
    try {
      await applyGlobalAccountSettings();
      setMessage("تنظیمات عمومی اعمال شد و overrideهای صریح حفظ شدند.");
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <section className="rtl-page commercial-page">
      <PageHeader title="تنظیمات" description="تنظیمات عمومی زمان‌بند، ظرفیت‌ها، timeout و رفتار توقف ایمن">
        <button className="secondary-button" type="button" onClick={load}>
          <RotateCw size={16} />
          تازه‌سازی
        </button>
      </PageHeader>
      {error ? <ErrorState error={error} /> : null}
      {message ? <div className="toast">{message}</div> : null}
      <div className="toast">استفاده مجدد از نشست مرورگر فقط در همان اکانت و همان دور Worker انجام می‌شود.</div>
      {loading || !form ? (
        <LoadingState />
      ) : (
        <section className="panel">
          <div className="settings-grid">
            {fields.map(([key, label, type]) => (
              <label key={key} className={type === "checkbox" ? "checkbox-row" : ""}>
                {type === "checkbox" ? (
                  <>
                    <input type="checkbox" checked={!!form[key]} onChange={(event) => setForm({ ...form, [key]: event.target.checked })} />
                    {label}
                  </>
                ) : type === "select" ? (
                  <>
                    {label}
                    <select value={form[key] || strategies[0]} onChange={(event) => setForm({ ...form, [key]: event.target.value })}>
                      {strategies.map((strategy) => <option key={strategy} value={strategy}>{strategy}</option>)}
                    </select>
                  </>
                ) : (
                  <>
                    {label}
                    <input type={type} value={form[key] ?? ""} onChange={(event) => setForm({ ...form, [key]: event.target.value })} />
                  </>
                )}
              </label>
            ))}
          </div>
          <div className="modal-actions">
            <button className="primary-button" type="button" disabled={saving} onClick={save}>
              <Save size={16} />
              ذخیره
            </button>
            <button className="secondary-button" type="button" disabled={saving} onClick={applyGlobal}>
              اعمال تنظیمات عمومی روی اکانت‌ها
            </button>
          </div>
          <h3 className="subheading">پیش‌نمایش سیاست موثر</h3>
          {policyPreview ? (
            <pre className="diagnostics-pre">{JSON.stringify({
              effective_policy: policyPreview.effective_policy,
              policy_resolution_source: policyPreview.policy_resolution_source,
              validation_errors: policyPreview.validation_errors,
            }, null, 2)}</pre>
          ) : null}
          <h3 className="subheading">وضعیت منابع</h3>
          {resourceStatus ? <pre className="diagnostics-pre">{JSON.stringify(resourceStatus, null, 2)}</pre> : null}
        </section>
      )}
    </section>
  );
}
