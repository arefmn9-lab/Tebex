import { ChevronDown, FileSpreadsheet, FileText, Instagram, MessageCircle, PauseCircle, Plus, Radio, RefreshCw, Save, Send, SendHorizontal, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { listPlatformAccounts } from "../api/accountRegistry";
import { confirmRecipientImport, uploadRecipientImport } from "../api/recipientImports";
import {
  allocateCampaignCapacity,
  createCampaign,
  deleteCampaign,
  finalReviewCampaign,
  getCampaign,
  getCampaignCapacity,
  listCampaigns,
  pauseCampaign,
  queueCampaign,
  resumeCampaign,
  startCampaign,
  updateCampaign,
  validateCampaignStart,
} from "../api/campaigns";
import { campaignValidationFailureMessage, executeCampaignStart } from "../campaignStartFlow";
import { recordDiagnosticEvent } from "../diagnostics";
import {
  ContentCard,
  EmptyState,
  FormField,
  InlineError,
  LoadingState,
  NumberInput,
  PrimaryButton,
  SecondaryButton,
  StatusBadge,
  SuccessButton,
  TextInput,
} from "../components/ui/DesignSystem.jsx";

const platformOptions = [
  { id: "bale", label: "Bale", name: "بله", supported: true, icon: MessageCircle, accent: "#1d9bf0" },
  { id: "telegram", label: "Telegram", name: "تلگرام", supported: false, icon: SendHorizontal, accent: "#229ed9" },
  { id: "whatsapp", label: "WhatsApp", name: "واتساپ", supported: false, icon: MessageCircle, accent: "#22a06b" },
  { id: "eitaa", label: "Eitaa", name: "ایتا", supported: false, icon: Radio, accent: "#d9902f" },
  { id: "rubika", label: "Rubika", name: "روبیکا", supported: false, icon: Instagram, accent: "#7c3aed" },
];

const defaultDraft = {
  id: "",
  name: "",
  description: "",
  status: "draft",
  platforms: ["bale"],
  sourceUrls: { bale: "" },
  capacityReservation: 0,
  capacityReservationRemaining: 0,
  numberFileName: "",
  numberFile: null,
  numberCount: 0,
  deliveriesPerRound: 50,
  dailyLimitPerAccount: 500,
  delaySeconds: 4,
  policyOverrides: { campaign_origin: "operator_ui", operator_visible: true },
};

const statusLabels = {
  draft: "پیش‌نویس",
  ready: "آماده اجرا",
  queued: "آماده اجرا",
  running: "در حال اجرا",
  paused: "متوقف",
  completed: "تمام شده",
  failed: "تمام شده",
  cancelled: "تمام شده",
};

function isInternalCampaign(campaign) {
  return Boolean(campaign.is_internal || campaign.operator_visible === false || campaign.hidden || campaign.soft_deleted || campaign.deleted_at);
}

function statusLabel(status) {
  return statusLabels[status] || "پیش‌نویس";
}

function statusTone(status) {
  if (["running", "queued", "ready"].includes(status)) return "success";
  if (status === "paused") return "warning";
  if (["failed", "cancelled"].includes(status)) return "danger";
  if (status === "completed") return "info";
  return "neutral";
}

function campaignId(campaign) {
  return campaign.id || campaign.campaign_id || "";
}

function campaignTitle(campaign) {
  return campaign.name || campaign.title || campaign.campaign_name || "کمپین بدون نام";
}

function getPolicy(campaign) {
  return campaign.policy_overrides || campaign.policy || {};
}

function normalizeCampaign(campaign) {
  const policy = getPolicy(campaign);
  const selectedPlatforms = campaign.platforms || policy.selected_platforms || [campaign.platform || "bale"];
  const sourceUrls = policy.platform_source_urls || campaign.platform_source_urls || {};
  if (!Object.keys(sourceUrls).length && (campaign.source_channel_uid || campaign.source_url || campaign.source_channel_url)) {
    sourceUrls[selectedPlatforms[0] || "bale"] = campaign.source_channel_uid || campaign.source_url || campaign.source_channel_url;
  }

  const reservation = campaign.capacity_reservation || {};
  return {
    ...defaultDraft,
    id: campaignId(campaign),
    name: campaignTitle(campaign),
    description: campaign.description || campaign.summary || "",
    status: campaign.status || "draft",
    platforms: selectedPlatforms,
    sourceUrls,
    capacityReservation: Number(reservation.allocated_account_count || 0),
    capacityReservationRemaining: Number(reservation.allocated_account_count || 0),
    numberFileName: campaign.number_file_name || policy.number_file_name || "",
    numberCount: campaign.number_count || campaign.contact_count || campaign.recipient_count || policy.number_count || 0,
    deliveriesPerRound: policy.deliveries_per_round ?? null,
    dailyLimitPerAccount: policy.daily_limit_per_account ?? null,
    delaySeconds: policy.operation_delay_seconds ?? policy.delay_between_deliveries_seconds ?? null,
    policyOverrides: policy,
  };
}

function payloadFromDraft(draft) {
  const selectedSourceUrls = Object.fromEntries(draft.platforms.map((platformId) => [platformId, draft.sourceUrls[platformId] || ""]));
  const primarySource = selectedSourceUrls[draft.platforms[0] || "bale"] || "";
  const numberOrUndefined = (value) => value == null || value === "" ? undefined : Number(value);
  const policyOverrides = {
      ...draft.policyOverrides,
      selected_platforms: draft.platforms,
      platform_source_urls: selectedSourceUrls,
      campaign_origin: draft.policyOverrides?.campaign_origin || "operator_ui",
      operator_visible: draft.policyOverrides?.operator_visible ?? true,
      number_file_name: draft.numberFileName || undefined,
      number_count: numberOrUndefined(draft.numberCount),
      deliveries_per_round: numberOrUndefined(draft.deliveriesPerRound),
      daily_limit_per_account: numberOrUndefined(draft.dailyLimitPerAccount),
      operation_delay_seconds: numberOrUndefined(draft.delaySeconds),
  };
  for (const [key, value] of Object.entries(policyOverrides)) {
    if (value === undefined) delete policyOverrides[key];
  }
  return {
    name: draft.name.trim(),
    description: draft.description.trim() || null,
    platform: draft.platforms[0] || "bale",
    status: draft.status,
    source_channel_uid: sourceUidFromValue(primarySource),
    policy_overrides: policyOverrides,
  };
}

function sourceUidFromValue(value) {
  const source = String(value || "").trim();
  if (!source) return null;
  try {
    const parsed = new URL(source);
    return parsed.searchParams.get("uid") || source;
  } catch {
    return source;
  }
}

function campaignActionError(error) {
  const detail = error?.data?.detail;
  const code = detail?.error_code || error?.data?.error_code;
  const message = detail?.error_message
    || error?.data?.error_message
    || (code === "campaign_validation_failed" && detail?.validation
      ? campaignValidationFailureMessage(detail.validation)
      : error?.message);
  const accounts = detail?.accounts || detail?.validation?.accounts || [];
  const accountReasons = accounts
    .filter((account) => account?.eligible === false)
    .map((account) => `${account.account_id}: ${(account.blockers || []).join(", ")}`)
    .join(" | ");
  const context = detail?.details || detail?.context || {};
  const collision = context.row_number
    ? `ردیف ${context.row_number} · شماره ${context.phone_masked || "***"} · نام پایدار ${context.stable_display_name || "نامشخص"}`
    : "";
  return [code, message, collision, accountReasons].filter(Boolean).join(": ") || "عملیات کمپین انجام نشد.";
}

function capacityActionError(error) {
  const detail = error?.data?.detail || error?.data || {};
  const code = detail?.error_code || error?.errorCode || "";
  const capacity = detail?.details || detail?.context || detail?.validation || detail;
  const requested = Number(capacity?.requested_account_count ?? capacity?.required_account_count);
  const eligible = Number(capacity?.eligible_account_count);
  if (["requested_accounts_exceed_eligible", "campaign_capacity_pool_insufficient"].includes(code)) {
    if (Number.isInteger(requested) && requested > 0 && Number.isInteger(eligible) && eligible >= 0) {
      return `برای اجرای این کمپین ${requested.toLocaleString("fa-IR")} اکانت لازم است؛ در حال حاضر ${eligible.toLocaleString("fa-IR")} اکانت آماده است.`;
    }
    return "تعداد درخواستی از اکانت‌های آماده و در دسترس بیشتر است.";
  }
  if (code === "no_eligible_bale_accounts") {
    return "اکانت آماده‌ای برای این کمپین پیدا نشد. وضعیت اکانت‌ها را بررسی کنید.";
  }
  if (code === "campaign_not_found") {
    return "این کمپین دیگر در دسترس نیست. یک کمپین دیگر را انتخاب کنید.";
  }
  if (error?.errorCode === "BACKEND_OFFLINE") {
    return "ارتباط با سامانه برقرار نیست. چند لحظه بعد دوباره تلاش کنید.";
  }
  return "ذخیره تخصیص انجام نشد. لطفاً دوباره تلاش کنید.";
}

function runtimeCapacityLabel(capacity = {}) {
  const ceilings = Object.values(capacity.exact_concurrency || {})
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value > 0);
  return ceilings.length ? Math.min(...ceilings) : 0;
}

