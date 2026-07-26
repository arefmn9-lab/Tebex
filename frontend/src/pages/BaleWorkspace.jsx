import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Eye,
  FileSpreadsheet,
  Link2,
  Lock,
  Play,
  Plus,
  RefreshCw,
  Save,
  Settings,
  Square,
  Trash2,
  UserPlus,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  BALE_CONCURRENCY_V1,
  BALE_MAX_RECIPIENTS_V1,
  checkBaleLogin,
  getBaleBulkResults,
  getBaleBulkResumeState,
  getBaleJobs,
  getBaleMessageConfig,
  getBaleSourceChannel,
  listBaleAccounts,
  listBaleLogs,
  listBaleTasks,
  openBaleLogin,
  runBaleBulkDryPreflight,
  saveBaleBulkCampaign,
  saveBaleMessageConfig,
  saveBaleSourceChannel,
  updateBaleAccount,
  validateBaleBulkCampaign,
} from "../api/baleWorkspace";
import { platforms } from "../data/platforms";
import {
  Checkbox,
  CollapsibleAdvancedSettings,
  ContentCard,
  DangerButton,
  FormField,
  IconButton,
  InlineError,
  LoadingState,
  Modal,
  NumberInput,
  PageHeader,
  PrimaryButton,
  SecondaryButton,
  Select,
  StatCard,
  StatusBadge,
  SuccessButton,
  TabBar,
  TextArea,
  TextInput,
} from "../components/ui/DesignSystem.jsx";

const tabs = [
  { id: "accounts", label: "اکانت‌ها" },
  { id: "campaigns", label: "کمپین‌ها" },
  { id: "preparation", label: "آماده‌سازی" },
  { id: "schedule", label: "زمان‌بندی" },
  { id: "reports", label: "گزارش‌ها" },
  { id: "settings", label: "تنظیمات" },
];

const statusLabels = {
  pending: "در انتظار",
  preflight_ready: "آماده ارسال",
  recipient_not_found: "مخاطب پیدا نشد",
  recipient_ambiguous: "چند مخاطب هم‌نام پیدا شد",
  selection_mismatch: "انتخاب مخاطب تأیید نشد",
  structural_failure: "صفحه یا ساختار آماده نبود",
  submitted: "ارسال ثبت شد",
  delivered: "تحویل تأیید شد",
  post_click_ambiguous: "وضعیت پس از ارسال نامشخص است؛ تکرار نمی‌شود",
  skipped_terminal: "قبلاً پردازش شده",
  skipped_disabled: "غیرفعال",
  unauthenticated: "ورود لازم است",
  auth_unverified: "وضعیت ورود قابل تأیید نیست",
  PROFILE_ALREADY_IN_USE: "این حساب در حال استفاده است",
  failed: "خطا",
  completed: "کامل شد",
  partial: "ناتمام",
  valid: "معتبر",
  draft: "پیش‌نویس",
};

const statusTones = {
  preflight_ready: "success",
  submitted: "success",
  delivered: "success",
  completed: "success",
  valid: "success",
  pending: "neutral",
  draft: "neutral",
  skipped_terminal: "warning",
  skipped_disabled: "warning",
  recipient_not_found: "warning",
  recipient_ambiguous: "danger",
  selection_mismatch: "danger",
  structural_failure: "danger",
  failed: "danger",
  post_click_ambiguous: "danger",
  unauthenticated: "danger",
  auth_unverified: "warning",
  PROFILE_ALREADY_IN_USE: "warning",
};

function translateStatus(value) {
  return statusLabels[value] || value || "نامشخص";
}

function toneForStatus(value) {
  return statusTones[value] || "neutral";
}

function normalizeRecipient(value) {
  return String(value || "").trim().replace(/\s+/g, " ").toLocaleLowerCase("fa-IR");
}

function uidFromUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.searchParams.get("uid") || "";
  } catch {
    return "";
  }
}

function sourceUrlFromUid(uid) {
  return uid ? `https://web.bale.ai/chat?uid=${encodeURIComponent(uid)}` : "";
}

function nowCampaignId() {
  const stamp = new Date().toISOString().replace(/[-:T.Z]/g, "").slice(0, 14);
  return `bale_campaign_${stamp}`;
}

