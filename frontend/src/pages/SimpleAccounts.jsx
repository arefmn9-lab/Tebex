import { Edit3, Eye, Info, MessageCircle, Plus, RefreshCw, SendHorizontal, Trash2, UsersRound } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  createPlatformAccount,
  deletePlatformAccount,
  getAccountRegistrySummary,
  listPlatformAccounts,
  updatePlatformAccount,
} from "../api/accountRegistry";
import { openBaleLogin } from "../api/platforms";
import {
  ContentCard,
  FormField,
  IconButton,
  InlineError,
  LoadingState,
  Modal,
  NumberInput,
  PageHeader,
  PrimaryButton,
  SecondaryButton,
  StatusBadge,
  TextInput,
} from "../components/ui/DesignSystem.jsx";

const supportedPlatforms = ["bale", "telegram", "whatsapp", "eitaa", "rubika", "soroush"];

const platformMeta = {
  bale: { label: "Bale", name: "بله", icon: MessageCircle, accent: "#1d9bf0" },
  telegram: { label: "Telegram", name: "تلگرام", icon: SendHorizontal, accent: "#229ed9" },
  whatsapp: { label: "WhatsApp", name: "واتساپ", icon: MessageCircle, accent: "#22a06b" },
  eitaa: { label: "Eitaa", name: "ایتا", icon: UsersRound, accent: "#d9902f" },
  rubika: { label: "Rubika", name: "روبیکا", icon: UsersRound, accent: "#7c3aed" },
  soroush: { label: "Soroush Plus", name: "سروش پلاس", icon: UsersRound, accent: "#16a0a6" },
};

function platformInfo(platformId) {
  return platformMeta[platformId] || { label: platformId, name: platformId, icon: UsersRound, accent: "#2947b6" };
}

function normalizeItems(payload) {
  return Array.isArray(payload) ? payload : payload?.items || payload?.value || [];
}

function accountId(account) {
  return account.account_id || account.id || "";
}

function accountPlatform(account) {
  return account.platform_id || account.platform || "bale";
}

function accountLabel(account) {
  return account.display_name || account.username_or_number || account.identifier || account.phone || "اکانت بدون نام";
}

function accountIdentifier(account) {
  return account.identifier || account.username_or_number || account.phone || "ثبت نشده";
}

function isActive(account) {
  if (typeof account.active === "boolean") return account.active;
  return String(account.status || "").toLowerCase() === "active";
}

function isAvailable(account) {
  if ("available" in account) return Boolean(account.available);
  return isActive(account) && !account.profile_in_use && !account.in_use && !account.lease_active && !account.locked;
}

function statusLabel(account) {
  if (isActive(account)) return "فعال";
  const status = account.status || "inactive";
  const labels = { disabled: "غیرفعال", inactive: "غیرفعال", new: "جدید", blocked: "مسدود", unknown: "نامشخص" };
  return labels[status] || status;
}

function summaryForPlatform(summary, platformId) {
  return (summary?.platforms || []).find((item) => item.platform_id === platformId) || {
    platform_id: platformId,
    total: 0,
    active: 0,
    inactive: 0,
  };
}

function emptyForm(platformId) {
  return {
    platform_id: platformId,
    display_name: "",
    identifier: "",
    active: true,
    daily_limit: "",
  };
}