function importCountMessage(counts = {}) {
  const submitted = Number(counts.submitted_count || counts.uploaded_row_count || 0);
  const valid = Number(counts.valid_count || counts.created_recipient_count || 0);
  const duplicate = Number(counts.duplicate_count || 0);
  const invalid = Number(counts.invalid_count || 0);
  const deliverable = Number(counts.created_job_count || 0);
  return `واردشده: ${submitted}، معتبر: ${valid}، تکراری: ${duplicate}، نامعتبر: ${invalid}، قابل ارسال: ${deliverable}`;
}

function actionIdempotencyKey(campaignIdValue) {
  return `campaign-ui-${campaignIdValue}-${Date.now()}`;
}

function fileCountHint(file) {
  if (!file) return 0;
  return Math.max(0, Math.round(file.size / 28));
}

function CampaignDetails({ campaign }) {
  const normalized = normalizeCampaign(campaign);
  const operations = campaign.operations_summary || campaign.summary || {};

  return (
    <div className="campaign-details-grid">
      <p><span>پلتفرم‌ها</span><b>{normalized.platforms.map((id) => platformOptions.find((item) => item.id === id)?.name || id).join("، ")}</b></p>
      <p><span>اکانت‌های این کمپین</span><b>{normalized.capacityReservation || 0}</b></p>
      <p><span>منبع ارسال</span><b>{Object.values(normalized.sourceUrls).filter(Boolean).length || 0} لینک</b></p>
      <p><span>بانک شماره</span><b>{normalized.numberFileName || "فایلی انتخاب نشده"} · {normalized.numberCount} شماره</b></p>
      <p><span>تنظیمات ارسال</span><b>{normalized.deliveriesPerRound || "پیش‌فرض سامانه"} ارسال در هر دور · {normalized.dailyLimitPerAccount || "پیش‌فرض سامانه"} روزانه</b></p>
      <p><span>وضعیت</span><b>{statusLabel(normalized.status)}</b></p>
      <p><span>خلاصه عملیات</span><b>{operations.completed || 0} انجام‌شده · {operations.failed || 0} خطا · {operations.pending || 0} در انتظار</b></p>
      <p><span>پیشرفت کمپین</span><b>{campaign.total_recipients || 0} مخاطب · {campaign.queued_count || 0} صف · {campaign.succeeded_count || 0} تکمیل · {campaign.failed_count || 0} ناموفق · {Math.max(0, Number(campaign.total_recipients || 0) - Number(campaign.succeeded_count || 0) - Number(campaign.failed_count || 0) - Number(campaign.cancelled_count || 0) - Number(campaign.skipped_count || 0))} باقی‌مانده</b></p>
    </div>
  );
}

