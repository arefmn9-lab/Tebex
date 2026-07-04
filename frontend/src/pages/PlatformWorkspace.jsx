import { Edit3, Eye, Play, Plus, Save, Settings, Square, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../api/client";
import {
  assignBaleProfileGroup,
  assignBulkCampaign,
  checkAdsPowerHealth,
  createAccountGroup,
  createBulkCampaign,
  createBulkCampaignRoute,
  createBulkContactList,
  createBulkExecutionQueue,
  createBulkMessageSource,
  createPlatformAccount,
  deleteBaleAccount,
  dryRunBulkExecutionQueue,
  dryRunBalePreparation,
  dryRunBaleSchedule,
  getBaleMessageConfig,
  getBalePreparation,
  getAdsPowerConfig,
  getBulkExecutionQueueSummary,
  importBulkContactList,
  listBaleProfileGroups,
  listBaleScenarios,
  listBulkCampaigns,
  listBulkContactLists,
  listBulkContacts,
  listBulkExecutionQueue,
  listBulkMessageSources,
  listPlatformAccountGroups,
  listPlatformAccounts,
  listPlatformLogs,
  listPlatformTasks,
  openAccountBrowser,
  openBaleAccount,
  planBulkCampaign,
  runBaleExecutionQueue,
  saveBaleMessageConfig,
  saveBalePreparation,
  saveAdsPowerConfig,
  testBaleForward,
  updateAccountGroup,
  updateBaleAccount,
  updateBulkCampaign,
  updateBulkContactList,
  updateBulkMessageSource,
} from "../api/platforms";
import StatusCard from "../components/StatusCard.jsx";
import { getPlatform } from "../data/platforms";
import { useAccounts } from "../hooks/useAccounts";
import { useLogs } from "../hooks/useLogs";
import { useTasks } from "../hooks/useTasks";

const today = new Date().toISOString().slice(0, 10);

const defaultBaleAccount = {
  phone: "",
  username_or_number: "",
  account_group_id: "bale_test_group",
  account_group_name: "Bale Test Group",
  browser_provider: "adspower",
  adspower_profile_id: "",
  profile_group_id: "group_001",
  status: "new",
  section: "new_accounts",
  daily_limit: 10,
  hourly_limit: 2,
  min_delay_seconds: 300,
  max_actions_per_session: 5,
  health_score: 100,
  login_status: "unknown",
  block_status: "unknown",
  consecutive_failures: 0,
  notes: "",
};

const defaultAccountGroup = {
  group_id: "",
  name: "",
  platform_id: "bale",
  browser_provider: "native_chrome",
  device_group_id: "device_group_001",
  profile_group_id: "default",
  max_concurrent: 5,
  batch_capacity: 30,
  daily_capacity: 100,
  enabled: true,
  notes: "",
};

const defaultBulkCampaign = {
  campaign_id: "",
  name: "",
  campaign_tag: "",
  status: "draft",
  dry_run: true,
  notes: "",
};

const defaultMessageSourceForm = {
  message_source_id: "",
  platform_id: "bale",
  name: "",
  campaign_tag: "",
  source_type: "channel",
  source_ref: "",
  message_ref_type: "latest",
  message_ref_value: "",
  enabled: true,
  notes: "",
};

const defaultContactListForm = {
  contact_list_id: "",
  name: "",
  platform_id: "",
  campaign_tag: "",
  source_filename: "",
  total_contacts: 0,
  valid_contacts: 0,
  duplicate_contacts: 0,
  status: "draft",
  notes: "",
};

const defaultCampaignRouteForm = {
  route_id: "",
  platform_id: "bale",
  account_group_id: "",
  message_source_id: "",
  contact_list_id: "",
  scenario_id: "save_contact_and_forward_from_source",
  contact_naming_pattern: "Bale-GHAB-{seq:06d}",
  daily_limit_per_account: 50,
  hourly_limit_per_account: 5,
  enabled: true,
};

const defaultAdsPowerConfig = {
  enabled: true,
  api_base_url: "http://127.0.0.1:50325",
  api_token: "",
  open_timeout_seconds: 60,
};

const defaultMessageConfig = {
  source_type: "message_link",
  source_value: "",
  source_message_hint: "",
  description: "",
  dry_run: true,
  target: "",
  account_id: "",
};

const defaultPreparation = {
  enabled: false,
  mode: "qa_only",
  selected_accounts: [],
  rules: {
    only_owned_accounts: true,
    manual_approval_required: true,
    max_test_messages_per_account_per_day: 3,
    min_delay_seconds: 300,
    stop_on_failure: true,
  },
  account_sections: ["new_accounts", "week_1", "week_2", "month_1", "old_accounts"],
};

const defaultCompliancePolicy = {
  enabled: true,
  mode: "conservative",
  respect_account_limits: true,
  require_manual_login: true,
  stop_on_error: true,
  stop_on_login_required: true,
  stop_on_rate_limit_warning: true,
  stop_on_block_or_limit_detected: true,
  max_consecutive_failures_per_account: 2,
  min_delay_between_actions_seconds: 300,
  max_actions_per_account_per_hour: 2,
  max_actions_per_account_per_day: 10,
  quiet_hours: {
    enabled: true,
    start: "23:00",
    end: "08:00",
  },
  allowed_targets_only: true,
  dry_run_default: true,
  global_safety_stops: {
    max_failed_jobs_per_run: 5,
    max_failure_rate_percent: 20,
    stop_all_on_provider_error: true,
    stop_all_on_network_error: false,
  },
  randomization: {
    enabled: true,
    shuffle_account_order: true,
    shuffle_batch_order: true,
    jitter_minutes_min: 3,
    jitter_minutes_max: 20,
    avoid_same_time_as_previous_day: true,
    avoid_same_account_order_as_previous_day: true,
    max_daily_time_shift_minutes: 90,
  },
};

function samePlatform(item, platformId) {
  return String(item?.platform || item?.platform_id || "").toLowerCase() === platformId;
}

function displayDate(value) {
  if (!value) return "-";
  return String(value).replace("T", " ").slice(0, 19);
}

function statusTone(status) {
  if (["active", "running", "done", "success", "ok"].includes(status)) return "success";
  if (["failed", "error", "blocked", "disabled"].includes(status)) return "danger";
  if (["limited", "paused", "preparing", "needs_check"].includes(status)) return "warning";
  return "neutral";
}

function Modal({ title, children, onClose }) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal-panel" role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-header">
          <h3>{title}</h3>
          <button className="icon-button" onClick={onClose} type="button" aria-label="بستن">
            <X size={17} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <label>
      <span>{label}</span>
      {children}
    </label>
  );
}

function Pill({ value }) {
  return (
    <span className="pill">
      <span className={`status-dot ${statusTone(value)}`} />
      {value || "-"}
    </span>
  );
}

