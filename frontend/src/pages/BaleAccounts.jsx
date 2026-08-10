import { useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  LockKeyhole,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import {
  deleteBaleAccount,
  getBaleAccountOperation,
  listBaleOnboardingAccounts,
  openBaleAuthentication,
  recheckBaleAuthentication,
  preflightBaleAccount,
  provisionBaleAccount,
  resetBaleAccountProfile,
} from "../api/baleOnboarding";
import { recordDiagnosticEvent } from "../diagnostics";
import { BALE_AUTH_LABELS, formatBaleAccountActionError, mapBaleAuthState } from "../baleAuthPresentation";

const pageSize = 10;
const draftStorageKey = "clinicos:bale-account-create-draft";
const sessionStorageKey = "clinicos:bale-controlled-login-sessions";
const TERMINAL_ACCOUNT_OPERATION_STATUSES = new Set(["completed", "succeeded", "failed", "cancelled", "interrupted", "timed_out"]);

// ============================================================
// BLOCK: BALE_ACCOUNT_CARD_PRESENTATION
// PURPOSE:
// Maps backend account data to the four simple MVP states shown to operators.
// ACCOUNT_SCOPE:
// One Bale account card only.
// DEPENDENCIES:
// Canonical onboarding account payload and local authentication session state.
// LAYER:
// UI
// ============================================================

function accountPhone(account) {
  return account.phone || account.username_or_number || account.normalized_identifier || account.account_id.replace(/^bale_/, "") || "شماره ثبت نشده";
}

function backendAuthState(session) {
  return session?.status?.auth?.auth_state || session?.auth?.auth_state || "";
}

function isTerminalAuthenticationError(account, sessionAuth) {
  const errorCode = String(sessionAuth.error_code || "");
  if (["authentication_failed", "blocked"].includes(account.lifecycle_status)) return true;
  if (["blocked", "manual_review"].includes(account.health_status)) return true;
  if (errorCode === "account_restricted") return true;
  if (errorCode === "maintenance_session_not_found") return false;
  return /(?:fatal|browser_(?:start|launch)|session_(?:closed|invalidated|page_closed)|profile_identity)/.test(errorCode);
}

function userState(account, session, busyKey = "") {
  const sessionAuth = session?.status?.auth || session?.auth || {};
  if (isTerminalAuthenticationError(account, sessionAuth)) return BALE_AUTH_LABELS.probe_failure;
  return mapBaleAuthState(account, session, busyKey);
}

// ============================================================
// END BLOCK: BALE_ACCOUNT_CARD_PRESENTATION
// ============================================================

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

export default function BaleAccounts() {
  const [payload, setPayload] = useState({ items: [], summary: {}, configuration: {} });
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [pendingActions, setPendingActions] = useState({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [expanded, setExpanded] = useState("");
  const [createModal, setCreateModal] = useState(() => readStoredJson(draftStorageKey, null));
  const [sessions, setSessions] = useState(() => readStoredJson(sessionStorageKey, {}));
  const pollers = useRef(new Map());
  const previousMappedStates = useRef(new Map());

  function setActionPending(accountId, action, pending) {
    setPendingActions((current) => ({
      ...current,
      [accountId]: { ...(current[accountId] || {}), [action]: pending },
    }));
  }

  function actionAvailability(account, action) {
    const pending = pendingActions[account.account_id] || {};
    const authenticationPending = Boolean(pending.open_login || pending.session_recheck);
    const destructivePending = Boolean(pending.delete_account || pending.reset_profile);
    const anyPending = authenticationPending || destructivePending || Boolean(account.operation_busy);
    const concreteRuntimeOwner = Boolean(account.active_job || account.account_lock || account.profile_lock);
    if (action === "delete_account") {
      return { disabled: anyPending, reason: anyPending ? "عملیات قبلی این اکانت هنوز تمام نشده است." : null };
    }
    if (action === "reset_profile") {
      return { disabled: anyPending || concreteRuntimeOwner, reason: anyPending ? "عملیات قبلی هنوز تمام نشده است." : concreteRuntimeOwner ? "اکانت اکنون مشغول است؛ پس از پایان کار دوباره تلاش کنید." : null };
    }
    return { disabled: anyPending || concreteRuntimeOwner || Boolean(account.soft_deleted || account.retired), reason: anyPending ? "عملیات قبلی این اکانت هنوز تمام نشده است." : concreteRuntimeOwner ? "اکانت اکنون مشغول است؛ پس از پایان کار دوباره تلاش کنید." : (account.soft_deleted || account.retired) ? "این اکانت دیگر فعال نیست." : null };
  }

  function recordActionClick(account, action, availability) {
    recordDiagnosticEvent({
      action: "[BALE_UI_ACCOUNT_ACTION_CLICK]",
      clicked_action: action,
      module: "bale_accounts_ui",
      account_id: account.account_id,
      handler_reached: true,
      button_disabled: availability.disabled,
      disabled_reason: availability.reason,
      pending_state: pendingActions[account.account_id] || {},
      timestamp: new Date().toISOString(),
      success: !availability.disabled,
    });
  }

  async function refresh() {
    setError("");
    setPayload(await listBaleOnboardingAccounts());
  }

  useEffect(() => {
    refresh().catch((err) => setError(safeError(err, "دریافت فهرست اکانت‌ها انجام نشد."))).finally(() => setLoading(false));
    // Stored session metadata is display-only on mount. Only an explicit
    // per-account action below may start polling or browser maintenance.
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

  useEffect(() => {
    for (const account of payload.items) {
      const session = sessions[account.account_id];
      const mappedState = userState(account, session, busy);
      const previousState = previousMappedStates.current.get(account.account_id) || "initial";
      if (previousState === mappedState) continue;
      const backendState = backendAuthState(session) || account.authentication_status || "unknown";
      const diagnosticState = {
        account_id: account.account_id,
        previous_state: previousState,
        mapped_state: mappedState,
        backend_auth_state: backendState,
      };
      console.info("[BALE_UI_AUTH_STATE]", diagnosticState);
      recordDiagnosticEvent({
        action: "[BALE_UI_AUTH_STATE]",
        module: "bale_accounts_ui",
        account_id: account.account_id,
        success: mappedState !== "خطا",
        status: mappedState,
        error_message: JSON.stringify(diagnosticState),
        related_files: ["frontend/src/pages/BaleAccounts.jsx"],
      });
      previousMappedStates.current.set(account.account_id, mappedState);
    }
  }, [payload.items, sessions, busy]);

  const filtered = useMemo(() => payload.items.filter((account) => {
    const session = sessions[account.account_id];
    const text = `${accountPhone(account)} ${account.account_id} ${userState(account, session, busy)}`.toLowerCase();
    return text.includes(search.trim().toLowerCase()) && (filter === "all" || userState(account, session, busy) === filter);
  }), [payload.items, search, filter, sessions, busy]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visible = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const operatorSummary = useMemo(() => {
    const ready = payload.items.filter((account) => userState(account, sessions[account.account_id], busy) === BALE_AUTH_LABELS.ready).length;
    return { total: payload.items.length, ready, needsAttention: Math.max(0, payload.items.length - ready) };
  }, [payload.items, sessions, busy]);

  async function act(key, action, success = "", formatError = safeError) {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      const result = await action();
      if (success) setNotice(success);
      await refresh();
      return result;
    } catch (err) {
      setError(formatError(err, "عملیات اکانت کامل نشد. دوباره تلاش کنید."));
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
    const preflight = await act("create:preflight", () => preflightBaleAccount({ identifier: phone, account_id: null }), "", formatBaleAccountActionError);
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
    }), "اکانت و پروفایل دائمی ایجاد شدند.", formatBaleAccountActionError);
    if (result) {
      setCreateModal((current) => ({
        ...current,
        stage: "created",
        account: result.account,
        operation: result.operation,
      }));
    }
  }

  // FUNCTION:
  // startPolling
  // RESPONSIBILITY:
  // Tracks one account authentication session and persists verified state.
  // INPUT:
  // accountId, maintenanceSessionId
  // OUTPUT:
  // None
  // SIDE EFFECTS:
  // Polls status, verifies authenticated sessions once, and removes stale sessions.
  function startPolling(accountId, operationId, action) {
    const previous = pollers.current.get(accountId);
    if (previous) window.clearInterval(previous);
    const timer = window.setInterval(async () => {
      try {
        const status = await getBaleAccountOperation(operationId);
        recordDiagnosticEvent({ action: "account_operation_poll", module: "bale_accounts_ui", account_id: accountId, operation_id: operationId, endpoint: `/automation/platforms/bale/account-operations/${operationId}`, http_method: "GET", stage: status.status, success: status.status !== "failed", result: { status: status.status, error_code: status.error_code } });
        setSessions((current) => ({ ...current, [accountId]: { ...current[accountId], status } }));
        if (TERMINAL_ACCOUNT_OPERATION_STATUSES.has(status.status)) {
          let refreshError = null;
          try {
            await refresh();
          } catch (rowRefreshError) {
            refreshError = rowRefreshError;
          }
          const succeeded = ["completed", "succeeded"].includes(status.status);
          recordDiagnosticEvent({ action: "account_operation_terminal_and_row_refreshed", clicked_action: action, module: "bale_accounts_ui", account_id: accountId, operation_id: operationId, endpoint: `/automation/platforms/bale/account-operations/${operationId}`, http_method: "GET", stage: status.status, success: succeeded && !refreshError, error_code: status.error_code || refreshError?.errorCode, error_message: status.error_message || (refreshError ? safeError(refreshError, "Account row refresh failed.") : null), result: status.result?.final_persisted_account_state || status.result?.after || null });
          window.clearInterval(timer);
          pollers.current.delete(accountId);
          setBusy("");
          setActionPending(accountId, action, false);
          if (["failed", "cancelled", "interrupted", "timed_out"].includes(status.status)) {
            setNotice("");
            setError(formatBaleAccountActionError(status, "بازبینی اکانت کامل نشد. دوباره تلاش کنید."));
          } else if (refreshError) setError(formatBaleAccountActionError(refreshError, "فهرست اکانت‌ها تازه‌سازی نشد. دوباره تلاش کنید."));
          else if (action === "session_recheck") setNotice("بازبینی نشست با موفقیت انجام شد.");
          else if (action === "open_login") setNotice("وضعیت ورود اکانت با موفقیت تأیید شد.");
          return;
        }
      } catch (pollingError) {
        window.clearInterval(timer);
        pollers.current.delete(accountId);
        setBusy("");
        if (action) setActionPending(accountId, action, false);
        setNotice("");
        setError(formatBaleAccountActionError(pollingError, "بازبینی اکانت کامل نشد. دوباره تلاش کنید."));
      }
    }, 3000);
    pollers.current.set(accountId, timer);
  }

  // FUNCTION:
  // openLogin
  // RESPONSIBILITY:
  // Starts the existing authentication API flow from one account card.
  // INPUT:
  // Canonical account payload and authentication purpose
  // OUTPUT:
  // Authentication maintenance session payload or null
  // SIDE EFFECTS:
  // Records UI diagnostics and starts status polling for this account.
  async function openLogin(account, purpose = "login") {
    const action = purpose === "session_recheck" ? "session_recheck" : "open_login";
    const key = `${account.account_id}:${action}`;
    const startedAt = performance.now();
    const availability = actionAvailability(account, action);
    recordActionClick(account, action, availability);
    if (availability.disabled) return null;
    setActionPending(account.account_id, action, true);
    const endpoint = purpose === "session_recheck" ? "/automation/platforms/bale/authentication/session-recheck" : "/automation/platforms/bale/authentication/open";
    recordDiagnosticEvent({ action: `${action}_request_started`, clicked_action: action, module: "bale_accounts_ui", account_id: account.account_id, endpoint, http_method: "POST", handler_reached: true, success: true, stage: "requesting_operation" });
    recordDiagnosticEvent({
      action: `${action}_started`,
      module: "bale_accounts_ui",
      account_id: account.account_id,
      success: true,
      status: "started",
      endpoint,
      related_files: ["frontend/src/pages/BaleAccounts.jsx"],
    });
    const result = await act(key, async () => {
      return purpose === "session_recheck"
        ? recheckBaleAuthentication(account.account_id)
        : openBaleAuthentication(account.account_id, purpose);
    }, action === "open_login" ? "پنجره ورود این اکانت باز شد." : "", formatBaleAccountActionError);
    recordDiagnosticEvent({
      action: `${action}_accepted`,
      module: "bale_accounts_ui",
      account_id: account.account_id,
      success: Boolean(result),
      status: result ? "succeeded" : "failed",
      endpoint,
      duration_ms: performance.now() - startedAt,
      related_files: ["frontend/src/pages/BaleAccounts.jsx"],
    });
    if (!result) {
      setActionPending(account.account_id, action, false);
      return null;
    }
    recordDiagnosticEvent({ action: "account_operation_accepted", clicked_action: action, module: "bale_accounts_ui", account_id: account.account_id, endpoint, http_method: "POST", operation_id: result.operation_id, stage: "accepted_202", success: true });
    setSessions((current) => ({
      ...current,
      [account.account_id]: {
        operation_id: result.operation_id,
        purpose,
        status: result,
      },
    }));
    startPolling(account.account_id, result.operation_id, action);
    return result;
  }

  async function runAccountAction(account, action) {
    const availability = actionAvailability(account, action);
    recordActionClick(account, action, availability);
    if (availability.disabled) return;
    recordDiagnosticEvent({ action: "confirmation_opened", clicked_action: action, module: "bale_accounts_ui", account_id: account.account_id, handler_reached: true, success: true });
    const confirmed = action === "delete_account"
      ? window.confirm(`حذف اکانت ${accountPhone(account)} (${account.account_id})، ورود ذخیره‌شده و پروفایل مرورگر دائمی است. ادامه می‌دهید؟`)
      : window.confirm(`پروفایل ذخیره‌شده ${account.account_id} بازنشانی شود؟`);
    recordDiagnosticEvent({ action: confirmed ? "confirmation_accepted" : "confirmation_cancelled", clicked_action: action, module: "bale_accounts_ui", account_id: account.account_id, success: confirmed });
    if (!confirmed) return;
    setActionPending(account.account_id, action, true);
    const endpoint = action === "delete_account" ? `/automation/platforms/bale/accounts/${account.account_id}?delete_profile=true` : `/automation/platforms/bale/onboarding/accounts/${account.account_id}/reset-profile`;
    recordDiagnosticEvent({ action: `${action}_request_started`, clicked_action: action, module: "bale_accounts_ui", account_id: account.account_id, endpoint, http_method: action === "delete_account" ? "DELETE" : "POST", stage: "requesting_operation", success: true });
    const operation = await act(`${account.account_id}:${action}`, () => (
      action === "delete_account"
        ? deleteBaleAccount(account.account_id)
        : resetBaleAccountProfile(account.account_id, account.account_id)
    ), "", formatBaleAccountActionError);
    if (!operation) {
      setActionPending(account.account_id, action, false);
      return;
    }
    const terminal = operation.status && TERMINAL_ACCOUNT_OPERATION_STATUSES.has(operation.status);
    recordDiagnosticEvent({ action: terminal ? "account_operation_terminal" : "account_operation_accepted", clicked_action: action, module: "bale_accounts_ui", account_id: account.account_id, endpoint, http_method: action === "delete_account" ? "DELETE" : "POST", operation_id: operation.operation_id, stage: terminal ? operation.status : "accepted_202", success: !terminal || ["completed", "succeeded"].includes(operation.status), error_code: operation.error_code, error_message: operation.error_message, result: terminal ? operation : null });
    if (!operation.operation_id || terminal) {
      setActionPending(account.account_id, action, false);
      await refresh();
      return;
    }
    setSessions((current) => ({ ...current, [account.account_id]: { operation_id: operation.operation_id, action, status: operation } }));
    startPolling(account.account_id, operation.operation_id, action);
  }

  return (
    <main className="bale-accounts-page" dir="rtl">
      <header className="bale-accounts-header">
        <div>
          <p className="eyebrow">مدیریت اکانت‌های بله</p>
          <h1>اکانت‌ها</h1>
          <p>ورود، بررسی نشست و وضعیت هر اکانت را از همین‌جا مدیریت کنید.</p>
        </div>
        <div className="header-actions">
          <button className="primary-button" onClick={beginCreate} disabled={Boolean(busy)}><Plus size={17} /> افزودن اکانت جدید</button>
          <button className="secondary-button" onClick={() => act("refresh", refresh)} disabled={Boolean(busy)}><RefreshCw size={16} /> بروزرسانی</button>
        </div>
      </header>

      {error ? <div className="inline-error" role="alert">{typeof error === "string" ? error : JSON.stringify(error)}</div> : null}
      {notice ? <div className="onboarding-notice"><CheckCircle2 size={17} /> {notice}</div> : null}

      {/*
      <section className="account-list-panel" aria-label="تنظیمات ماندگاری نشست">
        <button className="secondary-button" onClick={() => setSettingsOpen((value) => !value)}>تنظیمات ورود ذخیره‌شده</button>
        {false ? (
          <form className="account-advanced-details" onSubmit={(event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            act("session-policy", () => updateBaleOperationalConfiguration({
              bale_session_maintenance_mode: form.get("maintenance_mode"),
              session_revalidation_interval_seconds: Number(form.get("session_interval")),
              session_revalidation_grace_seconds: Number(form.get("session_grace")),
              identity_reverify_interval_seconds: Number(form.get("identity_interval")),
              maintenance_browser_concurrency: Number(form.get("maintenance_concurrency")),
              preserve_eligibility_during_inconclusive: form.get("preserve_grace") === "on",
              require_periodic_full_identity_probe: form.get("periodic_identity") === "on",
              activation_policy_after_identity_match: form.get("activation_policy"),
            }), "تنظیمات نشست ذخیره شد.");
          }}>
            <label>حالت نگهداری نشست<select name="maintenance_mode" defaultValue={payload.configuration.bale_session_maintenance_mode || "manual_only"}><option value="manual_only">فقط دستی</option><option value="on_demand">هنگام استفاده</option><option value="scheduled">زمان‌بندی‌شده</option></select></label>
            <label>فاصله بازبینی نشست (ثانیه)<input name="session_interval" type="number" min="30" defaultValue={payload.configuration.session_revalidation_interval_seconds} /></label>
            <label>مهلت نشست (ثانیه)<input name="session_grace" type="number" min="0" defaultValue={payload.configuration.session_revalidation_grace_seconds} /></label>
            <label>فاصله بازتأیید کامل هویت (ثانیه)<input name="identity_interval" type="number" min="60" defaultValue={payload.configuration.identity_reverify_interval_seconds} /></label>
            <label>همزمانی مرورگر نگهداری<input name="maintenance_concurrency" type="number" min="1" defaultValue={payload.configuration.maintenance_browser_concurrency} /></label>
            <label><input name="preserve_grace" type="checkbox" defaultChecked={payload.configuration.preserve_eligibility_during_inconclusive} /> حفظ آمادگی در خطای موقت و بدون شاهد خروج</label>
            <label><input name="periodic_identity" type="checkbox" defaultChecked={payload.configuration.require_periodic_full_identity_probe} /> الزام بازتأیید دوره‌ای کامل هویت</label>
            <label>سیاست فعال‌سازی<select name="activation_policy" defaultValue={payload.configuration.activation_policy_after_identity_match || "operator_approved"}><option value="operator_approved">اکانت تأییدشده اپراتور</option><option value="preserve_current">حفظ وضعیت فعلی</option><option value="manual">فعال‌سازی دستی</option></select></label>
            <button className="primary-button" type="submit">ذخیره تنظیمات</button>
          </form>
        ) : null}
      </section>
      */}

      <section className="account-summary-grid" aria-label="آمار اکانت‌ها">
        {[
          ["همه اکانت‌ها", operatorSummary.total],
          [BALE_AUTH_LABELS.ready, operatorSummary.ready],
          ["نیازمند اقدام", operatorSummary.needsAttention],
        ].map(([label, value]) => <article key={label}><span>{label}</span><strong>{loading ? "…" : value ?? 0}</strong></article>)}
      </section>

      <section className="account-list-panel">
        <div className="account-list-toolbar">
          <label><Search size={16} /><input aria-label="جستجوی اکانت" placeholder="شماره کامل، account_id یا وضعیت" value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }} /></label>
          <select aria-label="فیلتر وضعیت" value={filter} onChange={(event) => { setFilter(event.target.value); setPage(1); }}>
            <option value="all">همه وضعیت‌ها</option>
            {[...new Set(Object.values(BALE_AUTH_LABELS))].map((label) => <option key={label} value={label}>{label}</option>)}
          </select>
        </div>

        {loading ? <div className="loading-state">در حال دریافت اکانت‌ها…</div> : null}
        {!loading && !visible.length ? <div className="empty-state">اکانتی مطابق این فیلتر وجود ندارد.</div> : null}

        <div className="dynamic-account-list">
          {visible.map((account) => {
            const session = sessions[account.account_id];
            const isOpen = expanded === account.account_id;
            const state = userState(account, session, busy);
            const openAvailability = actionAvailability(account, "open_login");
            const recheckAvailability = actionAvailability(account, "session_recheck");
            const deleteAvailability = actionAvailability(account, "delete_account");
            const resetAvailability = actionAvailability(account, "reset_profile");
            return (
              <article className="dynamic-account-row account-centric-row" key={account.account_id}>
                <div className="account-row-main">
                  <span className={`health-dot health-${account.health_status}`} />
                  <span><b>{account.display_name || accountPhone(account)}</b><small>{account.display_name ? accountPhone(account) : "اکانت بله"}</small></span>
                  <span><small>وضعیت</small><b>{state}</b></span>
                  <div className="row-actions account-actions account-primary-actions">
                    <button type="button" className="primary-button" title={openAvailability.reason || ""} data-disabled-reason={openAvailability.reason || ""} onClick={() => openLogin(account, "login")} disabled={openAvailability.disabled}><LockKeyhole size={15} /> ورود</button>
                    <button type="button" className="secondary-button" title={recheckAvailability.reason || ""} data-disabled-reason={recheckAvailability.reason || ""} onClick={() => openLogin(account, "session_recheck")} disabled={recheckAvailability.disabled}>بازبینی نشست</button>
                    <button type="button" className="danger-button" title={deleteAvailability.reason || ""} data-disabled-reason={deleteAvailability.reason || ""} onClick={() => runAccountAction(account, "delete_account")} disabled={deleteAvailability.disabled}>حذف</button>
                    <button type="button" className="secondary-button account-more-actions" onClick={() => setExpanded(isOpen ? "" : account.account_id)} aria-expanded={isOpen}>گزینه‌های بیشتر {isOpen ? <ChevronUp size={15} /> : <ChevronDown size={15} />}</button>
                  </div>
                </div>

                {isOpen ? (
                  <div className="account-row-details">
                    <p className="account-advanced-copy">برای رفع مشکل ورود، ابتدا «بازبینی نشست» را انجام دهید. بازنشانی فقط برای عیب‌یابی است و ورود ذخیره‌شده را پاک می‌کند.</p>
                    {account.last_error ? <div className="inline-error" role="alert">{formatBaleAccountActionError(account.last_error)}</div> : null}
                    <div className="row-actions account-actions">
                      <button type="button" className="danger-button" title={resetAvailability.reason || ""} data-disabled-reason={resetAvailability.reason || ""} onClick={() => runAccountAction(account, "reset_profile")} disabled={resetAvailability.disabled}>بازنشانی اکانت</button>
                    </div>
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
                  {createModal.preflight.checks.map((check) => <article className={`preflight-${check.status}`} key={check.check_name}><b>{check.status === "pass" ? "✓" : "×"} {check.message_fa}</b></article>)}
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
                  <div><dt>شماره</dt><dd>{accountPhone(createModal.account)}</dd></div>
                  <div><dt>وضعیت</dt><dd>نیازمند ورود</dd></div>
                </dl>
                <p>OTP را فقط داخل Bale Web وارد کنید.</p>
                <div className="wizard-actions">
                  <button onClick={() => setCreateModal(null)}>فعلاً بستن</button>
                  <button className="primary-button" onClick={async () => { const opened = await openLogin(createModal.account, "login"); if (opened) setCreateModal(null); }} disabled={Boolean(busy)}><LockKeyhole size={16} /> ورود</button>
                </div>
              </div>
            ) : null}
          </section>
        </div>
      ) : null}

    </main>
  );
}