function platformMeta(platformId) {
  return platformOptions.find((item) => item.id === platformId) || { id: platformId, label: platformId, name: platformId, icon: MessageCircle, accent: "#2947b6" };
}

function platformSummary(platforms = []) {
  return platforms.map((id) => platformMeta(id).name).join("، ");
}

function sourcePlaceholder(platformId) {
  const placeholders = {
    bale: "https://web.bale.ai/chat?uid=...",
    telegram: "https://t.me/channel-or-post",
    whatsapp: "https://chat.whatsapp.com/...",
    eitaa: "https://eitaa.com/channel",
    rubika: "https://rubika.ir/channel",
  };
  return placeholders[platformId] || "https://...";
}

export default function CommercialCampaigns() {
  const [campaigns, setCampaigns] = useState([]);
  const [accountsByPlatform, setAccountsByPlatform] = useState({});
  const [draft, setDraft] = useState(defaultDraft);
  const [expandedCampaignId, setExpandedCampaignId] = useState(() => localStorage.getItem("clinicos:selected-bale-campaign") || "");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [backendOffline, setBackendOffline] = useState(false);
  const [capacityState, setCapacityState] = useState({ status: "no_campaign_selected", data: null, error: "" });
  const [capacityInput, setCapacityInput] = useState("");
  const capacityInputDirty = useRef(false);
  const capacityInitializedCampaign = useRef("");
  const newCampaignMode = useRef(false);
  const campaignRunGuards = useRef(new Set());
  const [notice, setNotice] = useState("");

  const connectedPlatforms = useMemo(() => {
    const map = {};
    for (const platform of platformOptions) {
      map[platform.id] = (accountsByPlatform[platform.id] || []).length > 0;
    }
    return map;
  }, [accountsByPlatform]);
  const visibleCampaigns = useMemo(() => campaigns.filter((campaign) => !isInternalCampaign(campaign)), [campaigns]);
  const campaignStats = useMemo(() => {
    const running = visibleCampaigns.filter((campaign) => campaign.status === "running").length;
    const draft = visibleCampaigns.filter((campaign) => !campaign.status || campaign.status === "draft").length;
    const platforms = new Set(visibleCampaigns.flatMap((campaign) => normalizeCampaign(campaign).platforms));
    return { total: visibleCampaigns.length, running, draft, platforms: platforms.size };
  }, [visibleCampaigns]);

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [campaignData, baleAccounts] = await Promise.all([
        listCampaigns({ limit: 100, offset: 0 }),
        listPlatformAccounts("bale"),
      ]);
      const rows = Array.isArray(campaignData) ? campaignData : campaignData?.items || [];
      const accountsMap = { bale: Array.isArray(baleAccounts) ? baleAccounts : baleAccounts?.items || [] };
      setCampaigns(rows);
      setAccountsByPlatform(accountsMap);
      setBackendOffline(false);
    } catch (err) {
      setBackendOffline(err.errorCode === "BACKEND_OFFLINE" || err.errorCode === "REQUEST_TIMEOUT");
      setError(err.errorCode === "REQUEST_TIMEOUT" ? "مهلت درخواست به پایان رسید" : "ارتباط با بکاند قطع است");
    } finally {
      setLoading(false);
    }
  }

  function updateDraft(patch) {
    setDraft((current) => ({ ...current, ...patch }));
  }

  function startNewCampaign() {
    newCampaignMode.current = true;
    setDraft({ ...defaultDraft, sourceUrls: { bale: "" }, capacityReservation: 0 });
    setExpandedCampaignId("");
    localStorage.removeItem("clinicos:selected-bale-campaign");
    setCapacityInput("");
    capacityInputDirty.current = false;
    capacityInitializedCampaign.current = "";
    setNotice("");
    setError("");
    recordDiagnosticEvent({ action: "campaign_create", stage: "form_opened", success: true });
  }

  function editCampaign(campaign) {
    newCampaignMode.current = false;
    const normalized = normalizeCampaign(campaign);
    setDraft(normalized);
    setExpandedCampaignId(campaignId(campaign));
    setCapacityInput(String(normalized.capacityReservation || ""));
    capacityInputDirty.current = false;
    capacityInitializedCampaign.current = campaignId(campaign);
    setNotice("");
    setError("");
  }

  function selectCampaign(id) {
    newCampaignMode.current = false;
    capacityInputDirty.current = false;
    capacityInitializedCampaign.current = "";
    setExpandedCampaignId(id);
    recordDiagnosticEvent({ action: "campaign_selection", stage: "operator_selected", campaign_id: id, success: true });
  }

  function togglePlatform(platformId) {
    const selected = draft.platforms.includes(platformId);
    const nextPlatforms = selected ? draft.platforms.filter((id) => id !== platformId) : [...draft.platforms, platformId];
    const safePlatforms = nextPlatforms.length ? nextPlatforms : ["bale"];
    const nextSourceUrls = Object.fromEntries(safePlatforms.map((id) => [id, draft.sourceUrls[id] || ""]));
    updateDraft({
      platforms: safePlatforms,
      sourceUrls: nextSourceUrls,
    });
  }

  function setPlatformSource(platformId, value) {
    updateDraft({ sourceUrls: { ...draft.sourceUrls, [platformId]: value } });
  }

  function setCapacityReservation(value) {
    const previous = capacityInput;
    setCapacityInput(value);
    capacityInputDirty.current = true;
    console.info("[CAMPAIGN_RESERVATION_INPUT]", { event: "input_change", campaign_id: draft.id || null, previous_value: previous, next_value: value, disabled: !draft.id || busy === "capacity", disabled_reason: !draft.id ? "no_campaign_selected" : busy === "capacity" ? "allocation_pending" : null });
    recordDiagnosticEvent({ action: "campaign_capacity_input", stage: "input_change", campaign_id: draft.id || null, result: { previous_value: previous, next_value: value }, button_disabled: !draft.id || busy === "capacity", disabled_reason: !draft.id ? "no_campaign_selected" : busy === "capacity" ? "allocation_pending" : null });
  }

  async function allocateCapacity() {
    if (!draft.id || busy === "capacity") return;
    if (!/^\d+$/.test(capacityInput) || Number(capacityInput) < 1) {
      setCapacityState((current) => ({ ...current, status: "failed", error: "تعداد اکانت باید یک عدد صحیح حداقل ۱ باشد." }));
      return;
    }
    const requestPayload = { requested_account_count: Number(capacityInput) };
    setBusy("capacity");
    setCapacityState((current) => ({ ...current, status: "allocating", error: "" }));
    console.info("[CAMPAIGN_CAPACITY_REQUEST]", { campaign_id: draft.id, endpoint: `/automation/campaigns/${draft.id}/capacity`, request_payload: requestPayload, before: capacityState.data });
    recordDiagnosticEvent({ action: "campaign_capacity_allocation", stage: "api_request_started", campaign_id: draft.id, endpoint: `/automation/campaigns/${draft.id}/capacity`, http_method: "PUT", result: { submitted_count: requestPayload.requested_account_count } });
    try {
      const result = await allocateCampaignCapacity(draft.id, requestPayload);
      setCapacityState({ status: "allocated", data: result, error: "" });
      const persistedCount = Number(result?.reservation?.allocated_account_count || 0);
      updateDraft({ capacityReservation: persistedCount, capacityReservationRemaining: persistedCount });
      setCapacityInput(String(persistedCount));
      capacityInputDirty.current = false;
      console.info("[CAMPAIGN_CAPACITY_RESPONSE]", { campaign_id: draft.id, http_status: result?.__http_status, reservation_id: result?.reservation?.campaign_id, after: result });
      recordDiagnosticEvent({ action: "campaign_capacity_allocation", stage: "terminal_result", campaign_id: draft.id, success: true, result: { reservation_id: result?.reservation?.campaign_id || null, requested_account_count: result?.requested_account_count, allocated_account_count: result?.allocated_account_count, eligible_account_count: result?.eligible_account_count, reserved_account_count: result?.reserved_account_count, free_account_count: result?.free_account_count } });
    } catch (err) {
      const status = err.errorCode === "BACKEND_OFFLINE" ? "backend_offline" : err.status === 409 ? "allocation_conflict" : "failed";
      setCapacityState({ status, data: capacityState.data, error: capacityActionError(err) });
      console.info("[CAMPAIGN_CAPACITY_ERROR]", { campaign_id: draft.id, http_status: err.status, error_code: err.errorCode, response: err.data });
      recordDiagnosticEvent({ action: "campaign_capacity_allocation", stage: "terminal_result", campaign_id: draft.id, success: false, error_code: err.errorCode, error_message: capacityActionError(err), result: err.data });
    } finally {
      setBusy("");
    }
  }

  function handleNumberFile(file) {
    if (!file) return;
    updateDraft({ numberFile: file, numberFileName: file.name, numberCount: fileCountHint(file) });
  }

  async function saveDraft() {
    if (!draft.name.trim()) {
      setError("نام کمپین را وارد کنید.");
      return;
    }
    setBusy("save");
    setError("");
    setNotice("");
    try {
      const creating = !draft.id;
      const persistedDraft = { ...draft };
      const saveEndpoint = creating ? "/automation/campaigns" : `/automation/campaigns/${draft.id}`;
      recordDiagnosticEvent({ action: creating ? "campaign_create" : "campaign_update", stage: "api_request_started", campaign_id: draft.id || null, endpoint: saveEndpoint, http_method: creating ? "POST" : "PATCH", handler_reached: true, success: true });
      const saved = draft.id
        ? await updateCampaign(draft.id, payloadFromDraft(persistedDraft))
        : await createCampaign(payloadFromDraft(persistedDraft));
      const savedCampaign = saved?.campaign || saved;
      const savedId = savedCampaign?.id || savedCampaign?.campaign_id || draft.id;
      if (savedId) {
        newCampaignMode.current = false;
        setDraft((current) => ({ ...current, id: savedId }));
        setExpandedCampaignId(savedId);
        localStorage.setItem("clinicos:selected-bale-campaign", savedId);
        setCampaigns((current) => current.some((item) => campaignId(item) === savedId)
          ? current.map((item) => campaignId(item) === savedId ? { ...item, ...savedCampaign } : item)
          : [{ ...savedCampaign, campaign_id: savedId }, ...current]);
        recordDiagnosticEvent({ action: creating ? "campaign_create" : "campaign_update", stage: "api_request_succeeded", campaign_id: savedId, endpoint: saveEndpoint, http_method: creating ? "POST" : "PATCH", success: true, result: { campaign_id: savedId } });
        recordDiagnosticEvent({ action: "campaign_selection", stage: creating ? "new_campaign_selected" : "saved_campaign_selected", campaign_id: savedId, success: true });
      } else {
        throw new Error("campaign_create_response_missing_id");
      }
      setNotice("۱. کمپین ایجاد شد.");
      let importResult = null;
      if (draft.numberFile && savedId) {
        try {
          const preview = await uploadRecipientImport(savedId, draft.numberFile);
          setNotice("۱. کمپین ایجاد شد. ۲. فایل مخاطبان بارگذاری شد.");
          if (Number(preview?.valid_count || 0) <= 0) {
            throw new Error(`هیچ مخاطب معتبری وارد نشد. ${importCountMessage(preview)}`);
          }
          importResult = await confirmRecipientImport(preview.batch_id || preview.id, {
            include_valid: true,
            authorize_for_live_execution: true,
            authorized_by: "campaign_ui_operator",
            authorization_note: "فایل مخاطبان توسط کاربر در فرم کمپین انتخاب و تأیید شد.",
          });
          if (Number(importResult?.created_recipient_count || 0) <= 0 || Number(importResult?.created_job_count || 0) <= 0) {
            throw new Error(`مخاطب قابل ارسال برای کمپین ایجاد نشد. ${importCountMessage({ ...preview, ...importResult })}`);
          }
        } catch (importError) {
          const exactError = campaignActionError(importError);
          setNotice("۱. کمپین ایجاد شد. ۲. فایل مخاطبان بارگذاری شد. ۳. تأیید مخاطبان ناموفق بود. کمپین به‌صورت پیش‌نویس باقی ماند و می‌توانید فایل اصلاح‌شده را روی همین کمپین دوباره وارد کنید.");
          setError(exactError);
          await load();
          return;
        }
      }
      setNotice(importResult
        ? `کمپین و مخاطبان ذخیره شدند. ${importCountMessage(importResult)}`
        : "کمپین ذخیره شد.");
      await load();
      if (savedId) setDraft((current) => ({ ...current, id: savedId, numberFile: null }));
    } catch (err) {
      recordDiagnosticEvent({ action: draft.id ? "campaign_update" : "campaign_create", stage: "terminal_error", campaign_id: draft.id || null, success: false, error_code: err.errorCode, error_message: campaignActionError(err) });
      setError(err.message || "ذخیره کمپین انجام نشد.");
    } finally {
      setBusy("");
    }
  }

  async function runCampaign(campaign) {
    const id = campaignId(campaign);
    if (!id) return;
    // React state updates are asynchronous; a rapid double click can enter
    // this handler twice before `busy` re-renders. Keep the guard synchronous
    // so one campaign can have only one active transition in this tab.
    if (campaignRunGuards.current.has(id)) return;
    campaignRunGuards.current.add(id);
    setBusy(`run:${id}`);
    setError("");
    setNotice("");
    try {
      console.info("[CAMPAIGN_ACTION]", { campaign_id: id, action: "run", previous_status: campaign.status });
      if (campaign.status === "running") {
        setNotice("کمپین هم‌اکنون در حال اجرا است.");
      } else if (campaign.status === "paused") {
        await resumeCampaign(id);
        setNotice("اجرای کمپین از سر گرفته شد.");
      } else if (campaign.status === "queued") {
        await startCampaign(id);
        setNotice("اجرای کمپین آغاز شد.");
      } else if (campaign.status === "draft") {
        await executeCampaignStart(id, {
          validateCampaignStart,
          finalReviewCampaign,
          queueCampaign,
          startCampaign,
          idempotencyKey: actionIdempotencyKey(id),
        });
        setNotice("کمپین صف‌بندی و اجرا شد.");
      } else {
        throw new Error(`invalid_campaign_transition: ${campaign.status}`);
      }
      await load();
    } catch (err) {
      console.info("[CAMPAIGN_TRANSITION]", { campaign_id: id, action: "run", status: "failed", error: campaignActionError(err) });
      setError(campaignActionError(err));
    } finally {
      campaignRunGuards.current.delete(id);
      setBusy("");
    }
  }

  async function stopCampaign(campaign) {
    const id = campaignId(campaign);
    if (!id) return;
    setBusy(`stop:${id}`);
    setError("");
    setNotice("");
    try {
      await pauseCampaign(id);
      console.info("[CAMPAIGN_TRANSITION]", { campaign_id: id, action: "pause", status: "success" });
      setNotice("درخواست توقف کمپین ثبت شد.");
      await load();
    } catch (err) {
      console.info("[CAMPAIGN_TRANSITION]", { campaign_id: id, action: "pause", status: "failed", error: campaignActionError(err) });
      setError(campaignActionError(err));
    } finally {
      setBusy("");
    }
  }

  async function removeCampaign(campaign) {
    const id = campaignId(campaign);
    if (!id || !window.confirm("این کمپین و همه داده‌های وابسته به آن حذف شود؟")) return;
    setBusy(`delete:${id}`);
    setError("");
    setNotice("");
    try {
      console.info("[CAMPAIGN_DELETE]", { campaign_id: id, status: "requested" });
      if (campaign.status === "running") await pauseCampaign(id);
      await deleteCampaign(id);
      if (draft.id === id) startNewCampaign();
      setNotice("کمپین حذف شد.");
      await load();
    } catch (err) {
      console.info("[CAMPAIGN_DELETE]", { campaign_id: id, status: "failed", error: campaignActionError(err) });
      setError(campaignActionError(err));
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (expandedCampaignId) {
      localStorage.setItem("clinicos:selected-bale-campaign", expandedCampaignId);
      recordDiagnosticEvent({ action: "campaign_selection", stage: "selection_persisted", campaign_id: expandedCampaignId, success: true });
    } else {
      localStorage.removeItem("clinicos:selected-bale-campaign");
    }
  }, [expandedCampaignId]);

  useEffect(() => {
    if (loading) return;
    if (newCampaignMode.current) return;
    if (expandedCampaignId && visibleCampaigns.some((item) => campaignId(item) === expandedCampaignId)) return;
    const replacement = visibleCampaigns[0];
    if (replacement) {
      const replacementId = campaignId(replacement);
      capacityInputDirty.current = false;
      capacityInitializedCampaign.current = "";
      setExpandedCampaignId(replacementId);
      recordDiagnosticEvent({ action: "campaign_selection", stage: "invalid_selection_replaced", campaign_id: replacementId, success: true });
      return;
    }
    if (expandedCampaignId) {
      setExpandedCampaignId("");
      recordDiagnosticEvent({ action: "campaign_selection", stage: "stored_selection_cleared", campaign_id: expandedCampaignId, success: true, error_code: "selected_campaign_not_visible" });
    }
  }, [expandedCampaignId, loading, visibleCampaigns]);

  useEffect(() => {
    if (!expandedCampaignId) {
      setCapacityState({ status: "no_campaign_selected", data: null, error: "" });
      return;
    }
    const selected = visibleCampaigns.find((item) => campaignId(item) === expandedCampaignId);
    if (!selected) {
      if (!loading) {
        setCapacityState({ status: "no_campaign_selected", data: null, error: "" });
        recordDiagnosticEvent({ action: "campaign_selection", stage: "stored_selection_not_visible", campaign_id: expandedCampaignId, success: false, error_code: "selected_campaign_not_visible" });
      }
      return;
    }
    recordDiagnosticEvent({ action: "campaign_selection", stage: "restored_campaign_resolved", campaign_id: expandedCampaignId, success: true });
    setCapacityState((current) => ({ ...current, status: "loading", error: "" }));
    Promise.all([getCampaign(expandedCampaignId), getCampaignCapacity(expandedCampaignId)])
      .then(([persistedCampaign, capacity]) => {
        setDraft((current) => current.id && current.id !== expandedCampaignId ? current : normalizeCampaign(persistedCampaign));
        if (!capacityInputDirty.current && capacityInitializedCampaign.current !== expandedCampaignId) {
          setCapacityInput(String(capacity?.reservation?.allocated_account_count || ""));
          capacityInitializedCampaign.current = expandedCampaignId;
        }
        setCapacityState({ status: capacity?.reservation ? "allocated" : "ready_to_allocate", data: capacity, error: "" });
      })
      .catch((err) => setCapacityState((current) => ({ ...current, status: err.errorCode === "BACKEND_OFFLINE" ? "backend_offline" : "failed", error: campaignActionError(err) })));
  }, [expandedCampaignId, visibleCampaigns, loading]);

  return (
    <section className="campaign-builder-page" dir="rtl">
      <div className="premium-campaign-hero">
        <div>
          <span className="campaign-eyebrow">مرکز کمپین</span>
          <h1>کمپین‌ها</h1>
          <p>کمپین را بسازید، مخاطبان را وارد کنید، تعداد اکانت لازم را تعیین کنید و سپس آن را صف‌بندی یا شروع کنید.</p>
        </div>
        <div className="premium-hero-actions">
          <SecondaryButton onClick={load} disabled={loading}><RefreshCw size={17} />تازه‌سازی</SecondaryButton>
          <PrimaryButton onClick={startNewCampaign}><Plus size={17} />ساخت کمپین جدید</PrimaryButton>
        </div>
        {campaignStats.total ? (
          <div className="premium-campaign-stats" aria-label="خلاصه کمپین‌ها">
            <article><span>کل کمپین‌ها</span><strong>{campaignStats.total}</strong></article>
            <article><span>در حال اجرا</span><strong>{campaignStats.running}</strong></article>
            <article><span>پیش‌نویس</span><strong>{campaignStats.draft}</strong></article>
            <article><span>پیام‌رسان‌ها</span><strong>{campaignStats.platforms}</strong></article>
          </div>
        ) : null}
      </div>

      {error ? <InlineError>{error} {backendOffline ? <button type="button" onClick={load}>Retry</button> : null}</InlineError> : null}
      {notice ? <div className="toast">{notice}</div> : null}
      {loading ? <LoadingState label="در حال دریافت کمپین‌ها" /> : null}

      {!loading && !error && !visibleCampaigns.length ? (
        <EmptyState
          title="هنوز کمپینی ایجاد نکرده‌اید"
          action={<PrimaryButton onClick={startNewCampaign}><Plus size={17} />ساخت کمپین جدید</PrimaryButton>}
        />
      ) : null}

      <div className={`premium-campaign-layout ${visibleCampaigns.length ? "" : "workspace-only"}`}>
        <div className="campaign-overview-column">
      {!loading && visibleCampaigns.length ? (
            <ContentCard title="نمای کلی کمپین‌ها" description="فقط کمپین‌های قابل استفاده برای کار روزانه نمایش داده می‌شوند.">
          <div className="simple-campaign-list">
            {visibleCampaigns.map((campaign) => {
              const id = campaignId(campaign);
              const expanded = expandedCampaignId === id;
              const normalized = normalizeCampaign(campaign);
              return (
                <article className="simple-campaign-item" key={id || campaign.name}>
                  <button className="simple-campaign-main" type="button" onClick={() => {
                    selectCampaign(id);
                  }}>
                    <span>
                      <strong>{campaignTitle(campaign)}</strong>
                      <small>{campaign.description || "بدون توضیحات"}</small>
                    </span>
                    <span className="campaign-row-meta">
                      <StatusBadge tone={statusTone(campaign.status)}>{statusLabel(campaign.status)}</StatusBadge>
                      <span className="campaign-platform-icons">
                        {normalized.platforms.slice(0, 4).map((platformId) => {
                          const meta = platformMeta(platformId);
                          const Icon = meta.icon;
                          return <span key={platformId} style={{ "--platform-accent": meta.accent }} title={meta.name}><Icon size={15} /></span>;
                        })}
                      </span>
                      <small>{normalized.numberCount || 0} شماره · ظرفیت {normalized.capacityReservation || 0}</small>
                    </span>
                    <ChevronDown className={expanded ? "open" : ""} size={18} />
                  </button>
                  <div className="campaign-inline-actions">
                    <SuccessButton disabled={Boolean(busy)} onClick={() => runCampaign(campaign)}><Send size={16} />اجرای کمپین</SuccessButton>
                    <SecondaryButton disabled={Boolean(busy) || campaign.status !== "running"} onClick={() => stopCampaign(campaign)}><PauseCircle size={16} />توقف کمپین</SecondaryButton>
                    <SecondaryButton disabled={Boolean(busy)} onClick={() => removeCampaign(campaign)}><Trash2 size={16} />حذف کمپین</SecondaryButton>
                  </div>
                  {expanded ? (
                    <div className="simple-campaign-details">
                      <CampaignDetails campaign={campaign} />
                      <SecondaryButton onClick={() => editCampaign(campaign)}>ویرایش این کمپین</SecondaryButton>
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        </ContentCard>
      ) : null}
        </div>

      <div className="campaign-workspace-column">
      <div className="business-builder-grid single">
        <div className="business-builder-main">
          <div className="builder-section-label">
            <span>مسیر ساخت کمپین</span>
            <strong>ساخت و ویرایش کمپین</strong>
          </div>

          <ContentCard title="۱. اطلاعات کمپین" description="نام و توضیح کوتاه برای تشخیص سریع کمپین در لیست.">
            <div className="business-form-grid simple">
              <FormField label="نام کمپین">
                <TextInput value={draft.name} onChange={(event) => updateDraft({ name: event.target.value })} placeholder="نام کمپین" />
              </FormField>
              <FormField label="توضیحات">
                <TextInput value={draft.description} onChange={(event) => updateDraft({ description: event.target.value })} placeholder="توضیح کوتاه" />
              </FormField>
            </div>
          </ContentCard>

          <ContentCard title="۲. انتخاب پیام‌رسان‌ها" description="پیام‌رسان مورد استفاده برای این کمپین را انتخاب کنید.">
            <div className="platform-checkbox-grid">
              {platformOptions.filter((platform) => platform.id === "bale").map((platform) => (
                (() => {
                  const Icon = platform.icon;
                  return (
                <button
                  aria-pressed={draft.platforms.includes(platform.id)}
                  className={draft.platforms.includes(platform.id) ? "selected" : ""}
                  key={platform.id}
                  type="button"
                  style={{ "--platform-accent": platform.accent }}
                  onClick={() => togglePlatform(platform.id)}
                >
                  <span className="platform-logo"><Icon size={18} /></span>
                  <input checked={draft.platforms.includes(platform.id)} readOnly type="checkbox" />
                  <span><b>{platform.label}</b><em>{platform.name}</em></span>
                  <small>{connectedPlatforms[platform.id] ? "آماده" : "نیاز به اتصال"}</small>
                </button>
                  );
                })()
              ))}
            </div>
          </ContentCard>

          <ContentCard title="۳. منبع ارسال" description="لینک منبع پیام را وارد کنید.">
            <div className="platform-source-list">
              {draft.platforms.map((platformId) => {
                const platform = platformMeta(platformId);
                const Icon = platform.icon;
                const hasSource = Boolean((draft.sourceUrls[platformId] || "").trim());
                return (
                  <article className={`platform-source-card ${hasSource ? "filled" : ""}`} key={platformId} style={{ "--platform-accent": platform.accent }}>
                    <div className="platform-source-head">
                      <span className="platform-logo"><Icon size={20} /></span>
                      <div>
                        <strong>{platform.label}</strong>
                        <small>{hasSource ? "منبع ثبت شده" : "در انتظار URL"}</small>
                      </div>
                    </div>
                    <FormField label="لینک منبع">
                      <TextInput value={draft.sourceUrls[platformId] || ""} onChange={(event) => setPlatformSource(platformId, event.target.value)} placeholder={sourcePlaceholder(platformId)} />
                    </FormField>
                  </article>
                );
              })}
            </div>
          </ContentCard>

          <ContentCard title="۴. وارد کردن مخاطبان" description="فایل CSV یا Excel را انتخاب کنید؛ پیش از صف‌بندی، شماره‌ها بررسی می‌شوند.">
            <div className="number-upload-grid">
              <label className="number-upload-card">
                <FileText size={22} />
                <strong>فایل CSV</strong>
                <span>انتخاب فایل CSV</span>
                <input accept=".csv,text/csv" type="file" onChange={(event) => handleNumberFile(event.target.files?.[0])} />
              </label>
              <label className="number-upload-card">
                <FileSpreadsheet size={22} />
                <strong>فایل Excel</strong>
                <span>انتخاب فایل Excel</span>
                <input accept=".xlsx" type="file" onChange={(event) => handleNumberFile(event.target.files?.[0])} />
              </label>
              <article className="number-file-summary">
                <span>فایل</span>
                <strong>{draft.numberFileName || "انتخاب نشده"}</strong>
                <small>{draft.numberCount} شماره</small>
              </article>
            </div>
          </ContentCard>

          <ContentCard title="۵. اکانت‌های این کمپین" description="فقط تعداد اکانت لازم را تعیین کنید؛ آماده‌بودن اکانت‌ها خودکار بررسی می‌شود.">
            {!draft.id ? (
              <EmptyState
                title="برای تخصیص اکانت، ابتدا کمپین را ذخیره یا از فهرست انتخاب کنید."
                description="پس از ذخیره، تعداد اکانت موردنیاز این کمپین را وارد کنید."
              />
            ) : (
              <div className="platform-pool-card">
                <div className="platform-pool-stats">
                  <p><span>اکانت‌های آماده</span><b>{capacityState.data?.eligible_account_count ?? "—"}</b></p>
                  <p><span>تعداد درخواستی</span><b>{capacityState.data?.requested_account_count ?? draft.capacityReservation ?? 0}</b></p>
                  <p><span>تخصیص فعلی</span><b>{capacityState.data?.allocated_account_count ?? draft.capacityReservation ?? 0}</b></p>
                </div>
                <FormField label="تعداد اکانت مورد نیاز این کمپین">
                  <NumberInput
                    min="1"
                    disabled={busy === "capacity"}
                    value={capacityInput}
                    onFocus={() => {
                      const disabledReason = busy === "capacity" ? "allocation_pending" : null;
                      console.info("[CAMPAIGN_RESERVATION_INPUT]", { event: "input_focus", selected_campaign_id: draft.id, disabled: Boolean(disabledReason), disabled_reason: disabledReason });
                      recordDiagnosticEvent({ action: "campaign_capacity_input", stage: "input_focus", campaign_id: draft.id, button_disabled: Boolean(disabledReason), disabled_reason: disabledReason });
                    }}
                    onChange={(event) => setCapacityReservation(event.target.value)}
                  />
                </FormField>
                <div className="capacity-allocation-state" data-state={capacityState.status}>
                  <strong>{({ loading: "در حال بررسی اکانت‌ها", backend_offline: "ارتباط با سامانه برقرار نیست", ready_to_allocate: "آماده ذخیره تخصیص", allocating: "در حال ذخیره", allocated: "تخصیص ذخیره شد", allocation_conflict: "تعداد درخواستی در دسترس نیست", failed: "ذخیره تخصیص انجام نشد" }[capacityState.status] || "آماده ذخیره تخصیص")}</strong>
                  {capacityState.error ? <InlineError>{capacityState.error}</InlineError> : null}
                  <PrimaryButton type="button" disabled={busy === "capacity"} onClick={allocateCapacity}>
                    {busy === "capacity" ? "در حال ذخیره…" : "ذخیره تخصیص"}
                  </PrimaryButton>
                </div>
                {capacityState.data?.ready_for_exact_account_execution ? <div className="toast">اکانت‌های درخواستی برای اجرای این کمپین آماده‌اند.</div> : null}
                {capacityState.data?.exact_blockers?.includes("requested_accounts_exceed_runtime_capacity") ? (
                  <InlineError>
                    Campaign capacity exceeds the current runtime concurrency. Requested {capacityState.data.required_account_count} account(s), runtime capacity {runtimeCapacityLabel(capacityState.data)}. Reduce the requested capacity or increase runtime concurrency before Execute.
                  </InlineError>
                ) : null}
              </div>
            )}
          </ContentCard>

          <ContentCard title="۶. تنظیمات ارسال" description="تنظیمات معمول ارسال؛ تنظیمات فنی سامانه به‌صورت خودکار اعمال می‌شوند.">
            <div className="business-settings-grid">
              <FormField label="ارسال در هر دور">
                <NumberInput min="1" value={draft.deliveriesPerRound} onChange={(event) => updateDraft({ deliveriesPerRound: event.target.value })} />
              </FormField>
              <FormField label="محدودیت روزانه هر اکانت">
                <NumberInput min="1" value={draft.dailyLimitPerAccount} onChange={(event) => updateDraft({ dailyLimitPerAccount: event.target.value })} />
              </FormField>
              <FormField label="مکث بین عملیات — ثانیه">
                <NumberInput min="0" value={draft.delaySeconds} onChange={(event) => updateDraft({ delaySeconds: event.target.value })} />
              </FormField>
            </div>
          </ContentCard>

          <ContentCard title="۷. بررسی و ذخیره" description={`پلتفرم‌های انتخاب‌شده: ${platformSummary(draft.platforms) || "هیچ"}`}>
          <div className="builder-save-actions">
            <SecondaryButton onClick={saveDraft} disabled={Boolean(busy)}>
              <Save size={17} />
              {busy === "save" ? "در حال ذخیره" : "ذخیره پیش‌نویس"}
            </SecondaryButton>
          </div>
          </ContentCard>
        </div>
      </div>
      </div>
      </div>
    </section>
  );
}
