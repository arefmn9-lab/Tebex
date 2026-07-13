import { useEffect, useState } from "react";
import { Edit3, RotateCw } from "lucide-react";
import {
  disableAccountHealth,
  enableAccountHealth,
  getAccountSettings,
  closeBaleAuthentication,
  getBaleAuthenticationStatus,
  listAccountHealth,
  listAccountRuntimeStatus,
  listBrowserIdentities,
  listRuntimeSessions,
  migrateBrowserIdentities,
  openBaleAuthentication,
  requireAccountReview,
  resetAccountWarning,
  updateBrowserIdentity,
  updateAccountSettings,
  validateBrowserIdentity,
  verifyBaleAuthentication,
} from "../api/commercialAccounts";
import { EmptyState, ErrorState, LoadingState, Modal, PageHeader, StatusBadge, fmt, shortId } from "../components/commercial/CommercialUi.jsx";

const editableFields = [
  ["enabled", "فعال", "checkbox"],
  ["priority", "اولویت", "number"],
  ["daily_limit_override", "سقف روزانه", "number"],
  ["deliveries_per_round_override", "تعداد در هر راند", "number"],
  ["delay_between_deliveries_override", "تاخیر بین ارسال‌ها", "number"],
  ["round_cooldown_override", "cooldown راند", "number"],
  ["source_channel_uid_override", "کانال منبع", "text"],
];

function normalizePayload(form) {
  const payload = { enabled: !!form.enabled };
  for (const [key, , type] of editableFields) {
    if (key === "enabled") continue;
    const value = form[key];
    if (value === "" || value === undefined || value === null) {
      payload[key] = null;
    } else {
      payload[key] = type === "number" ? Number(value) : value;
    }
  }
  return payload;
}

