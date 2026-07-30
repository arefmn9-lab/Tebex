import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleOff,
  LockKeyhole,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Stethoscope,
  X,
} from "lucide-react";
import {
  auditBaleAuthentication,
  closeBaleAuthentication,
  disableBaleAccount,
  getBaleAuthenticationStatus,
  listBaleOnboardingAccounts,
  openBaleAuthentication,
  preflightBaleAccount,
  provisionBaleAccount,
  reconcileBaleAccount,
  verifyBaleAuthentication,
} from "../api/baleOnboarding";

const pageSize = 10;
const draftStorageKey = "clinicos:bale-account-create-draft";
const sessionStorageKey = "clinicos:bale-controlled-login-sessions";

const statusLabels = {
  discovered_existing: "اکانت موجود",
  provisioned: "ایجاد شده",
  login_required: "نیازمند ورود",
  login_in_progress: "مرورگر ورود باز است",
  authenticated: "احراز شده",
  persistence_check_required: "نیازمند تست نشست",
  ready: "آماده",
  verification_expired: "تأیید منقضی",
  blocked: "مسدود",
  disabled: "غیرفعال",
  retired: "بازنشسته",
  provisioning_failed: "ایجاد ناموفق",
  authentication_failed: "احراز ناموفق",
};

function safeError(error, fallback) {
  return error?.data?.detail?.message || error?.data?.detail || error?.message || fallback;
}

function readStoredJson(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) || fallback;
  } catch {
    return fallback;
  }
}

function Technical({ value }) {
  const [open, setOpen] = useState(false);
  if (!value) return null;
  return (
    <button className="technical-toggle" type="button" onClick={() => setOpen((value) => !value)}>
      {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />} جزئیات فنی
      {open ? <code>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</code> : null}
    </button>
  );
}