export default function SimpleAccounts() {
  const [summary, setSummary] = useState(null);
  const [accounts, setAccounts] = useState([]);
  const [selectedPlatform, setSelectedPlatform] = useState("bale");
  const [loading, setLoading] = useState(true);
  const [platformLoading, setPlatformLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState(null);
  const [creating, setCreating] = useState(false);
  const [details, setDetails] = useState(null);
  const [form, setForm] = useState(emptyForm("bale"));

  const selectedMeta = platformInfo(selectedPlatform);
  const SelectedIcon = selectedMeta.icon;

  const platformCards = useMemo(() => {
    const summaryPlatforms = new Set((summary?.platforms || []).map((item) => item.platform_id));
    const ordered = [...supportedPlatforms, ...Array.from(summaryPlatforms).filter((id) => !supportedPlatforms.includes(id))];
    return ordered.map((platformId) => ({ ...summaryForPlatform(summary, platformId), platform_id: platformId }));
  }, [summary]);

  async function loadSummary(preferredPlatform = selectedPlatform) {
    const registrySummary = await getAccountRegistrySummary();
    setSummary(registrySummary);
    const firstWithAccounts = (registrySummary.platforms || []).find((item) => Number(item.total || 0) > 0);
    const nextPlatform = preferredPlatform || firstWithAccounts?.platform_id || supportedPlatforms[0];
    setSelectedPlatform(nextPlatform);
    return nextPlatform;
  }

  async function loadPlatform(platformId) {
    setPlatformLoading(true);
    try {
      const payload = await listPlatformAccounts(platformId);
      setAccounts(normalizeItems(payload));
    } finally {
      setPlatformLoading(false);
    }
  }

  async function load(preferredPlatform = selectedPlatform) {
    setLoading(true);
    setError("");
    try {
      const platformId = await loadSummary(preferredPlatform);
      await loadPlatform(platformId);
    } catch (err) {
      setError(err.message || "دریافت اطلاعات اکانت‌ها انجام نشد");
    } finally {
      setLoading(false);
    }
  }

  async function selectPlatform(platformId) {
    setSelectedPlatform(platformId);
    setError("");
    setNotice("");
    try {
      await loadPlatform(platformId);
    } catch (err) {
      setError(err.message || "دریافت اکانت‌های این پیام‌رسان انجام نشد");
    }
  }

  function openCreate() {
    setCreating(true);
    setEditing(null);
    setDetails(null);
    setForm(emptyForm(selectedPlatform));
  }

  function openEdit(account) {
    setEditing(account);
    setCreating(false);
    setDetails(null);
    setForm({
      platform_id: accountPlatform(account),
      account_id: accountId(account),
      display_name: accountLabel(account),
      identifier: accountIdentifier(account),
      active: isActive(account),
      daily_limit: account.daily_limit ?? "",
    });
  }

  async function saveAccount() {
    const platformId = form.platform_id || selectedPlatform;
    const payload = {
      display_name: form.display_name.trim() || form.identifier.trim(),
      username_or_number: form.identifier.trim(),
      identifier: form.identifier.trim(),
      phone: form.identifier.trim(),
      status: form.active ? "active" : "disabled",
      active: Boolean(form.active),
      daily_limit: form.daily_limit === "" ? null : Number(form.daily_limit),
    };
    if (!payload.username_or_number) {
      setError("شناسه یا شماره اکانت را وارد کنید.");
      return;
    }
    setError("");
    try {
      if (editing) {
        await updatePlatformAccount(platformId, form.account_id, payload);
        setNotice("اکانت ویرایش شد.");
      } else {
        await createPlatformAccount(platformId, payload);
        setNotice("اکانت اضافه شد.");
      }
      setEditing(null);
      setCreating(false);
      await load(platformId);
    } catch (err) {
      setError(err.message || "ذخیره اکانت انجام نشد");
    }
  }

  async function remove(account) {
    const id = accountId(account);
    const platformId = accountPlatform(account);
    if (!id || !window.confirm("حذف این اکانت انجام شود؟")) return;
    setError("");
    try {
      await deletePlatformAccount(platformId, id);
      setNotice("اکانت حذف شد.");
      if (details && accountId(details) === id) setDetails(null);
      await load(platformId);
    } catch (err) {
      setError(err.message || "حذف اکانت انجام نشد");
    }
  }

  async function openBrowser(account) {
    if (accountPlatform(account) !== "bale") {
      setDetails(account);
      return;
    }
    try {
      await openBaleLogin(accountId(account));
    } catch (err) {
      setError(err.message || "باز کردن مرورگر اکانت انجام نشد");
    }
  }

  useEffect(() => {
    load("");
  }, []);

  const selectedSummary = summaryForPlatform(summary, selectedPlatform);
  const totalSummaryCards = [
    ["کل اکانت‌ها", summary?.total_accounts ?? 0],
    ["اکانت‌های فعال", summary?.active_accounts ?? 0],
    ["اکانت‌های غیرفعال", summary?.inactive_accounts ?? 0],
    ["پیام‌رسان‌های دارای اکانت", summary?.platforms_with_accounts ?? 0],
  ];

  return (
    <section className="simple-accounts-page" dir="rtl">
      <PageHeader
        title="مرکز اکانت‌ها"
        description="مدیریت اکانت‌ها از مسیر canonical Account Registry؛ اطلاعات سلامت، مصرف و کمپین فقط وقتی نمایش داده می‌شود که API آن را صریح برگرداند."
        actions={<SecondaryButton onClick={() => load(selectedPlatform)} disabled={loading || platformLoading}><RefreshCw size={17} />تازه‌سازی</SecondaryButton>}
      />

      {error ? <InlineError>{error}</InlineError> : null}
      {notice ? <div className="toast">{notice}</div> : null}
      {loading ? <LoadingState label="در حال دریافت خلاصه رجیستری" /> : null}

      {!loading ? (
        <>
          <div className="account-hq-overview">
            {totalSummaryCards.map(([label, value]) => (
              <article key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </article>
            ))}
          </div>

          <div className="account-platform-summary-grid">
            {platformCards.map((platform) => {
              const meta = platformInfo(platform.platform_id);
              const Icon = meta.icon;
              return (
                <button
                  type="button"
                  className={`account-platform-summary-card${selectedPlatform === platform.platform_id ? " is-active" : ""}`}
                  key={platform.platform_id}
                  style={{ "--platform-accent": meta.accent }}
                  aria-pressed={selectedPlatform === platform.platform_id}
                  onClick={() => selectPlatform(platform.platform_id)}
                >
                  <div className="account-platform-head">
                    <span className="platform-logo"><Icon size={21} /></span>
                    <div>
                      <strong>{meta.label}</strong>
                      <small>{meta.name}</small>
                    </div>
                  </div>
                  <div className="account-platform-stats">
                    <p><span>کل</span><b>{platform.total || 0}</b></p>
                    <p><span>فعال</span><b>{platform.active || 0}</b></p>
                    <p><span>غیرفعال</span><b>{platform.inactive || 0}</b></p>
                  </div>
                  <span className="account-platform-select-label">مرکز اکانت‌های {meta.name}</span>
                </button>
              );
            })}
          </div>

          <ContentCard
            title={`مرکز اکانت‌های ${selectedMeta.name}`}
            description={`کل: ${selectedSummary.total || 0} · فعال: ${selectedSummary.active || 0} · غیرفعال: ${selectedSummary.inactive || 0}`}
          >
            <div className="account-hq-toolbar">
              <SecondaryButton onClick={() => selectPlatform(selectedPlatform)} disabled={platformLoading}>
                <RefreshCw size={16} />
                تازه‌سازی این پیام‌رسان
              </SecondaryButton>
              <PrimaryButton onClick={openCreate}>
                <Plus size={16} />
                افزودن اکانت {selectedMeta.name}
              </PrimaryButton>
            </div>

            {platformLoading ? <LoadingState label="در حال دریافت اکانت‌های پیام‌رسان" /> : null}
            {!platformLoading && !accounts.length ? (
              <div className="account-platform-empty" style={{ "--platform-accent": selectedMeta.accent }}>
                <span className="platform-logo"><SelectedIcon size={22} /></span>
                <div>
                  <strong>هنوز اکانتی برای {selectedMeta.name} اضافه نشده است.</strong>
                  <p>از دکمه افزودن اکانت برای ثبت اولین اکانت این پیام‌رسان در رجیستری استفاده کنید.</p>
                </div>
              </div>
            ) : null}

            <div className="account-card-list" id="accounts-list">
              {accounts.map((account) => {
                const platformId = accountPlatform(account);
                const meta = platformInfo(platformId);
                const Icon = meta.icon;
                const alerts = Array.isArray(account.alerts) ? account.alerts.slice(0, 3) : [];
                return (
                  <article className="account-card-row" key={accountId(account)} style={{ "--platform-accent": meta.accent }}>
                    <button type="button" className="account-card-main as-button" onClick={() => setDetails(account)}>
                      <span className="platform-logo"><Icon size={20} /></span>
                      <div>
                        <strong>{accountLabel(account)}</strong>
                        <small>{accountIdentifier(account)}</small>
                      </div>
                    </button>
                    <div className="account-badge-stack">
                      <StatusBadge tone={isActive(account) ? "success" : "warning"}>{statusLabel(account)}</StatusBadge>
                      {alerts.map((alert) => <StatusBadge key={alert.code || alert.category} tone="warning">{alert.label || alert.category}</StatusBadge>)}
                    </div>
                    <div className="account-card-limit">
                      <span>محدودیت روزانه</span>
                      <b>{account.daily_limit ?? "ثبت نشده"}</b>
                    </div>
                    <div className="account-card-actions">
                      <IconButton label="جزئیات" onClick={() => setDetails(account)}><Info size={16} /></IconButton>
                      {platformId === "bale" ? <IconButton label="مشاهده مرورگر" onClick={() => openBrowser(account)}><Eye size={16} /></IconButton> : null}
                      <IconButton label="ویرایش" onClick={() => openEdit(account)}><Edit3 size={16} /></IconButton>
                      <IconButton label="حذف" onClick={() => remove(account)}><Trash2 size={16} /></IconButton>
                    </div>
                  </article>
                );
              })}
            </div>
          </ContentCard>
        </>
      ) : null}

      {(creating || editing) ? (
        <Modal title={editing ? "ویرایش اکانت" : `افزودن اکانت ${selectedMeta.name}`} onClose={() => { setCreating(false); setEditing(null); }}>
          <div className="settings-modal-grid">
            <FormField label="نام نمایشی">
              <TextInput value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} />
            </FormField>
            <FormField label="شماره یا شناسه">
              <TextInput value={form.identifier} onChange={(event) => setForm({ ...form, identifier: event.target.value })} />
            </FormField>
            <FormField label="وضعیت">
              <label className="account-active-toggle">
                <input checked={Boolean(form.active)} type="checkbox" onChange={(event) => setForm({ ...form, active: event.target.checked })} />
                <span>{form.active ? "فعال" : "غیرفعال"}</span>
              </label>
            </FormField>
            <FormField label="محدودیت روزانه">
              <NumberInput min="0" value={form.daily_limit} onChange={(event) => setForm({ ...form, daily_limit: event.target.value })} />
            </FormField>
          </div>
          <div className="modal-actions">
            <PrimaryButton onClick={saveAccount}>ذخیره</PrimaryButton>
            <SecondaryButton onClick={() => { setCreating(false); setEditing(null); }}>انصراف</SecondaryButton>
          </div>
        </Modal>
      ) : null}

      {details ? (
        <Modal title="جزئیات اکانت" onClose={() => setDetails(null)}>
          <div className="account-details-panel">
            <p><span>نام ذخیره‌شده</span><b>{accountLabel(details)}</b></p>
            <p><span>پیام‌رسان</span><b>{platformInfo(accountPlatform(details)).name}</b></p>
            <p><span>شماره یا شناسه</span><b>{accountIdentifier(details)}</b></p>
            <p><span>وضعیت</span><b>{statusLabel(details)}</b></p>
            {details.daily_limit !== undefined && details.daily_limit !== null ? <p><span>محدودیت روزانه</span><b>{details.daily_limit}</b></p> : null}
            {Array.isArray(details.alerts) && details.alerts.length ? (
              <p><span>هشدارها</span><b>{details.alerts.slice(0, 3).map((alert) => alert.label || alert.category).join("، ")}</b></p>
            ) : null}
          </div>
          <div className="modal-actions">
            {accountPlatform(details) === "bale" ? <SecondaryButton onClick={() => openBrowser(details)}><Eye size={16} />مشاهده مرورگر</SecondaryButton> : null}
            <PrimaryButton onClick={() => openEdit(details)}>ویرایش</PrimaryButton>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