function displayDate(value) {
  if (!value) return "ثبت نشده";
  try {
    return new Intl.DateTimeFormat("fa-IR", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
  } catch {
    return String(value).slice(0, 19);
  }
}

function accountAuthState(account) {
  const raw = String(
    account?.auth_status ||
    account?.authentication_status ||
    account?.login_status ||
    account?.runtime_state?.auth_status ||
    account?.status ||
    ""
  ).toLowerCase();
  if (raw.includes("authenticated") && !raw.includes("unauth")) return "authenticated";
  if (raw.includes("unauth") || raw.includes("login_required") || raw.includes("not_logged")) return "unauthenticated";
  if (raw.includes("unverified") || raw.includes("unknown")) return "auth_unverified";
  if (raw.includes("active") || account?.active === true || account?.enabled === true) return "authenticated";
  if (raw.includes("disabled") || account?.active === false || account?.enabled === false) return "disabled";
  return "auth_unverified";
}

function accountStatusLabel(account) {
  const state = accountAuthState(account);
  if (state === "authenticated") return "آماده";
  if (state === "unauthenticated") return "ورود لازم است";
  if (state === "auth_unverified") return "وضعیت ورود قابل تأیید نیست";
  if (state === "disabled") return "غیرفعال";
  return "نامشخص";
}

function accountReady(account) {
  return accountAuthState(account) === "authenticated" && account?.runtime_state?.profile_in_use !== true;
}

function accountId(account) {
  return account?.account_id || account?.id || account?.phone || "";
}

function accountName(account) {
  return account?.display_name || account?.name || account?.phone || account?.username_or_number || accountId(account) || "اکانت بله";
}

function parseRecipients(text) {
  const rows = String(text || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const seen = new Map();
  return rows.map((value, index) => {
    const normalized = normalizeRecipient(value);
    const errors = [];
    if (seen.has(normalized)) errors.push("تکراری است");
    if (index >= BALE_MAX_RECIPIENTS_V1) errors.push("بیش از سقف ۱۰ گیرنده در نسخه فعلی است");
    seen.set(normalized, index);
    return {
      recipient_id: `recipient-${String(index + 1).padStart(3, "0")}`,
      display_name: value,
      enabled: errors.length === 0,
      errors,
      order: index + 1,
    };
  });
}

function countResults(rows) {
  const counts = {
    total: rows.length,
    ready: 0,
    skipped: 0,
    notFound: 0,
    ambiguous: 0,
    submitted: 0,
    failed: 0,
    pending: 0,
  };
  rows.forEach((row) => {
    const state = row.state || "pending";
    if (state === "preflight_ready") counts.ready += 1;
    else if (state === "skipped_terminal" || state === "skipped_disabled") counts.skipped += 1;
    else if (state === "recipient_not_found") counts.notFound += 1;
    else if (state === "recipient_ambiguous") counts.ambiguous += 1;
    else if (state === "submitted" || state === "delivered") counts.submitted += 1;
    else if (["selection_mismatch", "structural_failure", "post_click_ambiguous", "failed"].includes(state)) counts.failed += 1;
    else counts.pending += 1;
  });
  return counts;
}

function defaultDraft(account) {
  return {
    campaign_id: nowCampaignId(),
    name: "",
    platform: "bale",
    account_id: accountId(account),
    source: { title: "منبع بله", uid: "", url: "" },
    recipients_text: "",
    recipients: [],
    message_mode: "forward_source",
    message_text: "",
    batch_size: 5,
    daily_limit_per_account: 50,
    delay_seconds: 5,
    max_recipients: BALE_MAX_RECIPIENTS_V1,
    continue_on_not_found: true,
    stop_on_ambiguous: true,
    stop_on_selection_mismatch: true,
    stop_on_structural_failure: true,
    metadata: { platforms: ["bale"], ui_phase: "phase_2" },
  };
}

function campaignPayload(draft, mode = "dry_run", clickBudget = 0) {
  return {
    campaign_id: draft.campaign_id,
    platform: "bale",
    account_id: draft.account_id,
    source: { uid: draft.source.uid, url: draft.source.url },
    recipients: draft.recipients.slice(0, BALE_MAX_RECIPIENTS_V1).map((recipient) => ({
      recipient_id: recipient.recipient_id,
      display_name: recipient.display_name,
      enabled: recipient.enabled,
    })),
    mode,
    concurrency: BALE_CONCURRENCY_V1,
    max_recipients: BALE_MAX_RECIPIENTS_V1,
    max_final_clicks: mode === "live" ? clickBudget : 0,
    continue_on_not_found: draft.continue_on_not_found,
    stop_on_ambiguous: draft.stop_on_ambiguous,
    stop_on_selection_mismatch: draft.stop_on_selection_mismatch,
    stop_on_structural_failure: draft.stop_on_structural_failure,
    metadata: {
      ...draft.metadata,
      name: draft.name,
      source_title: draft.source.title,
      message_mode: draft.message_mode,
      message_text_present: Boolean(draft.message_text.trim()),
      batch_size: draft.batch_size,
      daily_limit_per_account: draft.daily_limit_per_account,
      delay_seconds: draft.delay_seconds,
    },
  };
}

export default function BaleWorkspace() {
  const [activeTab, setActiveTab] = useState("campaigns");
  const [accounts, setAccounts] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [logs, setLogs] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [messageConfig, setMessageConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState("");
  const [error, setError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [accountModal, setAccountModal] = useState(null);
  const [liveOpen, setLiveOpen] = useState(false);
  const [liveChecked, setLiveChecked] = useState(false);
  const [busy, setBusy] = useState("");
  const [validation, setValidation] = useState(null);
  const [dryRunResult, setDryRunResult] = useState(null);
  const [resumeState, setResumeState] = useState(null);
  const [draft, setDraft] = useState(() => defaultDraft(null));

  const selectedAccount = useMemo(
    () => accounts.find((account) => accountId(account) === draft.account_id) || null,
    [accounts, draft.account_id]
  );
  const parsedRecipients = useMemo(() => parseRecipients(draft.recipients_text), [draft.recipients_text]);
  const rowErrors = parsedRecipients.filter((recipient) => recipient.errors.length);
  const recipientResults = dryRunResult?.recipient_results || resumeState?.items || parsedRecipients.map((recipient) => ({ ...recipient, state: "pending" }));
  const resultCounts = countResults(recipientResults);
  const readyCount = resultCounts.ready;
  const skippedCount = resultCounts.skipped;
  const notFoundCount = resultCounts.notFound;
  const dryRunOk = dryRunResult?.status === "completed" || dryRunResult?.status === "partial";
  const sourceValid = Boolean(draft.source.uid && draft.source.url && uidFromUrl(draft.source.url) === draft.source.uid);
  const selectedAccountReady = selectedAccount ? accountReady(selectedAccount) : false;
  const canValidate = Boolean(draft.name.trim() && draft.account_id && selectedAccountReady && sourceValid && parsedRecipients.length && !rowErrors.length && draft.message_mode === "forward_source");
  const canOpenLive = dryRunOk && readyCount > 0 && selectedAccountReady && sourceValid;
  const maxPossibleFinalClicks = readyCount;
  const running = false;

  useEffect(() => {
    refreshWorkspace();
  }, []);

  useEffect(() => {
    setDraft((current) => ({ ...current, recipients: parsedRecipients }));
  }, [parsedRecipients]);

  async function refreshWorkspace() {
    setLoading(true);
    setError("");
    try {
      const [nextAccounts, nextTasks, nextLogs, nextJobs, nextConfig] = await Promise.all([
        listBaleAccounts(),
        listBaleTasks(),
        listBaleLogs(),
        getBaleJobs(8),
        getBaleMessageConfig().catch(() => null),
      ]);
      const accountList = Array.isArray(nextAccounts) ? nextAccounts : nextAccounts?.items || [];
      setAccounts(accountList);
      setTasks(Array.isArray(nextTasks) ? nextTasks : nextTasks?.items || []);
      setLogs(Array.isArray(nextLogs) ? nextLogs : nextLogs?.items || []);
      setJobs(Array.isArray(nextJobs) ? nextJobs : nextJobs?.items || []);
      setMessageConfig(nextConfig);
      setDraft((current) => {
        if (current.account_id || !accountList[0]) return current;
        return { ...current, account_id: accountId(accountList[0]) };
      });
      setLastUpdated(new Date().toISOString());
    } catch (err) {
      setError(err.message || "دریافت اطلاعات بله انجام نشد");
    } finally {
      setLoading(false);
    }
  }

  function updateDraft(patch) {
    setDraft((current) => ({ ...current, ...patch }));
  }

  function updateSource(patch) {
    setDraft((current) => ({ ...current, source: { ...current.source, ...patch } }));
  }

  async function loadAccountSource(accountIdValue) {
    updateDraft({ account_id: accountIdValue });
    if (!accountIdValue) return;
    try {
      const source = await getBaleSourceChannel(accountIdValue);
      const url = source?.source_channel_url || source?.url || "";
      const uid = source?.source_channel_uid || source?.uid || uidFromUrl(url);
      if (url || uid) {
        updateSource({ title: source?.title || source?.name || "منبع ذخیره‌شده بله", url, uid });
      }
    } catch {
      // Missing saved source should not block manual source entry.
    }
  }

  async function saveDraftOnly() {
    setBusy("save");
    setError("");
    try {
      const payload = campaignPayload(draft);
      const saved = await saveBaleBulkCampaign(payload);
      setValidation(saved?.validation || null);
      return saved;
    } catch (err) {
      setError(err.message || "ذخیره کمپین انجام نشد");
      return null;
    } finally {
      setBusy("");
    }
  }

  async function validateDraft() {
    if (!canValidate) return;
    setBusy("validate");
    setError("");
    try {
      const payload = campaignPayload(draft);
      await saveBaleBulkCampaign(payload);
      const nextValidation = await validateBaleBulkCampaign(draft.campaign_id, payload);
      setValidation(nextValidation);
      return nextValidation;
    } catch (err) {
      setError(err.message || "اعتبارسنجی انجام نشد");
      return null;
    } finally {
      setBusy("");
    }
  }

  async function runDryPreflight() {
    if (!canValidate) return;
    setBusy("dry");
    setError("");
    try {
      const payload = campaignPayload(draft);
      await saveBaleBulkCampaign(payload);
      const nextValidation = await validateBaleBulkCampaign(draft.campaign_id, payload);
      setValidation(nextValidation);
      if (!nextValidation?.ok) return;
      const result = await runBaleBulkDryPreflight(draft.campaign_id);
      setDryRunResult(result);
      const resume = await getBaleBulkResumeState(draft.campaign_id);
      setResumeState(resume);
    } catch (err) {
      setError(err.message || "بررسی بدون ارسال انجام نشد");
    } finally {
      setBusy("");
    }
  }

  async function reloadResults() {
    if (!draft.campaign_id) return;
    setBusy("results");
    try {
      const [result, resume] = await Promise.all([
        getBaleBulkResults(draft.campaign_id).catch(() => null),
        getBaleBulkResumeState(draft.campaign_id).catch(() => null),
      ]);
      if (result?.stored_campaign) setDryRunResult(result.stored_campaign);
      else if (result) setDryRunResult(result);
      if (resume) setResumeState(resume);
    } finally {
      setBusy("");
    }
  }

  async function openLogin(account) {
    const id = accountId(account);
    if (!id) return;
    setBusy(`login:${id}`);
    try {
      await openBaleLogin(id);
    } catch (err) {
      setError(err.message || "بازکردن ورود دستی انجام نشد");
    } finally {
      setBusy("");
    }
  }

  async function checkLogin(account) {
    const id = accountId(account);
    if (!id) return;
    setBusy(`check:${id}`);
    try {
      await checkBaleLogin(id);
      await refreshWorkspace();
    } catch (err) {
      setError(err.message || "بررسی ورود انجام نشد");
    } finally {
      setBusy("");
    }
  }

  const sentLogs = logs.filter((log) => ["sent", "submitted", "delivered"].includes(String(log.status || log.state || "").toLowerCase()));
  const today = new Date().toISOString().slice(0, 10);
  const sentToday = sentLogs.filter((log) => String(log.created_at || log.timestamp || "").startsWith(today));
  const activeAccounts = accounts.filter((account) => accountReady(account));

  return (
    <section className="bale-workspace" dir="rtl">
      <PageHeader
        title="ارسال پیام در بله"
        description="مدیریت اکانت‌ها، کمپین‌ها و ارسال پیام از منبع"
        actions={(
          <>
            <SecondaryButton onClick={() => setAccountModal({ mode: "new" })}><UserPlus size={17} />اکانت جدید</SecondaryButton>
            <SecondaryButton onClick={() => setSettingsOpen(true)}><Settings size={17} />تنظیمات</SecondaryButton>
            <DangerButton disabled={!running}><Square size={17} />توقف</DangerButton>
            <SuccessButton disabled={!canOpenLive} onClick={() => setLiveOpen(true)}><Play size={17} />شروع ارسال</SuccessButton>
          </>
        )}
      />

      {error ? <InlineError>{error}</InlineError> : null}

      <div className="bale-summary-grid">
        <StatCard label="کل اکانت‌ها" value={loading ? "..." : accounts.length} description={`آخرین بروزرسانی: ${displayDate(lastUpdated)}`} />
        <StatCard label="اکانت‌های فعال" value={loading ? "..." : activeAccounts.length} description={loading ? "در حال دریافت" : activeAccounts.length ? "آماده تخصیص" : "اکانت آماده وجود ندارد"} />
        <StatCard label="کل پیام‌های ارسال‌شده" value={loading ? "..." : sentLogs.length} description="بر اساس گزارش‌های موجود" />
        <StatCard label="پیام‌های امروز" value={loading ? "..." : sentToday.length} description="بر اساس زمان ثبت گزارش" />
      </div>

      <TabBar tabs={tabs} activeTab={activeTab} onChange={setActiveTab} />

      {loading && activeTab !== "campaigns" ? <LoadingState label="در حال دریافت اطلاعات بله" /> : null}
      {activeTab === "accounts" ? (
        <AccountsTab accounts={accounts} onOpenLogin={openLogin} onCheckLogin={checkLogin} onEdit={setAccountModal} busy={busy} />
      ) : null}
      {activeTab === "campaigns" ? (
        <CampaignBuilder
          accounts={accounts}
          busy={busy}
          canValidate={canValidate}
          draft={draft}
          dryRunOk={dryRunOk}
          dryRunResult={dryRunResult}
          maxPossibleFinalClicks={maxPossibleFinalClicks}
          onAccountChange={loadAccountSource}
          onLive={() => setLiveOpen(true)}
          onReloadResults={reloadResults}
          onRunDry={runDryPreflight}
          onSave={saveDraftOnly}
          onSourceChange={updateSource}
          onUpdate={updateDraft}
          onValidate={validateDraft}
          parsedRecipients={parsedRecipients}
          resultCounts={resultCounts}
          rowErrors={rowErrors}
          selectedAccount={selectedAccount}
          selectedAccountReady={selectedAccountReady}
          sourceValid={sourceValid}
          validation={validation}
        />
      ) : null}
      {activeTab === "preparation" ? <PreparationTab validation={validation} dryRunResult={dryRunResult} resultCounts={resultCounts} rowErrors={rowErrors} /> : null}
      {activeTab === "schedule" ? <ScheduleTab draft={draft} onUpdate={updateDraft} /> : null}
      {activeTab === "reports" ? <ReportsTab jobs={jobs} tasks={tasks} logs={logs} recipientResults={recipientResults} onNavigateAll={() => { window.location.hash = "#/logs"; }} /> : null}
      {activeTab === "settings" ? <SettingsTab messageConfig={messageConfig} draft={draft} onSettings={() => setSettingsOpen(true)} /> : null}

      <RecentActivity logs={logs} jobs={jobs} />

      {settingsOpen ? (
        <GlobalBaleSettingsModal
          draft={draft}
          messageConfig={messageConfig}
          onClose={() => setSettingsOpen(false)}
          onSaveMessageConfig={saveBaleMessageConfig}
          onSaveSource={saveBaleSourceChannel}
          onUpdate={updateDraft}
          onSourceChange={updateSource}
        />
      ) : null}
      {accountModal ? (
        <AccountModal
          account={accountModal.mode === "new" ? null : accountModal}
          onClose={() => setAccountModal(null)}
          onSave={async (payload) => {
            if (accountModal.mode === "new" && payload.account_id) await openBaleLogin(payload.account_id);
            else if (payload.account_id) await updateBaleAccount(payload.account_id, payload);
            setAccountModal(null);
            await refreshWorkspace();
          }}
        />
      ) : null}
      {liveOpen ? (
        <LiveConfirmation
          account={selectedAccount}
          checked={liveChecked}
          draft={draft}
          maxPossibleFinalClicks={maxPossibleFinalClicks}
          notFoundCount={notFoundCount}
          onChecked={setLiveChecked}
          onClose={() => setLiveOpen(false)}
          readyCount={readyCount}
          skippedCount={skippedCount}
          disabled={!canOpenLive || !liveChecked}
        />
      ) : null}
    </section>
  );
}

function AccountsTab({ accounts, onOpenLogin, onCheckLogin, onEdit, busy }) {
  if (!accounts.length) {
    return (
      <ContentCard title="اکانت‌های بله">
        <div className="empty-state"><strong>اکانت بله ثبت نشده است</strong><p>برای شروع، اکانت جدید را از بالای صفحه اضافه کنید.</p></div>
      </ContentCard>
    );
  }
  return (
    <ContentCard title="اکانت‌های بله" description="وضعیت ورود و ظرفیت روزانه هر اکانت از Backend خوانده می‌شود.">
      <div className="workspace-table-wrap">
        <table className="table bale-table">
          <thead><tr><th>اکانت</th><th>وضعیت</th><th>احراز ورود</th><th>محدودیت روزانه</th><th>مصرف امروز</th><th>ظرفیت باقی‌مانده</th><th>آخرین بررسی</th><th>عملیات</th></tr></thead>
          <tbody>
            {accounts.map((account) => {
              const id = accountId(account);
              const used = Number(account.used_today || account.runtime_state?.sent_today || 0);
              const limit = Number(account.daily_limit || account.settings?.daily_limit || 0);
              return (
                <tr key={id}>
                  <td><strong>{accountName(account)}</strong><small>{id}</small></td>
                  <td><StatusBadge tone={accountReady(account) ? "success" : "warning"}>{accountStatusLabel(account)}</StatusBadge></td>
                  <td>{translateStatus(accountAuthState(account))}</td>
                  <td>{limit || "تنظیم نشده"}</td>
                  <td>{used}</td>
                  <td>{limit ? Math.max(0, limit - used) : "نامشخص"}</td>
                  <td>{displayDate(account.last_health_check || account.updated_at || account.runtime_state?.checked_at)}</td>
                  <td className="row-actions">
                    <IconButton label="مشاهده" onClick={() => onOpenLogin(account)}><Eye size={16} /></IconButton>
                    <IconButton label="بررسی ورود" onClick={() => onCheckLogin(account)} disabled={busy === `check:${id}`}><RefreshCw size={16} /></IconButton>
                    <IconButton label="ویرایش" onClick={() => onEdit(account)}><Settings size={16} /></IconButton>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="mobile-card-list">
        {accounts.map((account) => (
          <article className="mobile-data-card" key={accountId(account)}>
            <strong>{accountName(account)}</strong>
            <p><span>وضعیت</span><b>{accountStatusLabel(account)}</b></p>
            <p><span>محدودیت روزانه</span><b>{account.daily_limit || "تنظیم نشده"}</b></p>
            <div className="row-actions"><SecondaryButton onClick={() => onOpenLogin(account)}>مشاهده</SecondaryButton><SecondaryButton onClick={() => onCheckLogin(account)}>بررسی ورود</SecondaryButton></div>
          </article>
        ))}
      </div>
    </ContentCard>
  );
}

function CampaignBuilder(props) {
  const {
    accounts,
    busy,
    canValidate,
    draft,
    dryRunOk,
    dryRunResult,
    maxPossibleFinalClicks,
    onAccountChange,
    onLive,
    onReloadResults,
    onRunDry,
    onSave,
    onSourceChange,
    onUpdate,
    onValidate,
    parsedRecipients,
    resultCounts,
    rowErrors,
    selectedAccount,
    selectedAccountReady,
    sourceValid,
    validation,
  } = props;

  return (
    <div className="campaign-builder-grid">
      <div className="campaign-main-column">
        <ContentCard title="ساخت کمپین بله" description="فرم ساده برای آماده‌سازی دستی و بررسی بدون ارسال.">
          <div className="campaign-form-grid">
            <FormField label="نام کمپین">
              <TextInput value={draft.name} onChange={(event) => onUpdate({ name: event.target.value })} placeholder="مثلاً پیگیری نوبت‌های امروز" />
            </FormField>
            <FormField label="حساب بله" hint={selectedAccount ? accountStatusLabel(selectedAccount) : "یک حساب آماده انتخاب کنید"} error={selectedAccount && !selectedAccountReady ? "این حساب برای اعتبارسنجی آماده نیست." : ""}>
              <Select value={draft.account_id} onChange={(event) => onAccountChange(event.target.value)}>
                <option value="">انتخاب حساب</option>
                {accounts.map((account) => <option key={accountId(account)} value={accountId(account)}>{accountName(account)} - {accountStatusLabel(account)}</option>)}
              </Select>
            </FormField>
            <FormField label="عنوان منبع">
              <TextInput value={draft.source.title} onChange={(event) => onSourceChange({ title: event.target.value })} />
            </FormField>
            <FormField label="لینک منبع" hint={sourceValid ? "لینک منبع معتبر است" : "لینک بله باید UID معتبر داشته باشد"} error={draft.source.url && !sourceValid ? "UID لینک و مقدار منبع سازگار نیستند." : ""}>
              <TextInput
                value={draft.source.url}
                onChange={(event) => {
                  const url = event.target.value;
                  onSourceChange({ url, uid: uidFromUrl(url) || draft.source.uid });
                }}
                placeholder="https://web.bale.ai/chat?uid=..."
              />
            </FormField>
          </div>

          <section className="builder-section">
            <div className="section-heading">
              <h3>گیرندگان</h3>
              <StatusBadge tone={rowErrors.length ? "danger" : "info"}>{parsedRecipients.length} از ۱۰</StatusBadge>
            </div>
            <TextArea
              value={draft.recipients_text}
              onChange={(event) => onUpdate({ recipients_text: event.target.value })}
              placeholder="هر شماره یا نام مخاطب را در یک خط وارد کنید"
            />
            <div className="recipient-tools">
              <div className="upload-dropzone">
                <FileSpreadsheet size={20} />
                <strong>آپلود Excel/CSV</strong>
                <span>پسوندهای مجاز: .xlsx, .csv</span>
                <SecondaryButton disabled>انتخاب فایل</SecondaryButton>
              </div>
              <SecondaryButton disabled title="در فاز بعدی">انتخاب از بانک شماره</SecondaryButton>
            </div>
            {rowErrors.length ? <InlineError>{rowErrors.length} ردیف نیاز به اصلاح دارد.</InlineError> : null}
            <RecipientPreview recipients={parsedRecipients} />
          </section>

          <section className="builder-section">
            <div className="section-heading"><h3>متن/حالت پیام</h3></div>
            <div className="message-mode-grid">
              <label className="mode-card selected"><input type="radio" checked readOnly />فوروارد از منبع<span>پیام انتخاب‌شده از منبع بله فوروارد می‌شود.</span></label>
              <label className="mode-card disabled"><input type="radio" disabled />پیام متنی<span>ارسال مستقیم متن در Bulk بله هنوز توسط Backend آماده نیست.</span></label>
            </div>
          </section>

          <section className="builder-section">
            <div className="section-heading"><h3>تنظیمات ساده</h3></div>
            <div className="campaign-form-grid">
              <FormField label="تعداد در هر دور"><NumberInput min="1" max="10" value={draft.batch_size} onChange={(event) => onUpdate({ batch_size: Number(event.target.value) })} /></FormField>
              <FormField label="محدودیت روزانه هر اکانت"><NumberInput min="1" value={draft.daily_limit_per_account} onChange={(event) => onUpdate({ daily_limit_per_account: Number(event.target.value) })} /></FormField>
              <FormField label="تأخیر بین عملیات"><NumberInput min="0" value={draft.delay_seconds} onChange={(event) => onUpdate({ delay_seconds: Number(event.target.value) })} /></FormField>
              <Checkbox label="ادامه پس از پیدا نشدن مخاطب" checked={draft.continue_on_not_found} onChange={(event) => onUpdate({ continue_on_not_found: event.target.checked })} />
              <Checkbox label="توقف در صورت چند مخاطب هم‌نام" checked={draft.stop_on_ambiguous} onChange={(event) => onUpdate({ stop_on_ambiguous: event.target.checked })} />
            </div>
          </section>

          <CollapsibleAdvancedSettings title="جزئیات فنی کمپین">
            <div className="campaign-form-grid">
              <FormField label="شناسه کمپین"><TextInput value={draft.campaign_id} onChange={(event) => onUpdate({ campaign_id: event.target.value })} /></FormField>
              <FormField label="UID منبع"><TextInput value={draft.source.uid} onChange={(event) => onSourceChange({ uid: event.target.value, url: sourceUrlFromUid(event.target.value) || draft.source.url })} /></FormField>
              <FormField label="حداکثر گیرندگان"><NumberInput value={BALE_MAX_RECIPIENTS_V1} readOnly /></FormField>
              <FormField label="همزمانی"><NumberInput value={BALE_CONCURRENCY_V1} readOnly /></FormField>
              <Checkbox label="توقف در صورت تأیید نشدن انتخاب مخاطب" checked={draft.stop_on_selection_mismatch} onChange={(event) => onUpdate({ stop_on_selection_mismatch: event.target.checked })} />
              <Checkbox label="توقف در صورت آماده نبودن ساختار صفحه" checked={draft.stop_on_structural_failure} onChange={(event) => onUpdate({ stop_on_structural_failure: event.target.checked })} />
            </div>
          </CollapsibleAdvancedSettings>

          <div className="builder-actions">
            <SecondaryButton onClick={onSave} disabled={busy === "save"}><Save size={17} />ذخیره پیش‌نویس</SecondaryButton>
            <SecondaryButton onClick={onValidate} disabled={!canValidate || busy === "validate"}><CheckCircle2 size={17} />اعتبارسنجی</SecondaryButton>
            <PrimaryButton onClick={onRunDry} disabled={!canValidate || busy === "dry"}><Play size={17} />بررسی بدون ارسال</PrimaryButton>
            <SecondaryButton onClick={onReloadResults} disabled={busy === "results"}><RefreshCw size={17} />دریافت نتیجه</SecondaryButton>
            <SuccessButton onClick={onLive} disabled={!dryRunOk || maxPossibleFinalClicks <= 0}><Lock size={17} />ارسال واقعی</SuccessButton>
          </div>
          {!canValidate ? <InlineError>برای بررسی بدون ارسال، نام کمپین، حساب آماده، منبع معتبر و گیرندگان بدون خطا لازم است.</InlineError> : null}
        </ContentCard>
      </div>

      <aside className="campaign-side-column">
        <ResultSummary resultCounts={resultCounts} />
        <ValidationPanel validation={validation} dryRunResult={dryRunResult} />
      </aside>
    </div>
  );
}

function RecipientPreview({ recipients }) {
  if (!recipients.length) return null;
  return (
    <div className="recipient-preview-list">
      {recipients.map((recipient) => (
        <article className={recipient.errors.length ? "invalid" : ""} key={recipient.recipient_id}>
          <span>{recipient.order}</span>
          <strong>{recipient.display_name}</strong>
          <small>{recipient.errors.length ? recipient.errors.join("، ") : "آماده بررسی"}</small>
        </article>
      ))}
    </div>
  );
}

function ResultSummary({ resultCounts }) {
  return (
    <ContentCard title="نتیجه">
      <div className="result-summary-grid">
        <StatCard label="کل گیرندگان" value={resultCounts.total} />
        <StatCard label="آماده ارسال" value={resultCounts.ready} />
        <StatCard label="قبلاً پردازش‌شده" value={resultCounts.skipped} />
        <StatCard label="پیدا نشد" value={resultCounts.notFound} />
        <StatCard label="مبهم" value={resultCounts.ambiguous} />
        <StatCard label="ارسال ثبت‌شده" value={resultCounts.submitted} />
        <StatCard label="خطادار" value={resultCounts.failed} />
      </div>
    </ContentCard>
  );
}

function ValidationPanel({ validation, dryRunResult }) {
  return (
    <ContentCard title="وضعیت کمپین">
      <div className="status-stack">
        <p><span>اعتبارسنجی</span><StatusBadge tone={validation?.ok ? "success" : "neutral"}>{validation?.ok ? "معتبر" : "در انتظار"}</StatusBadge></p>
        <p><span>بررسی بدون ارسال</span><StatusBadge tone={toneForStatus(dryRunResult?.status)}>{translateStatus(dryRunResult?.status || "pending")}</StatusBadge></p>
        <p><span>دلیل توقف</span><b>{translateStatus(dryRunResult?.stop_reason) || "ندارد"}</b></p>
      </div>
    </ContentCard>
  );
}

function PreparationTab({ validation, dryRunResult, resultCounts, rowErrors }) {
  return (
    <ContentCard title="آماده‌سازی" description="خلاصه بررسی بدون ارسال و خطاهای آماده‌سازی.">
      <div className="bale-summary-grid compact">
        <StatCard label="آماده ارسال" value={resultCounts.ready} />
        <StatCard label="در انتظار" value={resultCounts.pending} />
        <StatCard label="خطاهای ورودی" value={rowErrors.length} />
        <StatCard label="وضعیت Backend" value={validation?.ok ? "معتبر" : "در انتظار"} />
      </div>
      {dryRunResult?.stop_reason ? <InlineError>توقف: {translateStatus(dryRunResult.stop_reason)}</InlineError> : null}
    </ContentCard>
  );
}

function ScheduleTab({ draft, onUpdate }) {
  return (
    <ContentCard title="زمان‌بندی" description="تنظیمات زمان‌بندی فعلی فقط به پیکربندی کمپین نگاشت می‌شود؛ موتور زمان‌بندی جدید در این فاز اضافه نشده است.">
      <div className="campaign-form-grid">
        <FormField label="تعداد در هر دور"><NumberInput value={draft.batch_size} onChange={(event) => onUpdate({ batch_size: Number(event.target.value) })} /></FormField>
        <FormField label="محدودیت روزانه هر اکانت"><NumberInput value={draft.daily_limit_per_account} onChange={(event) => onUpdate({ daily_limit_per_account: Number(event.target.value) })} /></FormField>
        <FormField label="تأخیر بین عملیات"><NumberInput value={draft.delay_seconds} onChange={(event) => onUpdate({ delay_seconds: Number(event.target.value) })} /></FormField>
      </div>
    </ContentCard>
  );
}

function ReportsTab({ jobs, tasks, logs, recipientResults, onNavigateAll }) {
  return (
    <ContentCard title="گزارش‌ها" description="نتایج گیرندگان و چند فعالیت اخیر.">
      <ResultTable rows={recipientResults} />
      <div className="recent-inline-list">
        {[...jobs, ...tasks, ...logs].slice(0, 5).map((item, index) => (
          <article key={item.job_id || item.task_id || item.id || index}>
            <strong>{item.contact_naming_value || item.recipient_id || item.lead || item.contact || "گیرنده"}</strong>
            <StatusBadge tone={toneForStatus(item.status || item.state)}>{translateStatus(item.status || item.state)}</StatusBadge>
            <span>{displayDate(item.updated_at || item.created_at || item.timestamp)}</span>
          </article>
        ))}
      </div>
      <SecondaryButton onClick={onNavigateAll}>مشاهده همه گزارش‌ها</SecondaryButton>
    </ContentCard>
  );
}

function ResultTable({ rows }) {
  if (!rows.length) return <div className="empty-state"><strong>نتیجه‌ای ثبت نشده است</strong></div>;
  return (
    <>
      <div className="workspace-table-wrap">
        <table className="table bale-table">
          <thead><tr><th>ردیف</th><th>گیرنده</th><th>وضعیت بررسی</th><th>وضعیت ارسال</th><th>توضیح</th><th>امکان ادامه</th></tr></thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={row.recipient_id || index}>
                <td>{index + 1}</td>
                <td>{row.display_name || row.recipient_id}</td>
                <td><StatusBadge tone={toneForStatus(row.state)}>{translateStatus(row.state || "pending")}</StatusBadge></td>
                <td>{translateStatus(row.delivery_status || row.state || "pending")}</td>
                <td>{translateStatus(row.error_code) || row.reason || "-"}</td>
                <td>{row.retry_allowed === false ? "خیر" : "بر اساس وضعیت Backend"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mobile-card-list">
        {rows.map((row, index) => (
          <details className="mobile-data-card" key={row.recipient_id || index}>
            <summary>{row.display_name || row.recipient_id}</summary>
            <p><span>وضعیت</span><b>{translateStatus(row.state || "pending")}</b></p>
            <p><span>توضیح</span><b>{translateStatus(row.error_code) || "-"}</b></p>
          </details>
        ))}
      </div>
    </>
  );
}

function SettingsTab({ messageConfig, draft, onSettings }) {
  return (
    <ContentCard title="تنظیمات بله">
      <div className="status-stack">
        <p><span>روش فعلی پیام</span><b>{draft.message_mode === "forward_source" ? "فوروارد از منبع" : "پیام متنی"}</b></p>
        <p><span>پیکربندی Backend</span><b>{messageConfig ? "دریافت شد" : "ثبت نشده"}</b></p>
      </div>
      <PrimaryButton onClick={onSettings}><Settings size={17} />بازکردن تنظیمات</PrimaryButton>
    </ContentCard>
  );
}

function RecentActivity({ logs, jobs }) {
  const rows = [...jobs, ...logs].slice(0, 4);
  return (
    <ContentCard title="فعالیت‌های اخیر کمپین" actions={<SecondaryButton onClick={() => { window.location.hash = "#/logs"; }}>مشاهده همه گزارش‌ها</SecondaryButton>}>
      <div className="recent-inline-list">
        {rows.length ? rows.map((row, index) => (
          <article key={row.job_id || row.id || index}>
            <strong>{row.contact_naming_value || row.recipient_id || row.lead || "گیرنده"}</strong>
            <span>بله</span>
            <StatusBadge tone={toneForStatus(row.status || row.state)}>{translateStatus(row.status || row.state || "pending")}</StatusBadge>
            <span>{displayDate(row.updated_at || row.created_at || row.timestamp)}</span>
          </article>
        )) : <div className="empty-state"><strong>فعالیت تازه‌ای ثبت نشده است</strong></div>}
      </div>
    </ContentCard>
  );
}

function GlobalBaleSettingsModal({ draft, messageConfig, onClose, onSaveMessageConfig, onSaveSource, onUpdate, onSourceChange }) {
  const [saving, setSaving] = useState(false);
  return (
    <Modal title="تنظیمات عمومی ارسال بله" onClose={onClose}>
      <div className="settings-modal-grid">
        <FormField label="روش ارسال"><Select value="forward_source" disabled><option value="forward_source">فوروارد از منبع</option></Select></FormField>
        <FormField label="منابع ارسال"><TextInput value={draft.source.url} onChange={(event) => onSourceChange({ url: event.target.value, uid: uidFromUrl(event.target.value) || draft.source.uid })} /></FormField>
        <FormField label="مکث/زمان انتظار آماده‌شدن منبع"><NumberInput value={messageConfig?.open_timeout_seconds || 5} onChange={() => {}} /></FormField>
        <FormField label="تعداد ارسال در هر دور"><NumberInput value={draft.batch_size} onChange={(event) => onUpdate({ batch_size: Number(event.target.value) })} /></FormField>
        <FormField label="محدودیت روزانه هر اکانت"><NumberInput value={draft.daily_limit_per_account} onChange={(event) => onUpdate({ daily_limit_per_account: Number(event.target.value) })} /></FormField>
        <FormField label="حداکثر اکانت‌های قابل تخصیص"><NumberInput value="1" readOnly /></FormField>
        <FormField label="تأخیر بین عملیات"><NumberInput value={draft.delay_seconds} onChange={(event) => onUpdate({ delay_seconds: Number(event.target.value) })} /></FormField>
      </div>
      <div className="modal-actions">
        <PrimaryButton disabled={saving} onClick={async () => {
          setSaving(true);
          try {
            await Promise.all([
              onSaveMessageConfig({ ...(messageConfig || {}), source_type: "message_link", source_value: draft.source.url, dry_run: true }),
              draft.account_id ? onSaveSource({ account_id: draft.account_id, source_channel_url: draft.source.url }) : Promise.resolve(),
            ]);
            onClose();
          } finally {
            setSaving(false);
          }
        }}>ذخیره تنظیمات</PrimaryButton>
        <SecondaryButton onClick={onClose}>انصراف</SecondaryButton>
      </div>
    </Modal>
  );
}

function AccountModal({ account, onClose, onSave }) {
  const [form, setForm] = useState(() => ({
    account_id: accountId(account),
    display_name: accountName(account),
    active: account?.active ?? account?.enabled ?? true,
    daily_limit: account?.daily_limit || 50,
    notes: account?.notes || "",
  }));
  return (
    <Modal title={account ? "ویرایش اکانت بله" : "اکانت جدید"} onClose={onClose}>
      <div className="settings-modal-grid">
        <FormField label="نام نمایشی"><TextInput value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} /></FormField>
        <FormField label="شناسه/شماره حساب"><TextInput value={form.account_id} onChange={(event) => setForm({ ...form, account_id: event.target.value })} /></FormField>
        <Checkbox label="فعال" checked={form.active} onChange={(event) => setForm({ ...form, active: event.target.checked })} />
        <FormField label="محدودیت روزانه"><NumberInput value={form.daily_limit} onChange={(event) => setForm({ ...form, daily_limit: Number(event.target.value) })} /></FormField>
      </div>
      <p className="page-copy">{account ? "مسیر پروفایل در نمای عادی نمایش داده نمی‌شود و Backend پروفایل canonical را مدیریت می‌کند." : "با ذخیره، فرآیند ورود دستی همین شناسه با پروفایل canonical Backend باز می‌شود."}</p>
      <div className="modal-actions">
        <PrimaryButton onClick={() => onSave(form)}>ذخیره</PrimaryButton>
        <SecondaryButton onClick={onClose}>انصراف</SecondaryButton>
      </div>
    </Modal>
  );
}

function LiveConfirmation({ account, checked, disabled, draft, maxPossibleFinalClicks, notFoundCount, onChecked, onClose, readyCount, skippedCount }) {
  return (
    <Modal title="تأیید ارسال واقعی" onClose={onClose}>
      <div className="live-confirm-grid">
        <p><span>اکانت</span><b>{accountName(account)}</b></p>
        <p><span>منبع</span><b>{draft.source.title}</b></p>
        <p><span>آماده ارسال</span><b>{readyCount}</b></p>
        <p><span>قبلاً پردازش‌شده</span><b>{skippedCount}</b></p>
        <p><span>پیدا نشد</span><b>{notFoundCount}</b></p>
        <p><span>حداکثر کلیک نهایی ممکن</span><b>{maxPossibleFinalClicks}</b></p>
      </div>
      <InlineError>گیرندگانی که کلیک نهایی برای آن‌ها ثبت شده باشد به صورت خودکار تکرار نمی‌شوند.</InlineError>
      <Checkbox label="تأیید می‌کنم ارسال واقعی برای این تعداد گیرنده انجام شود." checked={checked} onChange={(event) => onChecked(event.target.checked)} />
      <div className="modal-actions">
        <DangerButton disabled={disabled}>شروع ارسال واقعی</DangerButton>
        <SecondaryButton onClick={onClose}>انصراف</SecondaryButton>
      </div>
    </Modal>
  );
}

export { parseRecipients, campaignPayload, translateStatus };