export default function BaleAccounts() {
  const [payload, setPayload] = useState({ items: [], summary: {}, configuration: {} });
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [expanded, setExpanded] = useState("");
  const [createModal, setCreateModal] = useState(() => readStoredJson(draftStorageKey, null));
  const [diagnostics, setDiagnostics] = useState(null);
  const [sessions, setSessions] = useState(() => readStoredJson(sessionStorageKey, {}));
  const pollers = useRef(new Map());

  async function refresh() {
    setError("");
    setPayload(await listBaleOnboardingAccounts());
  }

  useEffect(() => {
    refresh().catch((err) => setError(safeError(err, "دریافت فهرست اکانت‌ها انجام نشد."))).finally(() => setLoading(false));
    return () => {
      for (const timer of pollers.current.values()) window.clearInterval(timer);
      pollers.current.clear();
    };
  }, []);

  useEffect(() => {
    if (createModal) localStorage.setItem(draftStorageKey, JSON.stringify(createModal));
    else localStorage.removeItem(draftStorageKey);
  }, [createModal]);

  useEffect(() => {
    localStorage.setItem(sessionStorageKey, JSON.stringify(sessions));
  }, [sessions]);

  const filtered = useMemo(() => payload.items.filter((account) => {
    const text = `${account.masked_identifier} ${account.account_id} ${account.lifecycle_status} ${account.authentication_status}`.toLowerCase();
    return text.includes(search.trim().toLowerCase()) && (filter === "all" || account.lifecycle_status === filter);
  }), [payload.items, search, filter]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visible = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const config = payload.configuration || {};
  const summary = payload.summary || {};
  const disabledCount = payload.items.filter((account) => account.lifecycle_status === "disabled").length;

  async function act(key, action, success = "") {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      const result = await action();
      if (success) setNotice(success);
      await refresh();
      return result;
    } catch (err) {
      setError(safeError(err, "عملیات انجام نشد."));
      return null;
    } finally {
      setBusy("");
    }
  }

  function beginCreate() {
    setCreateModal({ phone: "", stage: "details", preflight: null, account: null, operation: null });
  }

  async function validateAndProvision() {
    const phone = createModal.phone.trim();
    const preflight = await act("create:preflight", () => preflightBaleAccount({ identifier: phone, account_id: null }));
    if (!preflight) return;
    setCreateModal((current) => ({ ...current, preflight, stage: "preflight" }));
    if (!preflight.provisioning_allowed) return;
    const idempotencyKey = createModal.idempotency_key || crypto.randomUUID();
    setCreateModal((current) => ({ ...current, idempotency_key: idempotencyKey }));
    const result = await act("create:provision", () => provisionBaleAccount({
      identifier: phone,
      account_id: preflight.account_id,
      idempotency_key: idempotencyKey,
      created_by: "operator-ui",
    }), "اکانت و پروفایل دائمی ایجاد شدند.");
    if (result) {
      setCreateModal((current) => ({
        ...current,
        stage: "created",
        account: result.account,
        operation: result.operation,
      }));
    }
  }

  function startPolling(accountId, maintenanceSessionId) {
    const previous = pollers.current.get(accountId);
    if (previous) window.clearInterval(previous);
    const timer = window.setInterval(async () => {
      try {
        const status = await getBaleAuthenticationStatus(maintenanceSessionId);
        setSessions((current) => ({ ...current, [accountId]: { ...current[accountId], status } }));
        if (status.closed) {
          window.clearInterval(timer);
          pollers.current.delete(accountId);
        }
      } catch {
        window.clearInterval(timer);
        pollers.current.delete(accountId);
      }
    }, 3000);
    pollers.current.set(accountId, timer);
  }

  async function openLogin(account, purpose = "login") {
    const key = `open:${account.account_id}:${purpose}`;
    const result = await act(key, async () => {
      await auditBaleAuthentication(account.account_id);
      return openBaleAuthentication(account.account_id, purpose);
    }, purpose === "persistence" ? "همان پروفایل برای تست نشست باز شد." : "Chrome با پروفایل دائمی این اکانت باز شد.");
    if (!result) return;
    setSessions((current) => ({
      ...current,
      [account.account_id]: {
        maintenance_session_id: result.maintenance_session_id,
        purpose,
        status: result,
      },
    }));
    startPolling(account.account_id, result.maintenance_session_id);
    return result;
  }

  async function confirmLogin(account) {
    const session = sessions[account.account_id];
    if (!session?.maintenance_session_id) {
      setError("ابتدا «ورود دستی» را برای این اکانت باز کنید.");
      return;
    }
    const result = await act(`verify:${account.account_id}`, () => verifyBaleAuthentication(session.maintenance_session_id), "نتیجه احراز هویت از بک‌اند دریافت شد.");
    if (result) setSessions((current) => ({ ...current, [account.account_id]: { ...session, status: result } }));
  }

  async function closeBrowser(account) {
    const session = sessions[account.account_id];
    if (!session?.maintenance_session_id) {
      setError("نشست مرورگر فعالی برای این اکانت ثبت نشده است.");
      return;
    }
    const result = await act(`close:${account.account_id}`, () => closeBaleAuthentication(session.maintenance_session_id), "مرورگر به‌صورت امن بسته شد.");
    if (result) {
      const timer = pollers.current.get(account.account_id);
      if (timer) window.clearInterval(timer);
      pollers.current.delete(account.account_id);
      setSessions((current) => {
        const next = { ...current };
        delete next[account.account_id];
        return next;
      });
    }
  }

  async function reconcileAll() {
    const results = [];
    for (const account of payload.items) {
      const result = await reconcileBaleAccount(account.account_id);
      results.push(result);
    }
    setDiagnostics({ mode: "all", results });
    setNotice("تطبیق فقط‌خواندنی همه اکانت‌ها انجام شد.");
  }

  async function showDiagnostics(account) {
    const result = await act(`diagnostics:${account.account_id}`, () => reconcileBaleAccount(account.account_id));
    if (result) setDiagnostics({ mode: "single", account, results: [result] });
  }

  async function disableAccount(account) {
    if (!window.confirm(`اکانت ${account.account_id} غیرفعال شود؟ پروفایل و نشست ذخیره‌شده حذف نمی‌شوند.`)) return;
    await act(`disable:${account.account_id}`, () => disableBaleAccount(account.account_id), "اکانت بدون حذف پروفایل غیرفعال شد.");
  }

  return (
    <main className="bale-accounts-page" dir="rtl">
      <header className="bale-accounts-header">
        <div>
          <p className="eyebrow">مدیریت اکانت‌های بله</p>
          <h1>اکانت‌ها و پروفایل‌های دائمی</h1>
          <p>هر اکانت مستقل است؛ ساخت، ورود و تست نشست به بچ یا تعداد اکانت‌ها وابسته نیست.</p>
        </div>
        <div className="header-actions">
          <button className="primary-button" onClick={beginCreate} disabled={Boolean(busy)}><Plus size={17} /> افزودن اکانت جدید</button>
          <button className="secondary-button" onClick={() => act("refresh", refresh)} disabled={Boolean(busy)}><RefreshCw size={16} /> بروزرسانی</button>
          <button className="secondary-button" onClick={() => act("reconcile:all", reconcileAll)} disabled={Boolean(busy) || !payload.items.length}><Stethoscope size={16} /> تطبیق همه</button>
        </div>
      </header>

      <section className="operational-banner" aria-label="وضعیت عملیاتی">
        <strong>ONBOARDING MODE</strong>
        <span>LIVE SENDING {config.live_sending_enabled ? "ENABLED" : "DISABLED"}</span>
        <span>QUEUE {config.queue_enabled ? "ENABLED" : "DISABLED"}</span>
        <span>LOGIN CONCURRENCY: {config.max_login_concurrency ?? "—"}</span>
      </section>

      {error ? <div className="inline-error" role="alert">{typeof error === "string" ? error : JSON.stringify(error)}</div> : null}
      {notice ? <div className="onboarding-notice"><CheckCircle2 size={17} /> {notice}</div> : null}

      <section className="account-summary-grid" aria-label="آمار اکانت‌ها">
        {[
          ["کل اکانت‌ها", summary.total_accounts],
          ["احراز شده", summary.authenticated_accounts],
          ["نیازمند ورود", summary.login_required_accounts],
          ["آماده", summary.ready_accounts],
          ["غیرفعال", disabledCount],
        ].map(([label, value]) => <article key={label}><span>{label}</span><strong>{loading ? "…" : value ?? 0}</strong></article>)}
      </section>

      <section className="account-list-panel">
        <div className="account-list-toolbar">
          <label><Search size={16} /><input aria-label="جستجوی اکانت" placeholder="شماره ماسک‌شده، account_id یا وضعیت" value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }} /></label>
          <select aria-label="فیلتر وضعیت" value={filter} onChange={(event) => { setFilter(event.target.value); setPage(1); }}>
            <option value="all">همه وضعیت‌ها</option>
            {Object.entries(statusLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </div>

        {loading ? <div className="loading-state">در حال دریافت اکانت‌ها…</div> : null}
        {!loading && !visible.length ? <div className="empty-state">اکانتی مطابق این فیلتر وجود ندارد.</div> : null}

        <div className="dynamic-account-list">
          {visible.map((account) => {
            const session = sessions[account.account_id];
            const isOpen = expanded === account.account_id;
            const hasActiveSession = Boolean(session?.maintenance_session_id && !session?.status?.closed);
            const persistenceNeeded = account.lifecycle_status === "persistence_check_required";
            return (
              <article className="dynamic-account-row account-centric-row" key={account.account_id}>
                <button className="account-row-main" onClick={() => setExpanded(isOpen ? "" : account.account_id)} aria-expanded={isOpen}>
                  <span className={`health-dot health-${account.health_status}`} />
                  <span><b>{account.masked_identifier || "شماره ثبت نشده"}</b><small>{account.account_id}</small></span>
                  <span><b>{account.profile_present ? "پروفایل دائمی موجود" : "پروفایل ناموجود"}</b><small>{statusLabels[account.lifecycle_status] || account.lifecycle_status}</small></span>
                  <span><b>{account.authentication_status}</b><small>نشست: {account.session_persistence_status}</small></span>
                  {isOpen ? <ChevronUp /> : <ChevronDown />}
                </button>

                {isOpen ? (
                  <div className="account-row-details">
                    <dl className="account-facts">
                      <div><dt>آخرین تأیید</dt><dd>{account.authentication_verified_at || "ثبت نشده"}</dd></div>
                      <div><dt>قفل مرورگر</dt><dd>{hasActiveSession ? "فعال" : "آزاد"}</dd></div>
                      <div><dt>پروفایل</dt><dd>{account.profile_present ? "موجود و اختصاصی" : "ناموجود"}</dd></div>
                      <div><dt>ماندگاری</dt><dd>{account.session_persistence_status}</dd></div>
                    </dl>
                    {account.eligibility_reasons?.length ? <p className="blocking-reasons"><AlertTriangle size={15} /> {account.eligibility_reasons.join("، ")}</p> : null}
                    <div className="row-actions account-actions">
                      <button onClick={() => openLogin(account, "login")} disabled={Boolean(busy) || hasActiveSession || account.retired}><LockKeyhole size={15} /> ورود دستی</button>
                      <button onClick={() => confirmLogin(account)} disabled={Boolean(busy) || !hasActiveSession}><ShieldCheck size={15} /> {session?.purpose === "persistence" ? "تأیید ماندگاری نشست" : "تأیید ورود"}</button>
                      <button onClick={() => openLogin(account, "persistence")} disabled={Boolean(busy) || hasActiveSession || (!persistenceNeeded && account.session_persistence_status === "verified")}><RefreshCw size={15} /> تست نشست ذخیره‌شده</button>
                      <button onClick={() => closeBrowser(account)} disabled={Boolean(busy) || !hasActiveSession}><X size={15} /> بستن امن مرورگر</button>
                      <button onClick={() => showDiagnostics(account)} disabled={Boolean(busy)}><Stethoscope size={15} /> عیب‌یابی</button>
                      <button className="danger-link" onClick={() => disableAccount(account)} disabled={Boolean(busy) || hasActiveSession || account.lifecycle_status === "disabled" || account.retired}><CircleOff size={15} /> غیرفعال‌کردن اکانت</button>
                    </div>
                    <p className="otp-safety-note">شماره و OTP فقط داخل Bale Web وارد می‌شوند؛ ClinicOS هیچ فیلد OTP ندارد.</p>
                    <Technical value={account.last_error_code} />
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>

        <div className="pagination">
          <button disabled={currentPage <= 1} onClick={() => setPage((value) => value - 1)}>قبلی</button>
          <span>صفحه {currentPage} از {pageCount} — {filtered.length} اکانت</span>
          <button disabled={currentPage >= pageCount} onClick={() => setPage((value) => value + 1)}>بعدی</button>
        </div>
      </section>

      {createModal ? (
        <div className="onboarding-modal-backdrop" role="presentation">
          <section className="onboarding-modal account-create-modal" role="dialog" aria-modal="true" aria-label="افزودن اکانت بله">
            <button className="modal-close" onClick={() => setCreateModal(null)} aria-label="بستن"><X /></button>
            <h2>افزودن اکانت جدید</h2>

            {createModal.stage === "details" ? (
              <div className="wizard-body">
                <p>اکانت مستقل ایجاد می‌شود و به هیچ بچی نیاز ندارد.</p>
                <label>شماره موبایل<input autoFocus inputMode="tel" value={createModal.phone} onChange={(event) => setCreateModal((current) => ({ ...current, phone: event.target.value }))} placeholder="09xxxxxxxxx" /></label>
                <button className="primary-button" onClick={validateAndProvision} disabled={!createModal.phone.trim() || Boolean(busy)}>ایجاد اکانت و پروفایل دائمی</button>
              </div>
            ) : null}

            {createModal.stage === "preflight" ? (
              <div className="wizard-body">
                <h3>پیش‌بررسی نیازمند اصلاح است</h3>
                <div className="preflight-checks">
                  {createModal.preflight.checks.map((check) => <article className={`preflight-${check.status}`} key={check.check_name}><b>{check.status === "pass" ? "✓" : "×"} {check.message_fa}</b><Technical value={check.error_code} /></article>)}
                </div>
                <button onClick={() => setCreateModal((current) => ({ ...current, stage: "details" }))}>اصلاح شماره</button>
              </div>
            ) : null}

            {createModal.stage === "created" ? (
              <div className="wizard-body created-account-success">
                <CheckCircle2 size={44} />
                <h3>اکانت و پروفایل دائمی ایجاد شدند</h3>
                <dl>
                  <div><dt>اکانت</dt><dd>{createModal.account.account_id}</dd></div>
                  <div><dt>شماره</dt><dd>{createModal.account.masked_identifier}</dd></div>
                  <div><dt>پروفایل</dt><dd>{createModal.account.canonical_profile_path}</dd></div>
                </dl>
                <p>OTP را فقط داخل Bale Web وارد کنید.</p>
                <div className="wizard-actions">
                  <button onClick={() => setCreateModal(null)}>فعلاً بستن</button>
                  <button className="primary-button" onClick={async () => { const opened = await openLogin(createModal.account, "login"); if (opened) setCreateModal(null); }} disabled={Boolean(busy)}><LockKeyhole size={16} /> بازکردن Chrome برای ورود</button>
                </div>
              </div>
            ) : null}
          </section>
        </div>
      ) : null}

      {diagnostics ? (
        <div className="onboarding-modal-backdrop" role="presentation">
          <section className="onboarding-modal" role="dialog" aria-modal="true" aria-label="عیب‌یابی اکانت">
            <button className="modal-close" onClick={() => setDiagnostics(null)} aria-label="بستن"><X /></button>
            <h2>{diagnostics.mode === "all" ? "تطبیق همه اکانت‌ها" : "عیب‌یابی اکانت"}</h2>
            {diagnostics.results.map((result) => (
              <article className="diagnostic-result" key={result.account_id}>
                <h3>{result.masked_identifier} <small>{result.account_id}</small></h3>
                <div className="check-grid">
                  {Object.entries(result).filter(([, value]) => typeof value === "boolean").map(([key, value]) => <span className={value ? "check-pass" : "check-warn"} key={key}>{value ? "✓" : "!"} {key}</span>)}
                </div>
                {result.conflicts?.length ? <div className="inline-error">{result.conflicts.join("، ")}</div> : <p>تعارضی گزارش نشد؛ هیچ تغییری روی پروفایل انجام نشد.</p>}
                <Technical value={result.recommended_action} />
              </article>
            ))}
          </section>
        </div>
      ) : null}
    </main>
  );
}