export default function CommercialAccounts() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState({});
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [authSessions, setAuthSessions] = useState({});

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [data, sessions, identities, health] = await Promise.all([
        listAccountRuntimeStatus({ limit: 100, offset: 0 }),
        listRuntimeSessions().catch(() => ({ items: [] })),
        listBrowserIdentities().catch(() => ({ items: [] })),
        listAccountHealth().catch(() => ({ items: [] })),
      ]);
      const byAccount = Object.fromEntries((sessions.items || []).map((session) => [session.account_id, session]));
      const identityByAccount = Object.fromEntries((identities.items || []).map((identity) => [identity.account_id, identity]));
      const healthByAccount = Object.fromEntries((health.items || []).map((item) => [item.account_id, item]));
      setRows((data.items || []).map((row) => ({ ...row, runtime_session: byAccount[row.account_id] || null, browser_identity: identityByAccount[row.account_id] || null, account_health: healthByAccount[row.account_id] || null })));
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }

  async function openEdit(accountId) {
    setError(null);
    try {
      const data = await getAccountSettings(accountId);
      setEditing(data);
      setForm(data);
    } catch (err) {
      setError(err);
    }
  }

  async function saveEdit() {
    setSaving(true);
    setError(null);
    try {
      await updateAccountSettings(editing.account_id, normalizePayload(form));
      setEditing(null);
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function runAccountAction(action, successMessage) {
    setError(null);
    setMessage("");
    try {
      await action();
      setMessage(successMessage);
      await load();
    } catch (err) {
      setError(err);
    }
  }

  async function editIdentityMetadata(row) {
    const locale = window.prompt("locale", row.browser_identity?.locale || "fa-IR");
    if (locale === null) return;
    const timezone_id = window.prompt("timezone", row.browser_identity?.timezone_id || "Asia/Tehran");
    if (timezone_id === null) return;
    const network_route_id = window.prompt("network_route_id", row.browser_identity?.network_route_id || "");
    if (network_route_id === null) return;
    const worker_node_id = window.prompt("worker_node_id", row.browser_identity?.worker_node_id || "local_windows_1");
    if (worker_node_id === null) return;
    await runAccountAction(
      () => updateBrowserIdentity(row.account_id, { locale, timezone_id, network_route_id: network_route_id || null, worker_node_id: worker_node_id || null }),
      "متادیتای هویت مرورگر ذخیره شد.",
    );
  }

  async function openManualBaleLogin(row) {
    await runAccountAction(async () => {
      const session = await openBaleAuthentication(row.account_id);
      setAuthSessions((current) => ({ ...current, [row.account_id]: session }));
    }, "نشست ورود دستی بله باز شد.");
  }

  async function checkManualBaleLogin(row) {
    const sessionId = authSessions[row.account_id]?.maintenance_session_id;
    if (!sessionId) {
      setMessage("ابتدا نشست ورود دستی را باز کنید.");
      return;
    }
    await runAccountAction(async () => {
      const status = await getBaleAuthenticationStatus(sessionId);
      setAuthSessions((current) => ({ ...current, [row.account_id]: status }));
    }, "وضعیت ورود بررسی شد.");
  }

  async function verifyManualBaleLogin(row) {
    const sessionId = authSessions[row.account_id]?.maintenance_session_id;
    if (!sessionId) {
      setMessage("ابتدا نشست ورود دستی را باز کنید.");
      return;
    }
    await runAccountAction(async () => {
      const status = await verifyBaleAuthentication(sessionId);
      setAuthSessions((current) => ({ ...current, [row.account_id]: status }));
    }, "احراز هویت بله بررسی شد.");
  }

  async function closeManualBaleLogin(row) {
    const sessionId = authSessions[row.account_id]?.maintenance_session_id;
    if (!sessionId) return;
    await runAccountAction(async () => {
      await closeBaleAuthentication(sessionId);
      setAuthSessions((current) => ({ ...current, [row.account_id]: null }));
    }, "نشست ورود دستی بسته شد.");
  }

  return (
    <section className="rtl-page commercial-page">
      <PageHeader title="اکانت‌ها" description="وضعیت runtime، ظرفیت روزانه، قفل‌ها و تنظیمات اختصاصی هر اکانت">
        <button className="secondary-button" type="button" onClick={load}>
          <RotateCw size={16} />
          تازه‌سازی
        </button>
      </PageHeader>
      {error ? <ErrorState error={error} /> : null}
      {message ? <div className="toast">{message}</div> : null}
      <div className="notice-card">
        ورود را مستقیماً در پنجره رسمی بله انجام دهید. ClinicOS رمز یا کد تأیید را دریافت و ذخیره نمی‌کند.
      </div>
      <div className="modal-actions">
        <button className="secondary-button" type="button" onClick={() => runAccountAction(() => migrateBrowserIdentities({ dry_run: true }), "پیش‌نمایش مهاجرت هویت مرورگر اجرا شد.")}>پیش‌نمایش مهاجرت هویت</button>
      </div>
      {loading ? (
        <LoadingState />
      ) : rows.length ? (
        <div className="table-scroll">
          <table className="table rtl-table commercial-table">
            <thead>
              <tr>
                <th>account_id</th>
                <th>فعال</th>
                <th>اولویت</th>
                <th>worker_status</th>
                <th>lock</th>
                <th>active_job</th>
                <th>ارسال روزانه</th>
                <th>سقف روزانه</th>
                <th>راند</th>
                <th>سقف راند</th>
                <th>cooldown_until</th>
                <th>source_channel</th>
                <th>last_started</th>
                <th>last_completed</th>
                <th>last_error_code</th>
                <th>last_error_message</th>
                <th>نشست فعال</th>
                <th>شناسه نشست</th>
                <th>jobهای نشست</th>
                <th>سلامت نشست</th>
                <th>دلیل ابطال</th>
                <th>میانگین job</th>
                <th>identity</th>
                <th>profile</th>
                <th>validation</th>
                <th>locale</th>
                <th>timezone</th>
                <th>viewport</th>
                <th>version</th>
                <th>network route</th>
                <th>worker node</th>
                <th>health</th>
                <th>failures</th>
                <th>last health error</th>
                <th>manual review</th>
                <th>health actions</th>
                <th>وضعیت احراز هویت</th>
                <th>نشست ورود دستی</th>
                <th>ویرایش</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.account_id}>
                  <td>{row.account_id}</td>
                  <td><StatusBadge value={row.enabled ? "enabled" : "disabled"} /></td>
                  <td>{fmt(row.priority)}</td>
                  <td><StatusBadge value={row.worker_status} /></td>
                  <td><StatusBadge value={row.lock_active ? "active" : "none"} /></td>
                  <td>{shortId(row.active_job_id)}</td>
                  <td>{fmt(row.current_daily_sent_count)}</td>
                  <td>{fmt(row.effective_daily_limit)}</td>
                  <td>{fmt(row.current_round_sent_count)}</td>
                  <td>{fmt(row.effective_deliveries_per_round)}</td>
                  <td>{fmt(row.cooldown_until)}</td>
                  <td>{fmt(row.source_channel_uid)}</td>
                  <td>{fmt(row.last_job_started_at)}</td>
                  <td>{fmt(row.last_job_completed_at)}</td>
                  <td>{fmt(row.last_error_code)}</td>
                  <td className="truncate">{fmt(row.last_error_message)}</td>
                  <td><StatusBadge value={row.runtime_session ? "active" : "none"} /></td>
                  <td>{shortId(row.runtime_session?.session_id)}</td>
                  <td>{fmt(row.runtime_session?.jobs_processed)}</td>
                  <td>{fmt(row.runtime_session?.healthy)}</td>
                  <td>{fmt(row.runtime_session?.invalidated_reason)}</td>
                  <td>{fmt(row.runtime_session?.average_job_duration_ms)}</td>
                  <td>{shortId(row.browser_identity?.identity_id)}</td>
                  <td className="truncate">{fmt(row.browser_identity?.normalized_profile_path || row.browser_identity?.profile_path)}</td>
                  <td>{fmt(row.browser_identity?.validation_status)}</td>
                  <td>{fmt(row.browser_identity?.locale)}</td>
                  <td>{fmt(row.browser_identity?.timezone_id)}</td>
                  <td>{row.browser_identity ? `${row.browser_identity.viewport_width}x${row.browser_identity.viewport_height}` : "-"}</td>
                  <td>{fmt(row.browser_identity?.identity_version)}</td>
                  <td>{fmt(row.browser_identity?.network_route_id)}</td>
                  <td>{fmt(row.browser_identity?.worker_node_id)}</td>
                  <td><StatusBadge value={row.account_health?.health_status || "healthy"} /></td>
                  <td>{fmt(row.account_health?.consecutive_failures)}</td>
                  <td className="truncate">{fmt(row.account_health?.last_error_code || row.account_health?.last_error_message)}</td>
                  <td>{fmt(row.account_health?.manual_review_required)}</td>
                  <td className="row-actions">
                    <button className="secondary-button" type="button" onClick={() => runAccountAction(() => validateBrowserIdentity(row.account_id), "هویت مرورگر اعتبارسنجی شد.")}>اعتبارسنجی</button>
                    <button className="secondary-button" type="button" onClick={() => editIdentityMetadata(row)}>ویرایش هویت</button>
                    <button className="secondary-button" type="button" onClick={() => runAccountAction(() => requireAccountReview(row.account_id), "اکانت نیازمند بازبینی شد.")}>بازبینی</button>
                    <button className="secondary-button" type="button" onClick={() => window.confirm("هشدار پاک شود؟") && runAccountAction(() => resetAccountWarning(row.account_id), "هشدار پاک شد.")}>پاک‌سازی هشدار</button>
                    <button className="secondary-button" type="button" onClick={() => runAccountAction(() => enableAccountHealth(row.account_id), "اکانت فعال شد.")}>فعال</button>
                    <button className="danger-button" type="button" onClick={() => window.confirm("اکانت غیرفعال شود؟") && runAccountAction(() => disableAccountHealth(row.account_id), "اکانت غیرفعال شد.")}>غیرفعال</button>
                  </td>
                  <td>
                    <div className="compact-stack">
                      <StatusBadge value={authSessions[row.account_id]?.auth?.auth_state || "unknown"} />
                      <span>{fmt(authSessions[row.account_id]?.maintenance_session_id)}</span>
                      <span>{fmt(authSessions[row.account_id]?.opened_at)}</span>
                      <span>{fmt(authSessions[row.account_id]?.last_checked_at)}</span>
                      <span>{fmt(authSessions[row.account_id]?.auth?.error_code)}</span>
                      <span>{authSessions[row.account_id]?.profile_lock?.owned ? "profile lock: owned" : "profile lock: none"}</span>
                    </div>
                  </td>
                  <td className="row-actions">
                    <button className="secondary-button" type="button" onClick={() => openManualBaleLogin(row)}>باز کردن بله برای ورود دستی</button>
                    <button className="secondary-button" type="button" onClick={() => checkManualBaleLogin(row)}>بررسی وضعیت ورود</button>
                    <button className="secondary-button" type="button" onClick={() => verifyManualBaleLogin(row)}>تأیید ورود</button>
                    <button className="danger-button" type="button" onClick={() => closeManualBaleLogin(row)}>بستن نشست ورود دستی</button>
                  </td>
                  <td>
                    <button className="icon-button" type="button" onClick={() => openEdit(row.account_id)} aria-label="ویرایش">
                      <Edit3 size={15} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState />
      )}

      {editing ? (
        <Modal title={`تنظیمات ${editing.account_id}`} onClose={() => setEditing(null)}>
          <div className="settings-grid">
            {editableFields.map(([key, label, type]) => (
              <label key={key} className={type === "checkbox" ? "checkbox-row" : ""}>
                {type === "checkbox" ? (
                  <>
                    <input type="checkbox" checked={!!form[key]} onChange={(event) => setForm({ ...form, [key]: event.target.checked })} />
                    {label}
                  </>
                ) : (
                  <>
                    {label}
                    <input value={form[key] ?? ""} type={type} onChange={(event) => setForm({ ...form, [key]: event.target.value })} />
                  </>
                )}
              </label>
            ))}
          </div>
          <div className="modal-actions">
            <button className="primary-button" type="button" disabled={saving} onClick={saveEdit}>ذخیره</button>
            <button className="secondary-button" type="button" onClick={() => setEditing(null)}>انصراف</button>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