export default function PlatformWorkspace({ platformId }) {
  const platform = getPlatform(platformId);
  const { accounts, activeAccounts } = useAccounts();
  const { tasks, run, stop } = useTasks();
  const { logs } = useLogs();
  const [platformData, setPlatformData] = useState(null);
  const [platformDataError, setPlatformDataError] = useState("");
  const [activeTab, setActiveTab] = useState("accounts");
  const [accountModalOpen, setAccountModalOpen] = useState(false);
  const [accountGroupModalOpen, setAccountGroupModalOpen] = useState(false);
  const [bulkCampaignModalOpen, setBulkCampaignModalOpen] = useState(false);
  const [bulkSourceModalOpen, setBulkSourceModalOpen] = useState(false);
  const [bulkContactListModalOpen, setBulkContactListModalOpen] = useState(false);
  const [bulkRouteModalOpen, setBulkRouteModalOpen] = useState(false);
  const [adsPowerSettingsOpen, setAdsPowerSettingsOpen] = useState(false);
  const [formErrors, setFormErrors] = useState({});
  const [editingAccount, setEditingAccount] = useState(null);
  const [editingAccountGroup, setEditingAccountGroup] = useState(null);
  const [editingBulkCampaign, setEditingBulkCampaign] = useState(null);
  const [editingBulkSource, setEditingBulkSource] = useState(null);
  const [editingBulkContactList, setEditingBulkContactList] = useState(null);
  const [routeCampaignId, setRouteCampaignId] = useState("");
  const [accountForm, setAccountForm] = useState(defaultBaleAccount);
  const [accountGroupForm, setAccountGroupForm] = useState(defaultAccountGroup);
  const [bulkCampaignForm, setBulkCampaignForm] = useState(defaultBulkCampaign);
  const [bulkSourceForm, setBulkSourceForm] = useState(defaultMessageSourceForm);
  const [bulkContactListForm, setBulkContactListForm] = useState(defaultContactListForm);
  const [bulkRouteForm, setBulkRouteForm] = useState(defaultCampaignRouteForm);
  const [adsPowerConfig, setAdsPowerConfig] = useState(defaultAdsPowerConfig);
  const [messageConfig, setMessageConfig] = useState(defaultMessageConfig);
  const [scenarios, setScenarios] = useState([]);
  const [preparation, setPreparation] = useState(defaultPreparation);
  const [compliancePolicy, setCompliancePolicy] = useState(defaultCompliancePolicy);
  const [profileGroups, setProfileGroups] = useState([]);
  const [accountGroups, setAccountGroups] = useState([]);
  const [bulkCampaigns, setBulkCampaigns] = useState([]);
  const [bulkMessageSources, setBulkMessageSources] = useState([]);
  const [bulkContactLists, setBulkContactLists] = useState([]);
  const [bulkPlanResult, setBulkPlanResult] = useState(null);
  const [bulkAssignmentResult, setBulkAssignmentResult] = useState(null);
  const [bulkQueueResult, setBulkQueueResult] = useState(null);
  const [bulkQueueJobs, setBulkQueueJobs] = useState([]);
  const [bulkRealRunResult, setBulkRealRunResult] = useState(null);
  const [bulkRealRunForm, setBulkRealRunForm] = useState({
    limit: 1,
    account_id: "",
  });
  const [assignmentForm, setAssignmentForm] = useState({
    planned_for_date: today,
    max_contacts_per_account: "",
    plan_seed: "",
  });
  const [contactImportForm, setContactImportForm] = useState({
    name: "",
    campaign_tag: "",
    platform_id: "",
    notes: "",
    file: null,
  });
  const [contactImportResult, setContactImportResult] = useState(null);
  const [sampleImportedContacts, setSampleImportedContacts] = useState([]);
  const [planResult, setPlanResult] = useState(null);
  const [toast, setToast] = useState("");
  const [actionError, setActionError] = useState("");

  async function loadPlatformData(ignore = false) {
    try {
      const [platformAccounts, platformTasks, platformLogs] = await Promise.all([
        listPlatformAccounts(platform.id),
        listPlatformTasks(platform.id),
        listPlatformLogs(platform.id),
      ]);
      if (!ignore) {
        setPlatformData({
          accounts: Array.isArray(platformAccounts) ? platformAccounts : [],
          tasks: Array.isArray(platformTasks) ? platformTasks : [],
          logs: Array.isArray(platformLogs) ? platformLogs : [],
        });
        setPlatformDataError("");
      }
    } catch (error) {
      if (!ignore) {
        setPlatformData(null);
        setPlatformDataError(error.message);
      }
    }
  }

  useEffect(() => {
    let ignore = false;
    loadPlatformData(ignore);
    const timer = window.setInterval(() => loadPlatformData(ignore), 5000);
    return () => {
      ignore = true;
      window.clearInterval(timer);
    };
  }, [platform.id]);

  useEffect(() => {
    if (platform.id !== "bale") return;
    let ignore = false;
    async function loadBaleGovernance() {
      try {
        const [config, scenarioList, prep, groups, schedulerGroups, campaigns, sources, contactLists, providerConfig] = await Promise.all([
          getBaleMessageConfig(),
          listBaleScenarios(),
          getBalePreparation(),
          listBaleProfileGroups(),
          listPlatformAccountGroups("bale"),
          listBulkCampaigns(),
          listBulkMessageSources(),
          listBulkContactLists(),
          getAdsPowerConfig(),
        ]);
        if (!ignore) {
          setMessageConfig((current) => ({ ...current, ...config }));
          setScenarios(Array.isArray(scenarioList) ? scenarioList : []);
          setPreparation({ ...defaultPreparation, ...prep, rules: { ...defaultPreparation.rules, ...(prep?.rules || {}) } });
          setProfileGroups(Array.isArray(groups) ? groups : []);
          setAccountGroups(Array.isArray(schedulerGroups) ? schedulerGroups : []);
          setBulkCampaigns(Array.isArray(campaigns) ? campaigns : []);
          setBulkMessageSources(Array.isArray(sources) ? sources : []);
          setBulkContactLists(Array.isArray(contactLists) ? contactLists : []);
          setAdsPowerConfig({ ...defaultAdsPowerConfig, ...providerConfig });
        }
      } catch (error) {
        if (!ignore) setActionError(error.message);
      }
    }
    loadBaleGovernance();
    return () => {
      ignore = true;
    };
  }, [platform.id]);

  const platformAccounts = useMemo(
    () => platformData?.accounts ?? accounts.filter((account) => samePlatform(account, platform.id)),
    [accounts, platform.id, platformData]
  );
  const platformTasks = useMemo(
    () => platformData?.tasks ?? tasks.filter((task) => samePlatform(task, platform.id)),
    [tasks, platform.id, platformData]
  );
  const platformLogs = useMemo(
    () => platformData?.logs ?? logs.filter((log) => samePlatform(log, platform.id)),
    [logs, platform.id, platformData]
  );

  const totalSent = platformLogs.filter((log) => String(log.status || "").toLowerCase() === "sent").length;
  const sentToday = platformLogs.filter((log) => String(log.created_at || log.timestamp || "").startsWith(today)).length;
  const platformActiveAccounts = platformAccounts.filter((account) => account.status === "active" || account.active).length;

  async function refresh() {
    await loadPlatformData(false);
  }

  async function handleOpenBrowser(accountId) {
    try {
      setActionError("");
      const result = platform.id === "bale" ? await openBaleAccount(accountId) : await openAccountBrowser(accountId);
      setToast(result?.ok ? "نشست مرورگر باز شد" : result?.message || "درخواست باز کردن نشست ثبت شد");
    } catch (error) {
      setToast("");
      setActionError(error.message);
    }
  }

  async function handleRunFirstTask() {
    const task = platformTasks.find((item) => ["pending", "failed"].includes(item.status));
    if (!task) {
      setToast("وظیفه آماده‌ای برای شروع وجود ندارد");
      return;
    }
    try {
      await run(task.task_id);
      setToast("شروع ارسال ثبت شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function handleStopFirstTask() {
    const task = platformTasks.find((item) => item.status === "running");
    if (!task) {
      setToast("وظیفه در حال اجرایی برای توقف وجود ندارد");
      return;
    }
    try {
      await stop(task.task_id);
      setToast("درخواست توقف ارسال شد");
    } catch (error) {
      const unavailable = error instanceof ApiError && error.status === 404;
      setActionError(unavailable ? "Stop endpoint is not available in this backend." : error.message);
    }
  }

  function openAccountModal(account = null) {
    setFormErrors({});
    setEditingAccount(account);
    setAccountForm(account ? { ...defaultBaleAccount, ...account } : defaultBaleAccount);
    setAccountModalOpen(true);
  }

  function openAccountGroupModal(group = null) {
    setFormErrors({});
    setEditingAccountGroup(group);
    setAccountGroupForm(group ? { ...defaultAccountGroup, ...group } : defaultAccountGroup);
    setAccountGroupModalOpen(true);
  }

  async function reloadBulkData() {
    const [campaigns, sources, contactLists] = await Promise.all([
      listBulkCampaigns(),
      listBulkMessageSources(),
      listBulkContactLists(),
    ]);
    setBulkCampaigns(Array.isArray(campaigns) ? campaigns : []);
    setBulkMessageSources(Array.isArray(sources) ? sources : []);
    setBulkContactLists(Array.isArray(contactLists) ? contactLists : []);
  }

  function openBulkCampaignModal(campaign = null) {
    setEditingBulkCampaign(campaign);
    setBulkCampaignForm(campaign ? { ...defaultBulkCampaign, ...campaign } : defaultBulkCampaign);
    setBulkCampaignModalOpen(true);
  }

  function openBulkSourceModal(source = null) {
    setEditingBulkSource(source);
    setBulkSourceForm(source ? { ...defaultMessageSourceForm, ...source } : defaultMessageSourceForm);
    setBulkSourceModalOpen(true);
  }

  function openBulkContactListModal(contactList = null) {
    setEditingBulkContactList(contactList);
    setBulkContactListForm(contactList ? { ...defaultContactListForm, ...contactList } : defaultContactListForm);
    setBulkContactListModalOpen(true);
  }

  function openBulkRouteModal(campaignId) {
    setRouteCampaignId(campaignId);
    setBulkRouteForm({
      ...defaultCampaignRouteForm,
      account_group_id: accountGroups[0]?.group_id || "",
      message_source_id: bulkMessageSources[0]?.message_source_id || "",
      contact_list_id: bulkContactLists[0]?.contact_list_id || "",
    });
    setBulkRouteModalOpen(true);
  }

  async function saveAccount() {
    const errors = {};
    if (!accountForm.phone.trim() && !accountForm.username_or_number.trim()) {
      errors.identity = "شماره تلفن یا نام کاربری الزامی است";
    }
    if (accountForm.browser_provider === "adspower" && !String(accountForm.adspower_profile_id || "").trim()) {
      errors.adspower_profile_id = "شناسه پروفایل AdsPower الزامی است";
    }
    setFormErrors(errors);
    if (Object.keys(errors).length > 0) return;
    try {
      if (editingAccount) {
        await updateBaleAccount(editingAccount.account_id, accountForm);
        setToast("اکانت به‌روزرسانی شد");
      } else {
        await createPlatformAccount("bale", accountForm);
        setToast("اکانت جدید ذخیره شد");
      }
      setAccountModalOpen(false);
      await refresh();
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function removeAccount(accountId) {
    try {
      await deleteBaleAccount(accountId);
      setToast("اکانت حذف شد");
      await refresh();
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function saveAccountGroup() {
    const errors = {};
    if (!String(accountGroupForm.name || "").trim()) {
      errors.account_group_name = "نام گروه الزامی است";
    }
    if (Number(accountGroupForm.max_concurrent) < 1) {
      errors.max_concurrent = "اجرای همزمان باید حداقل 1 باشد";
    }
    if (Number(accountGroupForm.batch_capacity) < 1) {
      errors.batch_capacity = "ظرفیت batch باید حداقل 1 باشد";
    }
    setFormErrors(errors);
    if (Object.keys(errors).length > 0) return;
    try {
      const payload = { ...accountGroupForm, platform_id: "bale" };
      if (editingAccountGroup) {
        await updateAccountGroup(editingAccountGroup.group_id, payload);
      } else {
        await createAccountGroup(payload);
      }
      const groups = await listPlatformAccountGroups("bale");
      setAccountGroups(Array.isArray(groups) ? groups : []);
      setAccountGroupModalOpen(false);
      setToast("گروه اکانت ذخیره شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function saveBulkCampaign() {
    try {
      const payload = { ...bulkCampaignForm };
      if (editingBulkCampaign) {
        await updateBulkCampaign(editingBulkCampaign.campaign_id, payload);
      } else {
        await createBulkCampaign(payload);
      }
      await reloadBulkData();
      setBulkCampaignModalOpen(false);
      setToast("کمپین ذخیره شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function saveBulkSource() {
    try {
      if (editingBulkSource) {
        await updateBulkMessageSource(editingBulkSource.message_source_id, bulkSourceForm);
      } else {
        await createBulkMessageSource(bulkSourceForm);
      }
      await reloadBulkData();
      setBulkSourceModalOpen(false);
      setToast("منبع پیام ذخیره شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function saveBulkContactList() {
    try {
      if (editingBulkContactList) {
        await updateBulkContactList(editingBulkContactList.contact_list_id, bulkContactListForm);
      } else {
        await createBulkContactList(bulkContactListForm);
      }
      await reloadBulkData();
      setBulkContactListModalOpen(false);
      setToast("لیست مخاطبین ذخیره شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function importContactsCsv() {
    if (!contactImportForm.file || !contactImportForm.name.trim()) {
      setActionError("نام لیست و فایل CSV الزامی است");
      return;
    }
    try {
      const formData = new FormData();
      formData.append("file", contactImportForm.file);
      formData.append("name", contactImportForm.name);
      formData.append("campaign_tag", contactImportForm.campaign_tag);
      formData.append("platform_id", contactImportForm.platform_id);
      formData.append("notes", contactImportForm.notes);
      const result = await importBulkContactList(formData);
      setContactImportResult(result);
      const contacts = await listBulkContacts(result.contact_list_id);
      setSampleImportedContacts(Array.isArray(contacts) ? contacts.slice(0, 10) : []);
      await reloadBulkData();
      setToast("لیست مخاطبین import شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function saveBulkRoute() {
    try {
      await createBulkCampaignRoute(routeCampaignId, bulkRouteForm);
      await reloadBulkData();
      setBulkRouteModalOpen(false);
      setToast("مسیر کمپین ذخیره شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function runBulkPlan(campaignId) {
    try {
      const result = await planBulkCampaign(campaignId);
      setBulkPlanResult(result);
      setToast("برنامه آزمایشی ساخته شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function runBulkAssignment(campaignId) {
    try {
      const payload = {
        dry_run: true,
        planned_for_date: assignmentForm.planned_for_date || today,
        max_contacts_per_account: assignmentForm.max_contacts_per_account === "" ? null : Number(assignmentForm.max_contacts_per_account),
        plan_seed: assignmentForm.plan_seed || null,
      };
      const result = await assignBulkCampaign(campaignId, payload);
      setBulkAssignmentResult(result);
      if (!assignmentForm.plan_seed && result.plan_seed) {
        setAssignmentForm((current) => ({ ...current, plan_seed: result.plan_seed }));
      }
      setToast("تقسیم‌بندی آزمایشی ساخته شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function refreshBulkQueue(campaignId) {
    if (!campaignId) return;
    const [summary, jobs] = await Promise.all([
      getBulkExecutionQueueSummary(campaignId),
      listBulkExecutionQueue(campaignId),
    ]);
    setBulkQueueResult(summary);
    setBulkQueueJobs(Array.isArray(jobs) ? jobs.slice(0, 20) : []);
  }

  async function createBulkQueue(campaignId) {
    try {
      const result = await createBulkExecutionQueue(campaignId, {
        dry_run: true,
        planned_for_date: assignmentForm.planned_for_date || today,
      });
      setBulkQueueResult(result);
      const jobs = await listBulkExecutionQueue(campaignId);
      setBulkQueueJobs(Array.isArray(jobs) ? jobs.slice(0, 20) : []);
      setToast("صف اجرای آزمایشی ساخته شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function runBulkQueueDryRun(campaignId) {
    try {
      const result = await dryRunBulkExecutionQueue(campaignId, { limit: 10 });
      setBulkQueueResult(result);
      await refreshBulkQueue(campaignId);
      setToast("اجرای آزمایشی ۱۰ job انجام شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function runBulkQueueBaleReal(campaignId) {
    try {
      const result = await runBaleExecutionQueue(campaignId, {
        dry_run: false,
        limit: Number(bulkRealRunForm.limit) || 1,
        account_id: bulkRealRunForm.account_id || null,
      });
      setBulkRealRunResult(result);
      await refreshBulkQueue(campaignId);
      setToast(result.ok ? "اجرای واقعی محدود Bale ثبت شد" : result.error_message || "اجرای واقعی محدود Bale انجام نشد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function assignProfileGroup(accountId, deviceGroupId, browserProvider = "native_chrome") {
    try {
      await assignBaleProfileGroup(accountId, {
        device_group_id: deviceGroupId,
        profile_group_id: deviceGroupId,
        browser_provider: browserProvider,
        profile_id: `profile_${accountId}`,
        adspower_profile_id: platformAccounts.find((account) => account.account_id === accountId)?.adspower_profile_id || "",
      });
      const groups = await listBaleProfileGroups();
      setProfileGroups(Array.isArray(groups) ? groups : []);
      setToast("گروه پروفایل اکانت به‌روزرسانی شد");
      await refresh();
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function saveAdsPowerSettings() {
    try {
      const saved = await saveAdsPowerConfig(adsPowerConfig);
      setAdsPowerConfig({ ...defaultAdsPowerConfig, ...saved });
      setToast("تنظیمات AdsPower ذخیره شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function testAdsPowerConnection() {
    try {
      const result = await checkAdsPowerHealth();
      setToast(result.ok ? "اتصال AdsPower برقرار است" : result.message || result.error_code || "اتصال AdsPower برقرار نشد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function saveMessageConfig() {
    try {
      const saved = await saveBaleMessageConfig(messageConfig);
      setMessageConfig((current) => ({ ...current, ...saved }));
      setToast("تنظیمات منبع ذخیره شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function runForwardTest(forceRealTest = false) {
    try {
      const result = await testBaleForward({ ...messageConfig, dry_run: forceRealTest ? false : messageConfig.dry_run });
      setPlanResult(result);
      setToast(result.ok ? "برنامه تست خشک آماده شد" : result.message || result.governance?.reason || "درخواست قابل اجرا نیست");
      await refresh();
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function runScheduleDryRun() {
    try {
      const result = await dryRunBaleSchedule({
        selected_accounts: messageConfig.account_id ? [messageConfig.account_id] : [],
        scenario_id: "forward_from_source",
        work_start: "10:00",
        work_end: "18:00",
        daily_limit: compliancePolicy.max_actions_per_account_per_day,
        hourly_limit: compliancePolicy.max_actions_per_account_per_hour,
        min_delay_seconds: compliancePolicy.min_delay_between_actions_seconds,
        max_actions_per_session: 5,
        compliance_policy: compliancePolicy,
      });
      setPlanResult(result);
      setToast("برنامه زمان‌بندی خشک ساخته شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function savePreparationConfig() {
    try {
      const saved = await saveBalePreparation(preparation);
      setPreparation({ ...defaultPreparation, ...saved, rules: { ...defaultPreparation.rules, ...(saved?.rules || {}) } });
      setToast("تنظیمات آماده‌سازی ذخیره شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  async function runPreparationDryRun() {
    try {
      const result = await dryRunBalePreparation();
      setPlanResult(result);
      setToast("بررسی خشک آماده‌سازی انجام شد");
    } catch (error) {
      setActionError(error.message);
    }
  }

  if (platform.id !== "bale") {
    return (
      <section className="rtl-page platform-workspace" dir="rtl">
        <GenericPlatformHeader platform={platform} onRun={handleRunFirstTask} onStop={handleStopFirstTask} />
        {toast ? <div className="toast">{toast}</div> : null}
        {actionError ? <div className="error-state">{actionError}</div> : null}
        {platformDataError ? <div className="error-state">Platform endpoint unavailable; using generic dashboard data.</div> : null}
        <Metrics sentToday={sentToday} totalSent={totalSent} active={platformActiveAccounts} total={platformAccounts.length} platform={platform} activeAccounts={activeAccounts} />
        <GenericAccountsTable platform={platform} accounts={platformAccounts} onOpen={handleOpenBrowser} />
        <TasksTable platform={platform} tasks={platformTasks} />
        <LogsTable platform={platform} logs={platformLogs} />
      </section>
    );
  }

  return (
    <section className="rtl-page platform-workspace" dir="rtl">
      <div className="page-header">
        <div>
          <h2 className="page-title">ارسال پیام در {platform.name}</h2>
          <p className="page-copy">کنترل سناریومحور اکانت‌ها، فوروارد از منبع، آماده‌سازی و گزارش‌های بله.</p>
        </div>
        <div className="toolbar">
          <button className="primary-button" onClick={handleRunFirstTask} type="button"><Play size={16} />شروع ارسال</button>
          <button className="danger-button" onClick={handleStopFirstTask} type="button"><Square size={16} />توقف</button>
          <button className="secondary-button" onClick={() => setActiveTab("settings")} type="button"><Settings size={16} />تنظیمات</button>
          <button className="secondary-button" onClick={() => openAccountModal()} type="button"><Plus size={16} />اکانت جدید</button>
        </div>
      </div>

      {toast ? <div className="toast">{toast}</div> : null}
      {actionError ? <div className="error-state">{actionError}</div> : null}
      {platformDataError ? <div className="error-state">Platform endpoint unavailable; using generic dashboard data.</div> : null}

      <Metrics sentToday={sentToday} totalSent={totalSent} active={platformActiveAccounts} total={platformAccounts.length} platform={platform} activeAccounts={activeAccounts} />

      <div className="tab-bar">
        {[
          ["accounts", "اکانت‌ها"],
          ["campaigns", "کمپین‌ها"],
          ["preparation", "آماده‌سازی"],
          ["schedule", "زمان‌بندی"],
          ["reports", "گزارش‌ها"],
          ["settings", "تنظیمات"],
        ].map(([id, label]) => (
          <button key={id} className={`tab-button ${activeTab === id ? "active" : ""}`} onClick={() => setActiveTab(id)} type="button">{label}</button>
        ))}
      </div>

      {activeTab === "accounts" ? (
        <BaleAccountsSection accounts={platformAccounts} onOpen={handleOpenBrowser} onEdit={openAccountModal} onDelete={removeAccount} />
      ) : null}
      {activeTab === "campaigns" ? (
        <CampaignsSection
          accounts={platformAccounts}
          quickMessageConfig={messageConfig}
          setQuickMessageConfig={setMessageConfig}
          onSaveQuickMessage={saveMessageConfig}
          onQuickDryRun={() => runForwardTest(false)}
          onQuickControlledTest={() => runForwardTest(true)}
          quickPlanResult={planResult}
          campaigns={bulkCampaigns}
          sources={bulkMessageSources}
          contactLists={bulkContactLists}
          accountGroups={accountGroups}
          planResult={bulkPlanResult}
          assignmentForm={assignmentForm}
          setAssignmentForm={setAssignmentForm}
          assignmentResult={bulkAssignmentResult}
          queueResult={bulkQueueResult}
          queueJobs={bulkQueueJobs}
          realRunResult={bulkRealRunResult}
          realRunForm={bulkRealRunForm}
          setRealRunForm={setBulkRealRunForm}
          onCreateCampaign={() => openBulkCampaignModal()}
          onEditCampaign={openBulkCampaignModal}
          onCreateSource={() => openBulkSourceModal()}
          onEditSource={openBulkSourceModal}
          onCreateContactList={() => openBulkContactListModal()}
          onEditContactList={openBulkContactListModal}
          onAddRoute={openBulkRouteModal}
          onPlan={runBulkPlan}
          onAssign={runBulkAssignment}
          onCreateQueue={createBulkQueue}
          onDryRunQueue={runBulkQueueDryRun}
          onRunBaleReal={runBulkQueueBaleReal}
          importForm={contactImportForm}
          setImportForm={setContactImportForm}
          onImportContacts={importContactsCsv}
          importResult={contactImportResult}
          sampleImportedContacts={sampleImportedContacts}
        />
      ) : null}
      {activeTab === "schedule" ? (
        <BaleScenariosSection
          scenarios={scenarios}
          compliancePolicy={compliancePolicy}
          setCompliancePolicy={setCompliancePolicy}
          onDryRunPlan={runScheduleDryRun}
          planResult={planResult}
        />
      ) : null}
      {activeTab === "preparation" ? (
        <BalePreparationSection
          accounts={platformAccounts}
          preparation={preparation}
          setPreparation={setPreparation}
          onSave={savePreparationConfig}
          onDryRun={runPreparationDryRun}
          planResult={planResult}
        />
      ) : null}
      {activeTab === "reports" ? <ReportsSection platform={platform} logs={platformLogs} /> : null}
      {activeTab === "settings" ? (
        <SettingsSection
          accountGroups={accountGroups}
          accounts={platformAccounts}
          onCreateAccountGroup={() => openAccountGroupModal()}
          onEditAccountGroup={openAccountGroupModal}
          profileGroups={profileGroups}
          onAssignProfileGroup={assignProfileGroup}
          adsPowerConfig={adsPowerConfig}
          setAdsPowerConfig={setAdsPowerConfig}
          onSaveAdsPower={saveAdsPowerSettings}
          onTestAdsPower={testAdsPowerConnection}
        />
      ) : null}

      {accountGroupModalOpen ? (
        <Modal title={editingAccountGroup ? "ویرایش گروه اکانت" : "گروه اکانت جدید"} onClose={() => setAccountGroupModalOpen(false)}>
          <div className="settings-grid">
            <Field label="نام گروه"><input value={accountGroupForm.name} onChange={(event) => setAccountGroupForm((current) => ({ ...current, name: event.target.value }))} /></Field>
            <Field label="شناسه گروه"><input value={accountGroupForm.group_id} disabled={Boolean(editingAccountGroup)} onChange={(event) => setAccountGroupForm((current) => ({ ...current, group_id: event.target.value }))} /></Field>
            {formErrors.account_group_name ? <div className="inline-error full-span">{formErrors.account_group_name}</div> : null}
            <Field label="browser provider"><select value={accountGroupForm.browser_provider} onChange={(event) => setAccountGroupForm((current) => ({ ...current, browser_provider: event.target.value }))}><option value="native_chrome">native_chrome</option><option value="adspower">adspower</option><option value="remote_worker_placeholder">remote_worker_placeholder</option></select></Field>
            <Field label="device group id"><input value={accountGroupForm.device_group_id} onChange={(event) => setAccountGroupForm((current) => ({ ...current, device_group_id: event.target.value }))} /></Field>
            <Field label="profile group id"><input value={accountGroupForm.profile_group_id} onChange={(event) => setAccountGroupForm((current) => ({ ...current, profile_group_id: event.target.value }))} /></Field>
            <Field label="اجرای همزمان"><input type="number" min="1" value={accountGroupForm.max_concurrent} onChange={(event) => setAccountGroupForm((current) => ({ ...current, max_concurrent: Number(event.target.value) }))} /></Field>
            <Field label="ظرفیت batch"><input type="number" min="1" value={accountGroupForm.batch_capacity} onChange={(event) => setAccountGroupForm((current) => ({ ...current, batch_capacity: Number(event.target.value) }))} /></Field>
            <Field label="ظرفیت روزانه"><input type="number" min="1" value={accountGroupForm.daily_capacity} onChange={(event) => setAccountGroupForm((current) => ({ ...current, daily_capacity: Number(event.target.value) }))} /></Field>
            <label className="checkbox-row"><input type="checkbox" checked={accountGroupForm.enabled} onChange={(event) => setAccountGroupForm((current) => ({ ...current, enabled: event.target.checked }))} />فعال باشد</label>
            <Field label="توضیحات"><textarea value={accountGroupForm.notes} onChange={(event) => setAccountGroupForm((current) => ({ ...current, notes: event.target.value }))} /></Field>
            {formErrors.max_concurrent ? <div className="inline-error full-span">{formErrors.max_concurrent}</div> : null}
            {formErrors.batch_capacity ? <div className="inline-error full-span">{formErrors.batch_capacity}</div> : null}
          </div>
          <div className="modal-actions">
            <button className="primary-button" onClick={saveAccountGroup} type="button"><Save size={16} />ذخیره</button>
            <button className="secondary-button" onClick={() => setAccountGroupModalOpen(false)} type="button">انصراف</button>
          </div>
        </Modal>
      ) : null}

      {bulkCampaignModalOpen ? (
        <Modal title={editingBulkCampaign ? "ویرایش کمپین" : "کمپین جدید"} onClose={() => setBulkCampaignModalOpen(false)}>
          <div className="settings-grid">
            <Field label="نام کمپین"><input value={bulkCampaignForm.name} onChange={(event) => setBulkCampaignForm((current) => ({ ...current, name: event.target.value }))} /></Field>
            <Field label="شناسه"><input value={bulkCampaignForm.campaign_id} disabled={Boolean(editingBulkCampaign)} onChange={(event) => setBulkCampaignForm((current) => ({ ...current, campaign_id: event.target.value }))} /></Field>
            <Field label="tag"><input value={bulkCampaignForm.campaign_tag} onChange={(event) => setBulkCampaignForm((current) => ({ ...current, campaign_tag: event.target.value }))} /></Field>
            <Field label="وضعیت"><select value={bulkCampaignForm.status} onChange={(event) => setBulkCampaignForm((current) => ({ ...current, status: event.target.value }))}>{["draft", "planned", "running", "paused", "completed"].map((item) => <option key={item} value={item}>{item}</option>)}</select></Field>
            <label className="checkbox-row"><input type="checkbox" checked={bulkCampaignForm.dry_run} onChange={(event) => setBulkCampaignForm((current) => ({ ...current, dry_run: event.target.checked }))} />dry-run فعال باشد</label>
            <Field label="توضیحات"><textarea value={bulkCampaignForm.notes} onChange={(event) => setBulkCampaignForm((current) => ({ ...current, notes: event.target.value }))} /></Field>
          </div>
          <div className="modal-actions"><button className="primary-button" onClick={saveBulkCampaign} type="button"><Save size={16} />ذخیره</button></div>
        </Modal>
      ) : null}

      {bulkSourceModalOpen ? (
        <Modal title={editingBulkSource ? "ویرایش منبع پیام" : "منبع پیام جدید"} onClose={() => setBulkSourceModalOpen(false)}>
          <div className="settings-grid">
            <Field label="نام"><input value={bulkSourceForm.name} onChange={(event) => setBulkSourceForm((current) => ({ ...current, name: event.target.value }))} /></Field>
            <Field label="platform"><select value={bulkSourceForm.platform_id} onChange={(event) => setBulkSourceForm((current) => ({ ...current, platform_id: event.target.value }))}>{["bale", "rubika", "eitaa", "instagram"].map((item) => <option key={item} value={item}>{item}</option>)}</select></Field>
            <Field label="tag"><input value={bulkSourceForm.campaign_tag} onChange={(event) => setBulkSourceForm((current) => ({ ...current, campaign_tag: event.target.value }))} /></Field>
            <Field label="source type"><select value={bulkSourceForm.source_type} onChange={(event) => setBulkSourceForm((current) => ({ ...current, source_type: event.target.value }))}>{["channel", "group", "chat", "link"].map((item) => <option key={item} value={item}>{item}</option>)}</select></Field>
            <Field label="source ref"><input value={bulkSourceForm.source_ref} onChange={(event) => setBulkSourceForm((current) => ({ ...current, source_ref: event.target.value }))} /></Field>
            <Field label="message ref"><select value={bulkSourceForm.message_ref_type} onChange={(event) => setBulkSourceForm((current) => ({ ...current, message_ref_type: event.target.value }))}>{["latest", "pinned", "specific"].map((item) => <option key={item} value={item}>{item}</option>)}</select></Field>
            <Field label="message ref value"><input value={bulkSourceForm.message_ref_value} onChange={(event) => setBulkSourceForm((current) => ({ ...current, message_ref_value: event.target.value }))} /></Field>
            <label className="checkbox-row"><input type="checkbox" checked={bulkSourceForm.enabled} onChange={(event) => setBulkSourceForm((current) => ({ ...current, enabled: event.target.checked }))} />فعال</label>
          </div>
          <div className="modal-actions"><button className="primary-button" onClick={saveBulkSource} type="button"><Save size={16} />ذخیره</button></div>
        </Modal>
      ) : null}

      {bulkContactListModalOpen ? (
        <Modal title={editingBulkContactList ? "ویرایش لیست مخاطبین" : "لیست مخاطبین جدید"} onClose={() => setBulkContactListModalOpen(false)}>
          <div className="settings-grid">
            <Field label="نام"><input value={bulkContactListForm.name} onChange={(event) => setBulkContactListForm((current) => ({ ...current, name: event.target.value }))} /></Field>
            <Field label="tag"><input value={bulkContactListForm.campaign_tag} onChange={(event) => setBulkContactListForm((current) => ({ ...current, campaign_tag: event.target.value }))} /></Field>
            <Field label="کل مخاطبین"><input type="number" value={bulkContactListForm.total_contacts} onChange={(event) => setBulkContactListForm((current) => ({ ...current, total_contacts: Number(event.target.value) }))} /></Field>
            <Field label="مخاطبین معتبر"><input type="number" value={bulkContactListForm.valid_contacts} onChange={(event) => setBulkContactListForm((current) => ({ ...current, valid_contacts: Number(event.target.value) }))} /></Field>
            <Field label="مخاطبین تکراری"><input type="number" value={bulkContactListForm.duplicate_contacts} onChange={(event) => setBulkContactListForm((current) => ({ ...current, duplicate_contacts: Number(event.target.value) }))} /></Field>
            <Field label="وضعیت"><select value={bulkContactListForm.status} onChange={(event) => setBulkContactListForm((current) => ({ ...current, status: event.target.value }))}>{["draft", "imported", "ready"].map((item) => <option key={item} value={item}>{item}</option>)}</select></Field>
          </div>
          <div className="modal-actions"><button className="primary-button" onClick={saveBulkContactList} type="button"><Save size={16} />ذخیره</button></div>
        </Modal>
      ) : null}

      {bulkRouteModalOpen ? (
        <Modal title="افزودن مسیر کمپین" onClose={() => setBulkRouteModalOpen(false)}>
          <div className="settings-grid">
            <Field label="platform"><select value={bulkRouteForm.platform_id} onChange={(event) => setBulkRouteForm((current) => ({ ...current, platform_id: event.target.value }))}>{["bale", "rubika", "eitaa", "instagram"].map((item) => <option key={item} value={item}>{item}</option>)}</select></Field>
            <Field label="account group"><select value={bulkRouteForm.account_group_id} onChange={(event) => setBulkRouteForm((current) => ({ ...current, account_group_id: event.target.value }))}>{accountGroups.map((group) => <option key={group.group_id} value={group.group_id}>{group.name}</option>)}</select></Field>
            <Field label="message source"><select value={bulkRouteForm.message_source_id} onChange={(event) => setBulkRouteForm((current) => ({ ...current, message_source_id: event.target.value }))}>{bulkMessageSources.map((source) => <option key={source.message_source_id} value={source.message_source_id}>{source.name}</option>)}</select></Field>
            <Field label="contact list"><select value={bulkRouteForm.contact_list_id} onChange={(event) => setBulkRouteForm((current) => ({ ...current, contact_list_id: event.target.value }))}>{bulkContactLists.map((list) => <option key={list.contact_list_id} value={list.contact_list_id}>{list.name}</option>)}</select></Field>
            <Field label="الگوی نام مخاطب"><input value={bulkRouteForm.contact_naming_pattern} onChange={(event) => setBulkRouteForm((current) => ({ ...current, contact_naming_pattern: event.target.value }))} /></Field>
            <Field label="سقف روزانه هر اکانت"><input type="number" value={bulkRouteForm.daily_limit_per_account} onChange={(event) => setBulkRouteForm((current) => ({ ...current, daily_limit_per_account: Number(event.target.value) }))} /></Field>
            <Field label="سقف ساعتی هر اکانت"><input type="number" value={bulkRouteForm.hourly_limit_per_account} onChange={(event) => setBulkRouteForm((current) => ({ ...current, hourly_limit_per_account: Number(event.target.value) }))} /></Field>
            <label className="checkbox-row"><input type="checkbox" checked={bulkRouteForm.enabled} onChange={(event) => setBulkRouteForm((current) => ({ ...current, enabled: event.target.checked }))} />فعال</label>
          </div>
          <div className="modal-actions"><button className="primary-button" onClick={saveBulkRoute} type="button"><Save size={16} />ذخیره مسیر</button></div>
        </Modal>
      ) : null}

      {accountModalOpen ? (
        <Modal title={editingAccount ? "ویرایش اکانت" : "اکانت جدید"} onClose={() => setAccountModalOpen(false)}>
          <div className="settings-grid">
            <Field label="شماره تلفن"><input value={accountForm.phone} onChange={(event) => setAccountForm((current) => ({ ...current, phone: event.target.value }))} /></Field>
            <Field label="نام / یوزرنیم"><input value={accountForm.username_or_number} onChange={(event) => setAccountForm((current) => ({ ...current, username_or_number: event.target.value }))} /></Field>
            {formErrors.identity ? <div className="inline-error full-span">{formErrors.identity}</div> : null}
            <Field label="Browser Provider"><select value={accountForm.browser_provider} onChange={(event) => setAccountForm((current) => ({ ...current, browser_provider: event.target.value }))}><option value="adspower">AdsPower</option><option value="native_chrome">Native Chrome</option></select></Field>
            <Field label="AdsPower Profile ID"><input value={accountForm.adspower_profile_id || ""} onChange={(event) => setAccountForm((current) => ({ ...current, adspower_profile_id: event.target.value }))} /></Field>
            {formErrors.adspower_profile_id ? <div className="inline-error full-span">{formErrors.adspower_profile_id}</div> : null}
            <Field label="Profile Group"><select value={accountForm.profile_group_id || accountForm.device_group_id || ""} onChange={(event) => setAccountForm((current) => ({ ...current, profile_group_id: event.target.value, device_group_id: event.target.value }))}>{profileGroups.map((group) => <option key={group.profile_group_id || group.device_group_id} value={group.profile_group_id || group.device_group_id}>{group.name}</option>)}</select></Field>
            <Field label="وضعیت"><select value={accountForm.status} onChange={(event) => setAccountForm((current) => ({ ...current, status: event.target.value }))}>{["new", "preparing", "active", "limited", "blocked", "paused", "disabled"].map((item) => <option key={item} value={item}>{item}</option>)}</select></Field>
            <Field label="سقف روزانه"><input type="number" value={accountForm.daily_limit} onChange={(event) => setAccountForm((current) => ({ ...current, daily_limit: Number(event.target.value) }))} /></Field>
            <Field label="سقف ساعتی"><input type="number" value={accountForm.hourly_limit} onChange={(event) => setAccountForm((current) => ({ ...current, hourly_limit: Number(event.target.value) }))} /></Field>
            <Field label="سلامت"><input type="number" value={accountForm.health_score} onChange={(event) => setAccountForm((current) => ({ ...current, health_score: Number(event.target.value) }))} /></Field>
            <Field label="توضیحات"><textarea value={accountForm.notes} onChange={(event) => setAccountForm((current) => ({ ...current, notes: event.target.value }))} /></Field>
          </div>
          <div className="modal-actions">
            <button className="primary-button" onClick={saveAccount} type="button"><Save size={16} />ذخیره</button>
            <button className="secondary-button" onClick={() => setAccountModalOpen(false)} type="button">انصراف</button>
          </div>
        </Modal>
      ) : null}

      {adsPowerSettingsOpen ? (
        <Modal title="تنظیمات AdsPower" onClose={() => setAdsPowerSettingsOpen(false)}>
          <div className="settings-grid">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={adsPowerConfig.enabled}
                onChange={(event) => setAdsPowerConfig((current) => ({ ...current, enabled: event.target.checked }))}
              />
              فعال / غیرفعال
            </label>
            <Field label="API Base URL">
              <input
                value={adsPowerConfig.api_base_url}
                onChange={(event) => setAdsPowerConfig((current) => ({ ...current, api_base_url: event.target.value }))}
              />
            </Field>
            <Field label="API Token / Key">
              <input
                type="password"
                value={adsPowerConfig.api_token || ""}
                onChange={(event) => setAdsPowerConfig((current) => ({ ...current, api_token: event.target.value }))}
              />
            </Field>
            <Field label="Open timeout seconds">
              <input
                type="number"
                value={adsPowerConfig.open_timeout_seconds}
                onChange={(event) => setAdsPowerConfig((current) => ({ ...current, open_timeout_seconds: Number(event.target.value) }))}
              />
            </Field>
          </div>
          <div className="modal-actions">
            <button className="primary-button" onClick={saveAdsPowerSettings} type="button"><Save size={16} />ذخیره</button>
            <button className="secondary-button" onClick={testAdsPowerConnection} type="button">تست اتصال</button>
            <button className="secondary-button" onClick={() => setAdsPowerSettingsOpen(false)} type="button">بستن</button>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}

function GenericPlatformHeader({ platform, onRun, onStop }) {
  return (
    <div className="page-header">
      <div>
        <h2 className="page-title">ارسال پیام در {platform.name}</h2>
        <p className="page-copy">کنترل ارسال مجاز، اکانت‌ها، وظایف و گزارش‌های مربوط به این پلتفرم.</p>
      </div>
      <div className="toolbar">
        <button className="primary-button" onClick={onRun} type="button"><Play size={16} />شروع ارسال</button>
        <button className="danger-button" onClick={onStop} type="button"><Square size={16} />توقف</button>
        <button className="secondary-button" type="button"><Settings size={16} />تنظیمات</button>
        <button className="secondary-button" type="button"><Plus size={16} />اکانت جدید</button>
      </div>
    </div>
  );
}

function Metrics({ sentToday, totalSent, active, total, platform, activeAccounts }) {
  return (
    <section className="grid metrics">
      <StatusCard label="پیام‌های امروز" value={sentToday} note="بر اساس گزارش‌های موجود" tone="neutral" />
      <StatusCard label="کل پیام‌های ارسال شده" value={totalSent} note="گزارش‌های با وضعیت sent" tone="success" />
      <StatusCard label="اکانت‌های فعال" value={active} note={activeAccounts ? `اکانت‌های فعال ${platform.name}` : "بدون فعالیت"} tone="warning" />
      <StatusCard label="کل اکانت‌ها" value={total} note={platform.name} tone="neutral" />
    </section>
  );
}

function GenericAccountsTable({ platform, accounts, onOpen }) {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">اکانت‌های {platform.name}</h3><span className="pill">{accounts.length} اکانت</span></div>
      <div className="table-scroll">
        <table className="table rtl-table">
          <thead><tr><th>نام اکانت / شماره</th><th>وضعیت</th><th>محدودیت روزانه</th><th>عملیات</th></tr></thead>
          <tbody>{accounts.map((account) => (
            <tr key={account.account_id}>
              <td>{account.name || account.phone || account.account_id}</td>
              <td><Pill value={account.status || "idle"} /></td>
              <td>{account.daily_limit || account.daily_message_limit || "-"}</td>
              <td><button className="icon-button" onClick={() => onOpen(account.account_id)} type="button" aria-label="باز کردن نشست"><Eye size={16} /></button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      {accounts.length === 0 ? <div className="empty-state">اکانتی برای این پلتفرم ثبت نشده است.</div> : null}
    </section>
  );
}

function BaleAccountsSection({ accounts, onOpen, onEdit, onDelete }) {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">اکانت‌ها</h3><span className="pill">{accounts.length} اکانت</span></div>
      <div className="table-scroll">
        <table className="table rtl-table wide-table">
          <thead><tr><th>شماره</th><th>Provider</th><th>AdsPower Profile ID</th><th>Profile Group</th><th>وضعیت</th><th>سلامت</th><th>عملیات</th></tr></thead>
          <tbody>{accounts.map((account) => (
            <tr key={account.account_id}>
              <td>{account.phone || account.username_or_number || account.account_id}</td>
              <td>{account.browser_provider || "-"}</td>
              <td className="truncate">{account.adspower_profile_id || "-"}</td>
              <td>{account.profile_group_id || account.device_group_id || "-"}</td>
              <td><Pill value={account.status} /></td>
              <td>{account.health_score}</td>
              <td><div className="toolbar compact-toolbar">
                <button className="icon-button" onClick={() => onOpen(account.account_id)} type="button" aria-label="باز کردن نشست"><Eye size={16} /></button>
                <button className="icon-button" onClick={() => onEdit(account)} type="button" aria-label="ویرایش"><Edit3 size={16} /></button>
                <button className="icon-button danger-icon" onClick={() => onDelete(account.account_id)} type="button" aria-label="حذف"><Trash2 size={16} /></button>
              </div></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      {accounts.length === 0 ? <div className="empty-state">اکانتی برای بله ثبت نشده است.</div> : null}
    </section>
  );
}

function CampaignsSection({
  accounts,
  quickMessageConfig,
  setQuickMessageConfig,
  onSaveQuickMessage,
  onQuickDryRun,
  onQuickControlledTest,
  quickPlanResult,
  campaigns,
  sources,
  contactLists,
  accountGroups,
  planResult,
  assignmentForm,
  setAssignmentForm,
  assignmentResult,
  queueResult,
  queueJobs,
  realRunResult,
  realRunForm,
  setRealRunForm,
  onCreateCampaign,
  onEditCampaign,
  onCreateSource,
  onEditSource,
  onCreateContactList,
  onEditContactList,
  onAddRoute,
  onPlan,
  onAssign,
  onCreateQueue,
  onDryRunQueue,
  onRunBaleReal,
  importForm,
  setImportForm,
  onImportContacts,
  importResult,
  sampleImportedContacts,
}) {
  return (
    <div>
      <BulkMessagingSection
        campaigns={campaigns}
        sources={sources}
        contactLists={contactLists}
        accountGroups={accountGroups}
        planResult={planResult}
        assignmentForm={assignmentForm}
        setAssignmentForm={setAssignmentForm}
        assignmentResult={assignmentResult}
        queueResult={queueResult}
        queueJobs={queueJobs}
        realRunResult={realRunResult}
        realRunForm={realRunForm}
        setRealRunForm={setRealRunForm}
        accounts={accounts}
        onCreateCampaign={onCreateCampaign}
        onEditCampaign={onEditCampaign}
        onCreateSource={onCreateSource}
        onEditSource={onEditSource}
        onCreateContactList={onCreateContactList}
        onEditContactList={onEditContactList}
        onAddRoute={onAddRoute}
        onPlan={onPlan}
        onAssign={onAssign}
        onCreateQueue={onCreateQueue}
        onDryRunQueue={onDryRunQueue}
        onRunBaleReal={onRunBaleReal}
        importForm={importForm}
        setImportForm={setImportForm}
        onImportContacts={onImportContacts}
        importResult={importResult}
        sampleImportedContacts={sampleImportedContacts}
      />
      <BaleSendSection
        accounts={accounts}
        config={quickMessageConfig}
        setConfig={setQuickMessageConfig}
        onSave={onSaveQuickMessage}
        onDryRun={onQuickDryRun}
        onControlledTest={onQuickControlledTest}
        planResult={quickPlanResult}
      />
    </div>
  );
}

function BaleSendSection({ accounts, config, setConfig, onSave, onDryRun, onControlledTest, planResult }) {
  const update = (key, value) => setConfig((current) => ({ ...current, [key]: value }));
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">پیام متنی سریع</h3><span className="pill">فوروارد از منبع</span></div>
      <p className="page-copy">روش اصلی ارسال، فوروارد پیام آماده از منبع است تا از آپلود تکراری عکس و ویدیو جلوگیری شود.</p>
      <div className="settings-grid" style={{ marginTop: 14 }}>
        <Field label="انتخاب اکانت"><select value={config.account_id} onChange={(event) => update("account_id", event.target.value)}><option value="">انتخاب اکانت</option>{accounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.phone || account.account_id}</option>)}</select></Field>
        <Field label="نوع منبع"><select value={config.source_type} onChange={(event) => update("source_type", event.target.value)}>{["group", "channel", "chat", "message_link"].map((item) => <option key={item} value={item}>{item}</option>)}</select></Field>
        <Field label="آدرس/شناسه منبع"><input value={config.source_value} onChange={(event) => update("source_value", event.target.value)} /></Field>
        <Field label="مقصد تست"><input value={config.target} onChange={(event) => update("target", event.target.value)} /></Field>
        <Field label="توضیح پیام منبع"><textarea value={config.source_message_hint} onChange={(event) => update("source_message_hint", event.target.value)} /></Field>
        <Field label="توضیح"><textarea value={config.description} onChange={(event) => update("description", event.target.value)} /></Field>
        <label className="checkbox-row"><input type="checkbox" checked={config.dry_run} onChange={(event) => update("dry_run", event.target.checked)} />dry_run فعال باشد</label>
      </div>
      <div className="modal-actions">
        <button className="secondary-button" onClick={onSave} type="button"><Save size={16} />ذخیره تنظیمات</button>
        <button className="primary-button" onClick={onDryRun} type="button">تست خشک</button>
        <button className="secondary-button" onClick={onControlledTest} type="button">تست فوروارد کنترل‌شده</button>
      </div>
      <PlanPreview result={planResult} />
    </section>
  );
}

function BulkMessagingSection({ campaigns, sources, contactLists, accountGroups, planResult, assignmentForm, setAssignmentForm, assignmentResult, queueResult, queueJobs, realRunResult, realRunForm, setRealRunForm, accounts, onCreateCampaign, onEditCampaign, onCreateSource, onEditSource, onCreateContactList, onEditContactList, onAddRoute, onPlan, onAssign, onCreateQueue, onDryRunQueue, onRunBaleReal, importForm, setImportForm, onImportContacts, importResult, sampleImportedContacts }) {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">پیام انبوه</h3><span className="pill">dry-run</span></div>

      <section className="safe-policy-section">
        <div className="panel-header"><h3 className="panel-title">کمپین‌ها</h3><button className="secondary-button" onClick={onCreateCampaign} type="button"><Plus size={16} />کمپین جدید</button></div>
        <div className="table-scroll">
          <table className="table rtl-table wide-table">
            <thead><tr><th>نام</th><th>tag</th><th>وضعیت</th><th>dry-run</th><th>مسیرها</th><th>عملیات</th></tr></thead>
            <tbody>{campaigns.map((campaign) => (
              <tr key={campaign.campaign_id}>
                <td>{campaign.name}</td>
                <td>{campaign.campaign_tag || "-"}</td>
                <td><Pill value={campaign.status} /></td>
                <td>{String(campaign.dry_run)}</td>
                <td>{campaign.routes?.length || 0}</td>
                <td><div className="toolbar compact-toolbar">
                  <button className="icon-button" onClick={() => onEditCampaign(campaign)} type="button" aria-label="ویرایش"><Edit3 size={16} /></button>
                  <button className="secondary-button" onClick={() => onAddRoute(campaign.campaign_id)} type="button">افزودن مسیر</button>
                  <button className="primary-button" onClick={() => onPlan(campaign.campaign_id)} type="button">ساخت برنامه آزمایشی</button>
                  <button className="secondary-button" onClick={() => onAssign(campaign.campaign_id)} type="button">ساخت تقسیم‌بندی آزمایشی</button>
                </div></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        {campaigns.length === 0 ? <div className="empty-state">کمپینی ثبت نشده است.</div> : null}
      </section>

      <section className="safe-policy-section">
        <div className="panel-header"><h3 className="panel-title">منابع پیام</h3><button className="secondary-button" onClick={onCreateSource} type="button"><Plus size={16} />منبع جدید</button></div>
        <div className="table-scroll">
          <table className="table rtl-table wide-table">
            <thead><tr><th>نام</th><th>platform</th><th>tag</th><th>type</th><th>ref</th><th>فعال</th><th>عملیات</th></tr></thead>
            <tbody>{sources.map((source) => (
              <tr key={source.message_source_id}>
                <td>{source.name}</td>
                <td>{source.platform_id}</td>
                <td>{source.campaign_tag || "-"}</td>
                <td>{source.source_type}</td>
                <td className="truncate">{source.source_ref || "-"}</td>
                <td>{String(source.enabled)}</td>
                <td><button className="icon-button" onClick={() => onEditSource(source)} type="button" aria-label="ویرایش"><Edit3 size={16} /></button></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </section>

      <section className="safe-policy-section">
        <div className="panel-header"><h3 className="panel-title">لیست مخاطبین</h3><button className="secondary-button" onClick={onCreateContactList} type="button"><Plus size={16} />لیست جدید</button></div>
        <div className="safe-policy-section">
          <div className="panel-header"><h4 className="panel-title">آپلود لیست مخاطبین</h4><span className="pill">CSV</span></div>
          <div className="settings-grid">
            <Field label="نام لیست">
              <input value={importForm.name} onChange={(event) => setImportForm((current) => ({ ...current, name: event.target.value }))} />
            </Field>
            <Field label="تگ کمپین">
              <input value={importForm.campaign_tag} placeholder="GHAB" onChange={(event) => setImportForm((current) => ({ ...current, campaign_tag: event.target.value }))} />
            </Field>
            <Field label="platform">
              <select value={importForm.platform_id} onChange={(event) => setImportForm((current) => ({ ...current, platform_id: event.target.value }))}>
                <option value="">عمومی</option>
                <option value="bale">bale</option>
                <option value="rubika">rubika</option>
                <option value="eitaa">eitaa</option>
                <option value="instagram">instagram</option>
              </select>
            </Field>
            <Field label="فایل CSV">
              <input type="file" accept=".csv,text/csv" onChange={(event) => setImportForm((current) => ({ ...current, file: event.target.files?.[0] || null }))} />
            </Field>
            <Field label="توضیحات">
              <textarea value={importForm.notes} onChange={(event) => setImportForm((current) => ({ ...current, notes: event.target.value }))} />
            </Field>
          </div>
          <div className="modal-actions">
            <button className="primary-button" onClick={onImportContacts} type="button">شروع import</button>
          </div>
          {importResult ? (
            <div className="plan-preview">
              <div className="panel-header"><h4 className="panel-title">خلاصه import</h4><span className="pill">{importResult.status}</span></div>
              <section className="grid metrics">
                <div><span>کل ردیف‌ها</span><strong>{importResult.total_rows || 0}</strong></div>
                <div><span>مخاطبین معتبر</span><strong>{importResult.valid_contacts || 0}</strong></div>
                <div><span>شماره‌های نامعتبر</span><strong>{importResult.invalid_contacts || 0}</strong></div>
                <div><span>شماره‌های تکراری</span><strong>{importResult.duplicate_contacts || 0}</strong></div>
                <div><span>وضعیت</span><strong>{importResult.status || "-"}</strong></div>
              </section>
            </div>
          ) : null}
          {sampleImportedContacts.length ? (
            <div className="table-scroll">
              <h4 className="panel-title">نمونه مخاطبین</h4>
              <table className="table rtl-table wide-table">
                <thead><tr><th>raw_phone</th><th>normalized_phone</th><th>نام</th><th>وضعیت</th></tr></thead>
                <tbody>{sampleImportedContacts.map((contact) => (
                  <tr key={contact.contact_id}>
                    <td>{contact.raw_phone || "-"}</td>
                    <td>{contact.normalized_phone || "-"}</td>
                    <td>{contact.full_name || "-"}</td>
                    <td><Pill value={contact.status} /></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : null}
        </div>
        <div className="table-scroll">
          <table className="table rtl-table wide-table">
            <thead><tr><th>نام</th><th>tag</th><th>کل</th><th>معتبر</th><th>تکراری</th><th>وضعیت</th><th>عملیات</th></tr></thead>
            <tbody>{contactLists.map((list) => (
              <tr key={list.contact_list_id}>
                <td>{list.name}</td>
                <td>{list.campaign_tag || "-"}</td>
                <td>{list.total_contacts}</td>
                <td>{list.valid_contacts}</td>
                <td>{list.duplicate_contacts}</td>
                <td><Pill value={list.status} /></td>
                <td><button className="icon-button" onClick={() => onEditContactList(list)} type="button" aria-label="ویرایش"><Edit3 size={16} /></button></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </section>

      <BulkCapacitySummary result={planResult} />
      <BulkAssignmentPlannerSection form={assignmentForm} setForm={setAssignmentForm} result={assignmentResult} onAssign={onAssign} campaigns={campaigns} />
      <BulkExecutionQueueSection form={assignmentForm} campaigns={campaigns} result={queueResult} jobs={queueJobs} realRunResult={realRunResult} realRunForm={realRunForm} setRealRunForm={setRealRunForm} accounts={accounts} onCreateQueue={onCreateQueue} onDryRunQueue={onDryRunQueue} onRunBaleReal={onRunBaleReal} />
      <div className="empty-state" style={{ marginTop: 12 }}>الگوی نام مخاطب: {"Bale-GHAB-{seq:06d} -> Bale-GHAB-000001"}</div>
    </section>
  );
}

function BulkCapacitySummary({ result }) {
  if (!result) return null;
  const summaries = result.route_summaries || [];
  return (
    <div className="plan-preview">
      <div className="panel-header"><h4 className="panel-title">خلاصه ظرفیت</h4><span className="pill">ظرفیت کل {result.total_planned || 0}</span></div>
      <div className="table-scroll">
        <table className="table rtl-table wide-table">
          <thead><tr><th>مسیر</th><th>platform</th><th>اکانت‌ها</th><th>ظرفیت مسیر</th><th>مخاطبین معتبر</th><th>planned</th><th>باقی‌مانده</th><th>هشدارها</th></tr></thead>
          <tbody>{summaries.map((summary) => (
            <tr key={summary.route_id}>
              <td>{summary.route_id}</td>
              <td>{summary.platform_id}</td>
              <td>{summary.available_accounts}</td>
              <td>{summary.route_capacity}</td>
              <td>{summary.valid_contacts}</td>
              <td>{summary.planned_count}</td>
              <td>{summary.remaining_contacts}</td>
              <td className="truncate">{(summary.warnings || []).join(", ") || "-"}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}

function BulkAssignmentPlannerSection({ form, setForm, result, onAssign, campaigns }) {
  const selectedCampaignId = form.campaign_id || campaigns[0]?.campaign_id || "";
  const summaries = result?.route_summaries || [];
  const sampleAssignments = summaries.flatMap((summary) => summary.sample_assignments || []).slice(0, 20);
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  return (
    <section className="safe-policy-section">
      <div className="panel-header"><h3 className="panel-title">تقسیم مخاطبین بین اکانت‌ها</h3><span className="pill">dry-run</span></div>
      <div className="settings-grid">
        <Field label="کمپین">
          <select value={selectedCampaignId} onChange={(event) => update("campaign_id", event.target.value)}>
            {campaigns.map((campaign) => <option key={campaign.campaign_id} value={campaign.campaign_id}>{campaign.name}</option>)}
          </select>
        </Field>
        <Field label="تاریخ برنامه">
          <input type="date" value={form.planned_for_date} onChange={(event) => update("planned_for_date", event.target.value)} />
        </Field>
        <Field label="حداکثر مخاطب برای هر اکانت">
          <input type="number" min="1" value={form.max_contacts_per_account} onChange={(event) => update("max_contacts_per_account", event.target.value)} />
        </Field>
        <Field label="plan_seed">
          <input value={form.plan_seed} onChange={(event) => update("plan_seed", event.target.value)} />
        </Field>
      </div>
      <div className="modal-actions">
        <button className="primary-button" onClick={() => selectedCampaignId && onAssign(selectedCampaignId)} type="button">ساخت تقسیم‌بندی آزمایشی</button>
      </div>
      {result ? (
        <div className="plan-preview">
          <div className="panel-header"><h4 className="panel-title">خلاصه تقسیم‌بندی</h4><span className="pill">seed {result.plan_seed}</span></div>
          <section className="grid metrics">
            <div><span>تعداد تخصیص‌ها</span><strong>{result.total_assignments || 0}</strong></div>
            <div><span>مخاطبین باقی‌مانده</span><strong>{result.total_remaining_contacts || 0}</strong></div>
            <div><span>تاریخ</span><strong>{result.planned_for_date || "-"}</strong></div>
          </section>
          <div className="table-scroll">
            <table className="table rtl-table wide-table">
              <thead><tr><th>مسیر</th><th>پلتفرم</th><th>اکانت‌ها</th><th>مخاطبین معتبر</th><th>تعداد تخصیص‌ها</th><th>مخاطبین باقی‌مانده</th><th>سقف روزانه هر اکانت</th><th>سقف ساعتی هر اکانت</th><th>محدودیت موثر</th><th>هشدارها</th></tr></thead>
              <tbody>{summaries.map((summary) => (
                <tr key={summary.route_id}>
                  <td>{summary.route_id}</td>
                  <td>{summary.platform_id}</td>
                  <td>{summary.available_accounts}</td>
                  <td>{summary.valid_contacts}</td>
                  <td>{summary.assigned_contacts}</td>
                  <td>{summary.remaining_contacts}</td>
                  <td>{summary.effective_daily_limit_per_account}</td>
                  <td>{summary.effective_hourly_limit_per_account}</td>
                  <td>{summary.route_capacity}</td>
                  <td>{(summary.warnings || []).join(", ") || "-"}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          {sampleAssignments.length ? (
            <div className="table-scroll">
              <h4 className="panel-title">نمونه تخصیص‌ها</h4>
              <table className="table rtl-table wide-table">
                <thead><tr><th>اکانت</th><th>شماره نرمال‌شده</th><th>نام ذخیره مخاطب</th><th>مسیر</th><th>پلتفرم</th></tr></thead>
                <tbody>{sampleAssignments.map((item) => (
                  <tr key={`${item.route_id}-${item.account_id}-${item.contact_naming_value}`}>
                    <td>{item.account_id}</td>
                    <td>{item.normalized_phone}</td>
                    <td>{item.contact_naming_value}</td>
                    <td>{item.route_id}</td>
                    <td>{item.platform_id}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function BulkExecutionQueueSection({ form, campaigns, result, jobs, realRunResult, realRunForm, setRealRunForm, accounts, onCreateQueue, onDryRunQueue, onRunBaleReal }) {
  const selectedCampaignId = form.campaign_id || campaigns[0]?.campaign_id || "";
  const summary = result?.status_summary || {};
  const sampleJobs = Array.isArray(jobs) && jobs.length ? jobs.slice(0, 20) : result?.sample_jobs || [];
  const realSummary = realRunResult?.status_summary || summary;
  const updateRealRun = (key, value) => setRealRunForm((current) => ({ ...current, [key]: value }));
  return (
    <section className="safe-policy-section">
      <div className="panel-header"><h3 className="panel-title">صف اجرای کمپین</h3><span className="pill">dry-run</span></div>
      <div className="modal-actions">
        <button className="primary-button" onClick={() => selectedCampaignId && onCreateQueue(selectedCampaignId)} type="button">ساخت صف اجرای آزمایشی</button>
        <button className="secondary-button" onClick={() => selectedCampaignId && onDryRunQueue(selectedCampaignId)} type="button">اجرای آزمایشی ۱۰ job</button>
      </div>
      {result ? (
        <div className="plan-preview">
          <div className="panel-header"><h4 className="panel-title">خلاصه صف</h4><span className="pill">تعداد jobهای ساخته‌شده {result.total_jobs ?? result.created_jobs ?? 0}</span></div>
          <section className="grid metrics">
            <div><span>jobهای در انتظار</span><strong>{summary.pending || 0}</strong></div>
            <div><span>انجام‌شده</span><strong>{summary.completed || 0}</strong></div>
            <div><span>خطاها</span><strong>{summary.failed || 0}</strong></div>
            <div><span>ردشده</span><strong>{summary.skipped || 0}</strong></div>
            <div><span>لغوشده</span><strong>{summary.cancelled || 0}</strong></div>
          </section>
          {sampleJobs.length ? (
            <div className="table-scroll">
              <h4 className="panel-title">نمونه jobها</h4>
              <table className="table rtl-table wide-table">
                <thead><tr><th>job</th><th>اکانت</th><th>شماره نرمال‌شده</th><th>نام مخاطب</th><th>وضعیت</th></tr></thead>
                <tbody>{sampleJobs.map((job) => (
                  <tr key={job.job_id}>
                    <td>{job.job_id}</td>
                    <td>{job.account_id}</td>
                    <td>{job.normalized_phone}</td>
                    <td>{job.contact_naming_value}</td>
                    <td><Pill value={job.status} /></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}
      <div className="plan-preview">
        <div className="panel-header"><h4 className="panel-title">اجرای واقعی محدود Bale</h4><span className="pill">فقط برای تست محدود</span></div>
        <div className="empty-state">ارسال انبوه نیست. حداکثر ۳ job در هر اجرا.</div>
        <div className="settings-grid" style={{ marginTop: 12 }}>
          <Field label="limit">
            <input type="number" min="1" max="3" value={realRunForm.limit} onChange={(event) => updateRealRun("limit", event.target.value)} />
          </Field>
          <Field label="account_id">
            <input list="bale-real-run-accounts" value={realRunForm.account_id} placeholder="اختیاری" onChange={(event) => updateRealRun("account_id", event.target.value)} />
            <datalist id="bale-real-run-accounts">
              {(accounts || []).map((account) => <option key={account.account_id} value={account.account_id}>{account.phone || account.username_or_number || account.account_id}</option>)}
            </datalist>
          </Field>
        </div>
        <div className="modal-actions">
          <button className="danger-button" onClick={() => selectedCampaignId && onRunBaleReal(selectedCampaignId)} type="button">اجرای واقعی ۱ تا ۳ job Bale</button>
        </div>
        {realRunResult ? (
          <>
            <section className="grid metrics">
              <div><span>processed_jobs</span><strong>{realRunResult.processed_jobs || 0}</strong></div>
              <div><span>completed_jobs</span><strong>{realRunResult.completed_jobs || 0}</strong></div>
              <div><span>failed_jobs</span><strong>{realRunResult.failed_jobs || 0}</strong></div>
              <div><span>jobهای در انتظار</span><strong>{realSummary.pending || 0}</strong></div>
              <div><span>انجام‌شده</span><strong>{realSummary.completed || 0}</strong></div>
              <div><span>خطاها</span><strong>{realSummary.failed || 0}</strong></div>
            </section>
            {(realRunResult.sample_results || []).length ? (
              <div className="table-scroll">
                <h4 className="panel-title">sample_results</h4>
                <table className="table rtl-table wide-table">
                  <thead><tr><th>job</th><th>اکانت</th><th>شماره نرمال‌شده</th><th>نام مخاطب</th><th>وضعیت</th></tr></thead>
                  <tbody>{realRunResult.sample_results.map((job) => (
                    <tr key={job.job_id}>
                      <td>{job.job_id}</td>
                      <td>{job.account_id}</td>
                      <td>{job.normalized_phone}</td>
                      <td>{job.contact_naming_value}</td>
                      <td><Pill value={job.status} /></td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </section>
  );
}

function BaleScenariosSection({ scenarios, compliancePolicy, setCompliancePolicy, onDryRunPlan, planResult }) {
  const updatePolicy = (key, value) => setCompliancePolicy((current) => ({ ...current, [key]: value }));
  const updateQuietHours = (key, value) =>
    setCompliancePolicy((current) => ({
      ...current,
      quiet_hours: { ...current.quiet_hours, [key]: value },
    }));
  const updateRandomization = (key, value) =>
    setCompliancePolicy((current) => ({
      ...current,
      randomization: { ...current.randomization, [key]: value },
    }));

  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <section className="safe-policy-section">
        <div className="panel-header"><h3 className="panel-title">سیاست اجرای امن</h3><span className="pill">{compliancePolicy.mode}</span></div>
        <p className="page-copy">این بخش برای اجرای کنترل‌شده و جلوگیری از فشار ناگهانی روی اکانت‌هاست. سیستم برای دور زدن قوانین پیام‌رسان طراحی نشده است.</p>
        <div className="settings-grid" style={{ marginTop: 14 }}>
          <Field label="حالت اجرا">
            <select value={compliancePolicy.mode} onChange={(event) => updatePolicy("mode", event.target.value)}>
              <option value="conservative">محافظه‌کار</option>
              <option value="normal">معمولی</option>
            </select>
          </Field>
          <Field label="سقف ساعتی هر اکانت">
            <input type="number" min="0" value={compliancePolicy.max_actions_per_account_per_hour} onChange={(event) => updatePolicy("max_actions_per_account_per_hour", Number(event.target.value))} />
          </Field>
          <Field label="سقف روزانه هر اکانت">
            <input type="number" min="0" value={compliancePolicy.max_actions_per_account_per_day} onChange={(event) => updatePolicy("max_actions_per_account_per_day", Number(event.target.value))} />
          </Field>
          <Field label="حداقل فاصله بین عملیات">
            <input type="number" min="0" value={compliancePolicy.min_delay_between_actions_seconds} onChange={(event) => updatePolicy("min_delay_between_actions_seconds", Number(event.target.value))} />
          </Field>
          <label className="checkbox-row"><input type="checkbox" checked={compliancePolicy.stop_on_error} onChange={(event) => updatePolicy("stop_on_error", event.target.checked)} />توقف روی خطا</label>
          <label className="checkbox-row"><input type="checkbox" checked={compliancePolicy.stop_on_rate_limit_warning && compliancePolicy.stop_on_block_or_limit_detected} onChange={(event) => {
            updatePolicy("stop_on_rate_limit_warning", event.target.checked);
            updatePolicy("stop_on_block_or_limit_detected", event.target.checked);
          }} />توقف روی محدودیت/هشدار</label>
          <label className="checkbox-row"><input type="checkbox" checked={compliancePolicy.quiet_hours.enabled} onChange={(event) => updateQuietHours("enabled", event.target.checked)} />ساعات غیرفعال</label>
          <div className="quiet-hours-row">
            <Field label="شروع"><input type="time" value={compliancePolicy.quiet_hours.start} onChange={(event) => updateQuietHours("start", event.target.value)} /></Field>
            <Field label="پایان"><input type="time" value={compliancePolicy.quiet_hours.end} onChange={(event) => updateQuietHours("end", event.target.value)} /></Field>
          </div>
          <label className="checkbox-row"><input type="checkbox" checked={compliancePolicy.dry_run_default} onChange={(event) => updatePolicy("dry_run_default", event.target.checked)} />حالت تست خشک فعال باشد</label>
        </div>
      </section>
      <section className="safe-policy-section">
        <div className="panel-header"><h3 className="panel-title">تنوع زمان‌بندی روزانه</h3><span className="pill">{compliancePolicy.randomization.enabled ? "فعال" : "غیرفعال"}</span></div>
        <p className="page-copy">این گزینه برای پخش طبیعی‌تر حجم کار در طول روز و جلوگیری از اجرای خشک و پشت‌سرهم است. همه زمان‌ها همچنان داخل محدودیت‌های امن و سقف‌های تعریف‌شده باقی می‌مانند.</p>
        <div className="settings-grid" style={{ marginTop: 14 }}>
          <label className="checkbox-row"><input type="checkbox" checked={compliancePolicy.randomization.enabled} onChange={(event) => updateRandomization("enabled", event.target.checked)} />فعال/غیرفعال</label>
          <label className="checkbox-row"><input type="checkbox" checked={compliancePolicy.randomization.shuffle_batch_order} onChange={(event) => updateRandomization("shuffle_batch_order", event.target.checked)} />جابه‌جایی تصادفی زمان‌ها</label>
          <Field label="حداقل تغییر زمان">
            <input type="number" min="0" value={compliancePolicy.randomization.jitter_minutes_min} onChange={(event) => updateRandomization("jitter_minutes_min", Number(event.target.value))} />
          </Field>
          <Field label="حداکثر تغییر زمان">
            <input type="number" min="0" value={compliancePolicy.randomization.jitter_minutes_max} onChange={(event) => updateRandomization("jitter_minutes_max", Number(event.target.value))} />
          </Field>
          <label className="checkbox-row"><input type="checkbox" checked={compliancePolicy.randomization.avoid_same_time_as_previous_day} onChange={(event) => updateRandomization("avoid_same_time_as_previous_day", event.target.checked)} />جلوگیری از تکرار زمان دیروز</label>
          <label className="checkbox-row"><input type="checkbox" checked={compliancePolicy.randomization.avoid_same_account_order_as_previous_day} onChange={(event) => updateRandomization("avoid_same_account_order_as_previous_day", event.target.checked)} />جلوگیری از ترتیب تکراری اکانت‌ها</label>
        </div>
      </section>
      <div className="panel-header"><h3 className="panel-title">سناریوها</h3><button className="secondary-button" onClick={onDryRunPlan} type="button">dry-run plan</button></div>
      <div className="table-scroll">
        <table className="table rtl-table wide-table">
          <thead><tr><th>نام</th><th>نوع</th><th>فعال</th><th>dry_run</th><th>محدودیت‌ها</th><th>پرچم‌های ایمنی</th></tr></thead>
          <tbody>{scenarios.map((scenario) => (
            <tr key={scenario.scenario_id}>
              <td>{scenario.name}</td>
              <td>{scenario.type}</td>
              <td>{String(scenario.enabled)}</td>
              <td>{String(scenario.dry_run)}</td>
              <td className="truncate">{JSON.stringify(scenario.limits || {})}</td>
              <td className="truncate">{JSON.stringify(scenario.safety || {})}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      {scenarios.length === 0 ? <div className="empty-state">سناریویی ثبت نشده است.</div> : null}
      <GroupSummary result={planResult} />
      <PlanPreview result={planResult} />
    </section>
  );
}

function SettingsSection({
  accountGroups,
  accounts,
  onCreateAccountGroup,
  onEditAccountGroup,
  profileGroups,
  onAssignProfileGroup,
  adsPowerConfig,
  setAdsPowerConfig,
  onSaveAdsPower,
  onTestAdsPower,
}) {
  return (
    <div>
      <BaleAccountGroupsSection groups={accountGroups} accounts={accounts} onCreate={onCreateAccountGroup} onEdit={onEditAccountGroup} />
      <BaleProfileGroupsSection groups={profileGroups} accounts={accounts} onAssign={onAssignProfileGroup} />
      <AdsPowerSettingsPanel
        config={adsPowerConfig}
        setConfig={setAdsPowerConfig}
        onSave={onSaveAdsPower}
        onTest={onTestAdsPower}
      />
    </div>
  );
}

function BaleAccountGroupsSection({ groups, accounts, onCreate, onEdit }) {
  const accountCounts = groups.reduce((acc, group) => {
    acc[group.group_id] = accounts.filter((account) => (account.account_group_id || "bale_test_group") === group.group_id).length;
    return acc;
  }, {});
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">گروه‌های اکانت</h3><button className="secondary-button" onClick={onCreate} type="button"><Plus size={16} />گروه جدید</button></div>
      <div className="table-scroll">
        <table className="table rtl-table wide-table">
          <thead><tr><th>نام گروه</th><th>provider</th><th>ظرفیت batch</th><th>اجرای همزمان</th><th>فعال/غیرفعال</th><th>تعداد اکانت‌ها</th><th>توضیحات</th><th>عملیات</th></tr></thead>
          <tbody>{groups.map((group) => (
            <tr key={group.group_id}>
              <td>{group.name}</td>
              <td>{group.browser_provider}</td>
              <td>{group.batch_capacity}</td>
              <td>{group.max_concurrent}</td>
              <td><Pill value={group.enabled ? "active" : "disabled"} /></td>
              <td>{accountCounts[group.group_id] || 0}</td>
              <td className="truncate">{group.notes || "-"}</td>
              <td><button className="icon-button" onClick={() => onEdit(group)} type="button" aria-label="ویرایش"><Edit3 size={16} /></button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      {groups.length === 0 ? <div className="empty-state">گروه اکانتی ثبت نشده است.</div> : null}
    </section>
  );
}

function GroupSummary({ result }) {
  const summary = result?.group_summary || [];
  if (!summary.length) return null;
  return (
    <div className="plan-preview">
      <div className="panel-header"><h4 className="panel-title">خلاصه گروه‌ها</h4><span className="pill">{summary.length} گروه</span></div>
      <div className="table-scroll">
        <table className="table rtl-table">
          <thead><tr><th>گروه</th><th>planned jobs</th><th>skipped</th><th>max concurrent</th><th>batch</th><th>provider</th></tr></thead>
          <tbody>{summary.map((group) => (
            <tr key={group.group_id}>
              <td>{group.name}</td>
              <td>{group.planned_jobs}</td>
              <td>{group.skipped_accounts}</td>
              <td>{group.max_concurrent}</td>
              <td>{group.batch_capacity}</td>
              <td>{group.browser_provider}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}

function BaleProfileGroupsSection({ groups, accounts, onAssign }) {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header">
        <h3 className="panel-title">گروه پروفایل‌ها</h3>
        <span className="pill">{groups.length} گروه</span>
      </div>
      <div className="table-scroll">
        <table className="table rtl-table wide-table">
          <thead>
            <tr>
              <th>نام گروه</th>
              <th>شناسه</th>
              <th>provider</th>
              <th>تعداد اکانت</th>
              <th>max concurrent</th>
              <th>اکانت‌های متصل</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <tr key={group.profile_group_id || group.device_group_id}>
                <td>{group.name}</td>
                <td>{group.profile_group_id || group.device_group_id}</td>
                <td>{group.browser_provider}</td>
                <td>{group.account_ids?.length || 0} / {group.max_accounts || group.group_size_limit}</td>
                <td>{group.max_concurrent_accounts}</td>
                <td className="truncate">{(group.account_ids || []).join(", ") || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {groups.length === 0 ? <div className="empty-state">گروه پروفایلی ثبت نشده است.</div> : null}

      <div className="settings-grid" style={{ marginTop: 14 }}>
        {accounts.map((account) => (
          <Field key={account.account_id} label={account.phone || account.account_id}>
            <select
              value={account.profile_group_id || account.device_group_id || ""}
              onChange={(event) => {
                const group = groups.find((item) => (item.profile_group_id || item.device_group_id) === event.target.value);
                onAssign(account.account_id, event.target.value, group?.browser_provider || account.browser_provider || "native_chrome");
              }}
            >
              <option value="">انتخاب گروه</option>
              {groups.map((group) => (
                <option key={group.profile_group_id || group.device_group_id} value={group.profile_group_id || group.device_group_id}>
                  {group.name} - {group.browser_provider}
                </option>
              ))}
            </select>
          </Field>
        ))}
      </div>
    </section>
  );
}

function AdsPowerSettingsPanel({ config, setConfig, onSave, onTest }) {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">تنظیمات Provider / AdsPower</h3><span className="pill">{config.enabled ? "active" : "disabled"}</span></div>
      <div className="settings-grid">
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={config.enabled}
            onChange={(event) => setConfig((current) => ({ ...current, enabled: event.target.checked }))}
          />
          فعال / غیرفعال
        </label>
        <Field label="API Base URL">
          <input
            value={config.api_base_url}
            onChange={(event) => setConfig((current) => ({ ...current, api_base_url: event.target.value }))}
          />
        </Field>
        <Field label="API Token / Key">
          <input
            value={config.api_token}
            onChange={(event) => setConfig((current) => ({ ...current, api_token: event.target.value }))}
          />
        </Field>
        <Field label="Open timeout seconds">
          <input
            type="number"
            value={config.open_timeout_seconds}
            onChange={(event) => setConfig((current) => ({ ...current, open_timeout_seconds: Number(event.target.value) }))}
          />
        </Field>
      </div>
      <div className="modal-actions">
        <button className="primary-button" onClick={onSave} type="button"><Save size={16} />ذخیره</button>
        <button className="secondary-button" onClick={onTest} type="button">تست اتصال</button>
      </div>
    </section>
  );
}

function BalePreparationSection({ accounts, preparation, setPreparation, onSave, onDryRun, planResult }) {
  const selected = new Set(preparation.selected_accounts || []);
  function toggleAccount(accountId) {
    const next = new Set(selected);
    if (next.has(accountId)) next.delete(accountId); else next.add(accountId);
    setPreparation((current) => ({ ...current, selected_accounts: Array.from(next) }));
  }
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">آماده‌سازی و سلامت اکانت</h3><span className="pill">{preparation.mode}</span></div>
      <p className="page-copy">این بخش برای بررسی سلامت و آماده‌سازی کنترل‌شده اکانت‌های متعلق به خودتان است و برای دور زدن قوانین پیام‌رسان طراحی نشده است.</p>
      <div className="settings-grid" style={{ marginTop: 14 }}>
        <label className="checkbox-row"><input type="checkbox" checked={preparation.enabled} onChange={(event) => setPreparation((current) => ({ ...current, enabled: event.target.checked }))} />فعال</label>
        <Field label="mode"><input readOnly value={preparation.mode} /></Field>
        <Field label="max test messages per day"><input type="number" value={preparation.rules.max_test_messages_per_account_per_day} onChange={(event) => setPreparation((current) => ({ ...current, rules: { ...current.rules, max_test_messages_per_account_per_day: Number(event.target.value) } }))} /></Field>
        <Field label="min delay"><input type="number" value={preparation.rules.min_delay_seconds} onChange={(event) => setPreparation((current) => ({ ...current, rules: { ...current.rules, min_delay_seconds: Number(event.target.value) } }))} /></Field>
        <label className="checkbox-row"><input type="checkbox" checked={preparation.rules.manual_approval_required} onChange={(event) => setPreparation((current) => ({ ...current, rules: { ...current.rules, manual_approval_required: event.target.checked } }))} />manual approval required</label>
        <div className="full-span checklist">
          {accounts.map((account) => <label key={account.account_id}><input type="checkbox" checked={selected.has(account.account_id)} onChange={() => toggleAccount(account.account_id)} />{account.phone || account.account_id} - {account.section}</label>)}
        </div>
      </div>
      <div className="modal-actions">
        <button className="secondary-button" onClick={onSave} type="button"><Save size={16} />ذخیره</button>
        <button className="primary-button" onClick={onDryRun} type="button">dry-run</button>
      </div>
      <PlanPreview result={planResult} />
    </section>
  );
}

function TasksTable({ platform, tasks }) {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">وظایف {platform.name}</h3><span className="pill">{tasks.length} وظیفه</span></div>
      <div className="table-scroll">
        <table className="table rtl-table wide-table">
          <thead><tr><th>شناسه</th><th>پلتفرم</th><th>عملیات</th><th>اکانت</th><th>راه‌انداز</th><th>وضعیت</th><th>شروع در</th><th>خاتمه در</th></tr></thead>
          <tbody>{tasks.map((task) => <tr key={task.task_id}><td className="truncate">{task.task_id}</td><td>{task.platform || platform.name}</td><td>{task.operation || task.action || "-"}</td><td>{task.account_id || "-"}</td><td>{task.trigger || "-"}</td><td><Pill value={task.status} /></td><td>{displayDate(task.started_at || task.created_at || task.run_at)}</td><td>{displayDate(task.finished_at || task.completed_at)}</td></tr>)}</tbody>
        </table>
      </div>
      {tasks.length === 0 ? <div className="empty-state">وظیفه‌ای برای این پلتفرم وجود ندارد.</div> : null}
    </section>
  );
}

function ReportsSection({ platform, logs }) {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">گزارش‌ها</h3><span className="pill">{logs.length} گزارش</span></div>
      <div className="settings-grid">
        <section className="empty-state">گزارش اجرا بر اساس لاگ‌های موجود نمایش داده می‌شود.</section>
        <section className="empty-state">گزارش پیام‌ها هنوز به منبع داده جداگانه متصل نشده است.</section>
      </div>
      <LogsTable platform={platform} logs={logs} />
    </section>
  );
}

function LogsTable({ platform, logs }) {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header"><h3 className="panel-title">لاگ فنی {platform.name}</h3><span className="pill">{logs.length} لاگ</span></div>
      <div className="table-scroll">
        <table className="table rtl-table wide-table">
          <thead><tr><th>شناسه</th><th>لید / مخاطب</th><th>سناریو</th><th>مرحله</th><th>نوع عملیات</th><th>کلید رویداد</th><th>وضعیت</th><th>پیام</th><th>خطا</th><th>علت</th><th>اکانت</th><th>dry_run</th><th>ایجاد شده در</th></tr></thead>
          <tbody>{logs.map((log) => <tr key={log.id || `${log.task_id}-${log.timestamp}`}><td>{log.id || "-"}</td><td>{log.lead || log.contact || "-"}</td><td>{log.scenario_id || "-"}</td><td>{log.step || "-"}</td><td>{log.operation_type || log.action || "-"}</td><td>{log.event_key || log.level || "-"}</td><td>{log.status || "-"}</td><td className="truncate">{log.message || "-"}</td><td className="truncate">{log.error || log.error_code || "-"}</td><td>{log.reason || "-"}</td><td>{log.account_id || "-"}</td><td>{String(log.dry_run ?? "-")}</td><td>{displayDate(log.created_at || log.timestamp)}</td></tr>)}</tbody>
        </table>
      </div>
      {logs.length === 0 ? <div className="empty-state">گزارشی برای این پلتفرم وجود ندارد.</div> : null}
    </section>
  );
}

function PlanPreview({ result }) {
  if (!result) return null;
  const items = result.planned_steps || result.plan || result.selected_accounts || [];
  return (
    <div className="plan-preview">
      <div className="panel-header"><h4 className="panel-title">نتیجه dry-run</h4><span className="pill">{items.length} مورد</span></div>
      <pre>{JSON.stringify(result, null, 2)}</pre>
    </div>
  );
}
