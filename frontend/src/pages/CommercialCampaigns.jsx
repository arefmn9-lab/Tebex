import { useEffect, useState } from "react";
import { Eye, Plus, RotateCw, Upload } from "lucide-react";
import {
  cancelCampaign,
  approveCampaignConfigurationRevision,
  checkCampaignWithoutSending,
  checkCampaignConfigurationDrift,
  createCampaignConfigurationRevision,
  createLiveApproval,
  createCampaign,
  finalReviewCampaign,
  getCampaignConfiguration,
  livePreflightCampaign,
  listLiveApprovals,
  listCampaignConfigurationRevisions,
  listCampaignRecipients,
  listRecipientScenarios,
  listCampaigns,
  pauseCampaign,
  queueCampaign,
  requestSendApproval,
  resumeCampaign,
  startCampaign,
  validateCampaignConfiguration,
  validateCampaignStart,
  validateLiveReadiness,
} from "../api/campaigns";
import { listEvents } from "../api/events";
import { confirmRecipientImport, deleteRecipientImport, listRecipientImportItems, previewPasteImport, uploadRecipientImport } from "../api/recipientImports";
import { getEffectivePolicy } from "../api/settings";
import { EmptyState, ErrorState, KeyValueGrid, LoadingState, Modal, PageHeader, Pager, StatusBadge, fmt, shortId } from "../components/commercial/CommercialUi.jsx";

const statuses = ["draft", "queued", "running", "paused", "completed", "cancelled", "failed"];
const importFilters = ["", "valid", "invalid", "duplicate"];
const campaignWizardSteps = [
  "گیرنده‌ها",
  "مخاطب‌های بله",
  "منبع پیام",
  "زمان‌بندی و محدودیت‌ها",
  "بررسی نهایی",
];

const blockingMessages = {
  campaign_has_no_deliverable_jobs: "جاب قابل ارسال وجود ندارد.",
  campaign_already_completed: "کمپین کامل شده است.",
  campaign_cancelled: "کمپین لغو شده است.",
  campaign_import_in_progress: "یک import برای این کمپین در حال انجام است.",
  source_channel_not_resolved: "کانال منبع قابل تشخیص نیست.",
  no_eligible_account: "اکانت واجد شرایط وجود ندارد.",
};

function persianImportError(error) {
  const detail = error?.data?.detail;
  const code = typeof detail === "object" ? detail.error_code : "";
  const messages = {
    invalid_phone: "شماره نامعتبر است.",
    duplicate_in_input: "شماره در فایل یا متن تکراری است.",
    duplicate_in_campaign: "شماره قبلا در همین کمپین وجود دارد.",
    unsupported_file_type: "نوع فایل پشتیبانی نمی‌شود.",
    file_too_large: "حجم فایل بیش از حد مجاز است.",
    missing_phone_column: "ستون شماره پیدا نشد. ستون شماره را انتخاب کنید.",
    malformed_csv: "فایل CSV نامعتبر است.",
    malformed_excel: "فایل Excel نامعتبر است.",
    batch_already_confirmed: "این batch قبلا تایید شده است.",
    no_valid_selected_rows: "هیچ ردیف معتبر انتخاب نشده است.",
    max_rows_exceeded: "تعداد ردیف‌ها بیش از حد مجاز است.",
  };
  return messages[code] || error?.message || "خطا در عملیات import";
}

function ImportModal({ campaign, onClose, onImported }) {
  const [tab, setTab] = useState("paste");
  const [content, setContent] = useState("");
  const [file, setFile] = useState(null);
  const [phoneColumn, setPhoneColumn] = useState("");
  const [nameColumn, setNameColumn] = useState("");
  const [sheetName, setSheetName] = useState("");
  const [columns, setColumns] = useState([]);
  const [sheets, setSheets] = useState([]);
  const [batch, setBatch] = useState(null);
  const [items, setItems] = useState([]);
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedMode, setSelectedMode] = useState("all");
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const limit = 100;

  async function loadItems(batchId = batch?.batch_id, nextOffset = offset, nextFilter = filter) {
    if (!batchId) return;
    const data = await listRecipientImportItems(batchId, { validation_status: nextFilter, limit, offset: nextOffset });
    setItems(data.items || []);
    setOffset(nextOffset);
  }

  async function handlePreview() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const preview = tab === "paste"
        ? await previewPasteImport(campaign.id, { import_source: "paste", content })
        : await uploadRecipientImport(campaign.id, file, { phone_column: phoneColumn, display_name_column: nameColumn, sheet_name: sheetName });
      setBatch(preview);
      setItems(preview.preview_items || []);
      setColumns(preview.metadata?.columns || preview.columns || []);
      setSheets(preview.metadata?.sheets || []);
      setFilter("");
      setOffset(0);
      setSelectedMode("all");
      setSelectedIds(new Set());
    } catch (err) {
      const detail = err?.data?.detail;
      setColumns(detail?.details?.columns || []);
      setSheets(detail?.details?.sheets || []);
      setError(persianImportError(err));
    } finally {
      setBusy(false);
    }
  }

  async function changeFilter(nextFilter) {
    setFilter(nextFilter);
    await loadItems(batch?.batch_id, 0, nextFilter);
  }

  function toggleItem(itemId) {
    const next = new Set(selectedIds);
    if (next.has(itemId)) next.delete(itemId);
    else next.add(itemId);
    setSelectedMode("explicit");
    setSelectedIds(next);
  }

  function selectAllVisibleValid() {
    setSelectedMode("explicit");
    setSelectedIds(new Set(items.filter((item) => item.validation_status === "valid").map((item) => item.id)));
  }

  async function confirmImport() {
    setBusy(true);
    setError(null);
    try {
      const payload = selectedMode === "all"
        ? { include_valid: true, selected_item_ids: null, default_priority: 0, scheduled_at: null, authorize_for_live_execution: false }
        : { include_valid: true, selected_item_ids: Array.from(selectedIds), default_priority: 0, scheduled_at: null, authorize_for_live_execution: false };
      if (selectedMode === "explicit" && selectedIds.size === 0) {
        setError("هیچ ردیف معتبری انتخاب نشده است.");
        return;
      }
      const confirmed = await confirmRecipientImport(batch.batch_id, payload);
      setResult(confirmed);
      await onImported();
      await loadItems(batch.batch_id, 0, filter);
    } catch (err) {
      setError(persianImportError(err));
    } finally {
      setBusy(false);
    }
  }

  async function cancelBatch() {
    setBusy(true);
    setError(null);
    try {
      if (batch?.batch_id) await deleteRecipientImport(batch.batch_id);
      onClose();
    } catch (err) {
      setError(persianImportError(err));
    } finally {
      setBusy(false);
    }
  }

  const selectedCount = selectedMode === "all" ? batch?.valid_count || 0 : selectedIds.size;

  return (
    <Modal title={`افزودن شماره‌ها - ${campaign.name}`} onClose={onClose} wide>
      {error ? <div className="error-state">{error}</div> : null}
      {result ? <div className="toast">Import انجام شد: {result.created_recipient_count} مخاطب و {result.created_job_count} جاب ساخته شد.</div> : null}
      {!batch ? (
        <>
          <div className="tab-bar">
            <button className={`tab-button ${tab === "paste" ? "active" : ""}`} type="button" onClick={() => setTab("paste")}>ورود دستی</button>
            <button className={`tab-button ${tab === "csv" ? "active" : ""}`} type="button" onClick={() => setTab("csv")}>فایل CSV</button>
            <button className={`tab-button ${tab === "excel" ? "active" : ""}`} type="button" onClick={() => setTab("excel")}>فایل Excel</button>
          </div>
          {tab === "paste" ? (
            <div className="settings-grid import-form">
              <label className="full-span">شماره‌ها
                <textarea rows="12" value={content} onChange={(event) => setContent(event.target.value)} placeholder="هر شماره در یک خط، یا جدا شده با کاما، تب و ;" />
              </label>
              <div className="full-span import-note">تعداد آیتم‌های تقریبی: {content.split(/[\r\n,;\t،؛]+/).filter(Boolean).length}</div>
            </div>
          ) : (
            <div className="settings-grid import-form">
              <label className="full-span">فایل
                <input type="file" accept={tab === "csv" ? ".csv,text/csv" : ".xlsx"} onChange={(event) => setFile(event.target.files?.[0] || null)} />
              </label>
              {file ? <div className="full-span import-note">{file.name} - {Math.round(file.size / 1024)} KB</div> : null}
              {sheets.length ? <label>Sheet<select value={sheetName} onChange={(event) => setSheetName(event.target.value)}><option value="">اولین sheet</option>{sheets.map((sheet) => <option key={sheet} value={sheet}>{sheet}</option>)}</select></label> : null}
              {columns.length ? (
                <>
                  <label>ستون شماره<select value={phoneColumn} onChange={(event) => setPhoneColumn(event.target.value)}><option value="">تشخیص خودکار</option>{columns.map((column) => <option key={column} value={column}>{column}</option>)}</select></label>
                  <label>ستون نام<select value={nameColumn} onChange={(event) => setNameColumn(event.target.value)}><option value="">بدون نام</option>{columns.map((column) => <option key={column} value={column}>{column}</option>)}</select></label>
                </>
              ) : null}
            </div>
          )}
          <div className="modal-actions">
            <button className="primary-button" type="button" disabled={busy || (tab === "paste" ? !content.trim() : !file)} onClick={handlePreview}>
              <Upload size={16} />
              پیش‌نمایش
            </button>
            <button className="secondary-button" type="button" onClick={onClose}>انصراف</button>
          </div>
        </>
      ) : (
        <>
          <div className="commercial-metrics import-metrics">
            <article className="metric-card"><span>submitted</span><strong>{batch.submitted_count}</strong></article>
            <article className="metric-card"><span>valid</span><strong>{batch.valid_count}</strong></article>
            <article className="metric-card"><span>invalid</span><strong>{batch.invalid_count}</strong></article>
            <article className="metric-card"><span>duplicate</span><strong>{batch.duplicate_count}</strong></article>
            <article className="metric-card"><span>selected</span><strong>{selectedCount}</strong></article>
          </div>
          <div className="filter-bar">
            <select value={filter} onChange={(event) => changeFilter(event.target.value)}>
              {importFilters.map((value) => <option key={value || "all"} value={value}>{value || "همه"}</option>)}
            </select>
            <button className="secondary-button" type="button" onClick={selectAllVisibleValid}>انتخاب معتبرهای صفحه</button>
            <button className="secondary-button" type="button" onClick={() => { setSelectedMode("explicit"); setSelectedIds(new Set()); }}>لغو انتخاب</button>
          </div>
          <div className="table-scroll">
            <table className="table rtl-table commercial-table import-table">
              <thead>
                <tr>
                  <th>انتخاب</th>
                  <th>row</th>
                  <th>raw phone</th>
                  <th>normalized</th>
                  <th>display name</th>
                  <th>status</th>
                  <th>reason</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td><input type="checkbox" disabled={item.validation_status !== "valid"} checked={selectedMode === "all" ? item.validation_status === "valid" : selectedIds.has(item.id)} onChange={() => toggleItem(item.id)} /></td>
                    <td>{item.row_number}</td>
                    <td>{fmt(item.phone_raw)}</td>
                    <td>{fmt(item.phone_normalized)}</td>
                    <td>{fmt(item.display_name)}</td>
                    <td><StatusBadge value={item.validation_status} /></td>
                    <td>{fmt(item.error_code || item.duplicate_reason || item.error_message)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager limit={limit} offset={offset} onPage={(nextOffset) => loadItems(batch.batch_id, nextOffset, filter)} />
          <div className="modal-actions">
            <button className="primary-button" type="button" disabled={busy || selectedCount === 0 || batch.status === "completed"} onClick={confirmImport}>تایید import</button>
            <button className="danger-button" type="button" disabled={busy || batch.status === "completed"} onClick={cancelBatch}>لغو batch</button>
            <button className="secondary-button" type="button" onClick={onClose}>بستن</button>
          </div>
        </>
      )}
    </Modal>
  );
}

export default function CommercialCampaigns() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", platform: "bale", status: "draft", source_channel_uid: "", deliveries_per_round: "", daily_limit_per_account: "", delay_between_deliveries_seconds: "", round_cooldown_seconds: "", operation_order: "save_contact,forward_message", send_method: "forward_latest_channel_message" });
  const [detail, setDetail] = useState(null);
  const [recipients, setRecipients] = useState([]);
  const [recipientScenarios, setRecipientScenarios] = useState([]);
  const [events, setEvents] = useState([]);
  const [detailValidation, setDetailValidation] = useState(null);
  const [importCampaign, setImportCampaign] = useState(null);
  const [busyCampaignId, setBusyCampaignId] = useState("");
  const [validationResult, setValidationResult] = useState(null);
  const [dryRunResult, setDryRunResult] = useState(null);
  const [effectivePolicy, setEffectivePolicy] = useState(null);
  const [liveReadiness, setLiveReadiness] = useState(null);
  const [finalReview, setFinalReview] = useState(null);
  const [livePreflight, setLivePreflight] = useState(null);
  const [liveApprovals, setLiveApprovals] = useState([]);
  const [liveBusy, setLiveBusy] = useState(false);
  const [configuration, setConfiguration] = useState(null);
  const [configurationRevisions, setConfigurationRevisions] = useState([]);
  const [configurationDraftText, setConfigurationDraftText] = useState("");
  const [configurationResult, setConfigurationResult] = useState(null);
  const [configurationBusy, setConfigurationBusy] = useState(false);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const data = await listCampaigns({ limit: 50, offset: 0 });
      setRows(data.items || []);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }

  async function create() {
    setError(null);
    try {
      const policy_overrides = {
        deliveries_per_round: form.deliveries_per_round === "" ? null : Number(form.deliveries_per_round),
        daily_limit_per_account: form.daily_limit_per_account === "" ? null : Number(form.daily_limit_per_account),
        delay_between_deliveries_seconds: form.delay_between_deliveries_seconds === "" ? null : Number(form.delay_between_deliveries_seconds),
        round_cooldown_seconds: form.round_cooldown_seconds === "" ? null : Number(form.round_cooldown_seconds),
        operation_order: form.operation_order.split(",").map((item) => item.trim()).filter(Boolean),
        send_method: form.send_method || null,
      };
      await createCampaign({ name: form.name, platform: form.platform, status: form.status, source_channel_uid: form.source_channel_uid || null, policy_overrides });
      setCreating(false);
      setForm({ name: "", platform: "bale", status: "draft", source_channel_uid: "", deliveries_per_round: "", daily_limit_per_account: "", delay_between_deliveries_seconds: "", round_cooldown_seconds: "", operation_order: "save_contact,forward_message", send_method: "forward_latest_channel_message" });
      await load();
    } catch (err) {
      setError(err);
    }
  }

  async function runLifecycleAction(campaign, label, action, destructive = false) {
    if (destructive && !window.confirm(`${label}؟`)) return;
    setBusyCampaignId(campaign.id);
    setError(null);
    setValidationResult(null);
    setDryRunResult(null);
    try {
      const result = await action(campaign.id);
      if (label.includes("اعتبارسنجی")) setValidationResult(result);
      if (label.includes("آزمایشی")) setDryRunResult(result);
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusyCampaignId("");
    }
  }

  async function openDetail(campaign) {
    setDetail(campaign);
    setRecipients([]);
    setRecipientScenarios([]);
    setEvents([]);
    setDetailValidation(null);
    setEffectivePolicy(null);
    setLiveReadiness(null);
    setLiveApprovals([]);
    setConfiguration(null);
    setConfigurationRevisions([]);
    setConfigurationDraftText("");
    setConfigurationResult(null);
    try {
      const [recipientData, scenarioData, eventData, validationData, policyData, approvalData, configData, revisionData] = await Promise.all([
        listCampaignRecipients(campaign.id, { limit: 50 }),
        listRecipientScenarios(campaign.id, { limit: 100 }),
        listEvents({ campaign_id: campaign.id, limit: 20 }),
        validateCampaignStart(campaign.id).catch((err) => err?.data?.detail?.validation || null),
        getEffectivePolicy({ campaign_id: campaign.id }),
        listLiveApprovals({ campaign_id: campaign.id, limit: 20 }),
        getCampaignConfiguration(campaign.id),
        listCampaignConfigurationRevisions(campaign.id),
      ]);
      const refreshed = rows.find((item) => item.id === campaign.id) || campaign;
      setDetail(refreshed);
      setRecipients(recipientData.items || []);
      setRecipientScenarios(scenarioData.items || []);
      setEvents(eventData.items || []);
      setDetailValidation(validationData);
      setEffectivePolicy(policyData);
      setLiveApprovals(approvalData.items || []);
      setConfiguration(configData);
      setConfigurationRevisions(revisionData.items || []);
      setConfigurationDraftText(JSON.stringify(configData?.effective?.resolved_configuration || {}, null, 2));
    } catch (err) {
      setError(err);
    }
  }

  async function refreshConfiguration() {
    if (!detail?.id) return;
    const [configData, revisionData] = await Promise.all([
      getCampaignConfiguration(detail.id),
      listCampaignConfigurationRevisions(detail.id),
    ]);
    setConfiguration(configData);
    setConfigurationRevisions(revisionData.items || []);
    setConfigurationDraftText(JSON.stringify(configData?.effective?.resolved_configuration || {}, null, 2));
  }

  async function configurationAction(action) {
    if (!detail?.id) return;
    setConfigurationBusy(true);
    setError(null);
    try {
      const result = await action();
      setConfigurationResult(result);
      await refreshConfiguration();
    } catch (err) {
      setError(err);
    } finally {
      setConfigurationBusy(false);
    }
  }

  function parsedConfigurationDraft() {
    return JSON.parse(configurationDraftText || "{}");
  }

  async function checkLiveReadiness() {
    if (!detail?.id) return;
    setLiveBusy(true);
    setError(null);
    try {
      const result = await validateLiveReadiness(detail.id, { account_ids: null, max_jobs: null });
      setLiveReadiness(result);
      const approvalData = await listLiveApprovals({ campaign_id: detail.id, limit: 20 });
      setLiveApprovals(approvalData.items || []);
    } catch (err) {
      setError(err);
    } finally {
      setLiveBusy(false);
    }
  }

  async function requestLiveApproval() {
    if (!detail?.id) return;
    let review = finalReview;
    if (!review?.final_review_hash) {
      review = await finalReviewCampaign(detail.id);
      setFinalReview(review);
    }
    const requestedBy = window.prompt("درخواست‌کننده تأیید ارسال");
    if (!requestedBy) return;
    setLiveBusy(true);
    setError(null);
    try {
      const approval = await requestSendApproval(detail.id, {
        final_review_hash: review.final_review_hash,
        requested_by: requestedBy,
        approval_scope: "campaign_send",
        explicit_confirmation: false,
      });
      setFinalReview(approval.final_review || review);
      const approvalData = await listLiveApprovals({ campaign_id: detail.id, limit: 20 });
      setLiveApprovals(approvalData.items || []);
    } catch (err) {
      setError(err);
    } finally {
      setLiveBusy(false);
    }
  }

  async function runFinalReview() {
    if (!detail?.id) return;
    setLiveBusy(true);
    setError(null);
    try {
      const review = await finalReviewCampaign(detail.id);
      setFinalReview(review);
    } catch (err) {
      setError(err);
    } finally {
      setLiveBusy(false);
    }
  }

  async function runLivePreflight() {
    if (!detail?.id) return;
    setLiveBusy(true);
    setError(null);
    try {
      const preflight = await livePreflightCampaign(detail.id, {});
      setLivePreflight(preflight);
    } catch (err) {
      setError(err);
    } finally {
      setLiveBusy(false);
    }
  }

  async function reloadAfterImport() {
    await load();
    if (detail) await openDetail(detail);
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <section className="rtl-page commercial-page">
      <PageHeader title="کمپین‌ها" description="تعریف کمپین، مشاهده شمارنده‌ها و افزودن شماره‌ها بدون شروع ارسال">
        <button className="primary-button" type="button" onClick={() => setCreating(true)}>
          <Plus size={16} />
          کمپین جدید
        </button>
        <button className="secondary-button" type="button" onClick={load}>
          <RotateCw size={16} />
          تازه‌سازی
        </button>
      </PageHeader>
      {error ? <ErrorState error={error} /> : null}
      {validationResult ? (
        <section className="panel lifecycle-result">
          <div className="panel-header"><h3 className="panel-title">نتیجه اعتبارسنجی شروع</h3></div>
          <div className="commercial-metrics import-metrics">
            <article className="metric-card"><span>deliverable</span><strong>{validationResult.deliverable_job_count}</strong></article>
            <article className="metric-card"><span>valid recipients</span><strong>{validationResult.valid_recipient_count}</strong></article>
            <article className="metric-card"><span>eligible accounts</span><strong>{validationResult.eligible_account_count}</strong></article>
            <article className="metric-card"><span>source</span><strong>{validationResult.source_channel_resolved ? "ok" : "blocked"}</strong></article>
          </div>
          <div className="ops-list">
            {(validationResult.blocking_reasons || []).length ? validationResult.blocking_reasons.map((reason) => (
              <p key={reason}>{blockingMessages[reason] || reason}</p>
            )) : <p>مانعی برای شروع وجود ندارد.</p>}
          </div>
        </section>
      ) : null}
      {dryRunResult ? (
        <section className="panel lifecycle-result">
          <div className="panel-header"><h3 className="panel-title">نتیجه بررسی بدون ارسال</h3></div>
          <pre className="diagnostics-pre">{JSON.stringify(dryRunResult, null, 2)}</pre>
        </section>
      ) : null}
      {loading ? (
        <LoadingState />
      ) : rows.length ? (
        <div className="table-scroll">
          <table className="table rtl-table commercial-table">
            <thead>
              <tr>
                <th>name</th>
                <th>platform</th>
                <th>status</th>
                <th>total</th>
                <th>queued</th>
                <th>running</th>
                <th>succeeded</th>
                <th>failed</th>
                <th>skipped</th>
                <th>cancelled</th>
                <th>created_at</th>
                <th>started_at</th>
                <th>completed_at</th>
                <th>کنترل</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((campaign) => (
                <tr key={campaign.id}>
                  <td>{campaign.name}</td>
                  <td>{campaign.platform}</td>
                  <td><StatusBadge value={campaign.status} /></td>
                  <td>{fmt(campaign.total_recipients)}</td>
                  <td>{fmt(campaign.queued_count)}</td>
                  <td>{fmt(campaign.running_count)}</td>
                  <td>{fmt(campaign.succeeded_count)}</td>
                  <td>{fmt(campaign.failed_count)}</td>
                  <td>{fmt(campaign.skipped_count)}</td>
                  <td>{fmt(campaign.cancelled_count)}</td>
                  <td>{fmt(campaign.created_at)}</td>
                  <td>{fmt(campaign.started_at)}</td>
                  <td>{fmt(campaign.completed_at)}</td>
                  <td className="row-actions">
                    <button className="icon-button" type="button" onClick={() => openDetail(campaign)} aria-label="مشاهده"><Eye size={15} /></button>
                    <button className="secondary-button" type="button" onClick={() => setImportCampaign(campaign)}>افزودن شماره‌ها</button>
                    {campaign.status === "draft" ? (
                      <>
                        <button className="secondary-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "اعتبارسنجی شروع", validateCampaignStart)}>اعتبارسنجی شروع</button>
                        <button className="primary-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "آماده‌سازی صف", queueCampaign)}>آماده‌سازی صف</button>
                        <button className="danger-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "لغو کمپین", cancelCampaign, true)}>لغو</button>
                      </>
                    ) : null}
                    {campaign.status === "queued" ? (
                      <>
                        <button className="primary-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "شروع", startCampaign)}>شروع</button>
                        <button className="danger-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "لغو کمپین", cancelCampaign, true)}>لغو</button>
                      </>
                    ) : null}
                    {campaign.status === "running" ? (
                      <>
                        <button className="secondary-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "بررسی بدون ارسال", checkCampaignWithoutSending)}>بررسی بدون ارسال</button>
                        <button className="secondary-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "توقف کمپین", pauseCampaign)}>توقف کمپین</button>
                        <button className="danger-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "لغو کمپین", cancelCampaign, true)}>لغو</button>
                      </>
                    ) : null}
                    {campaign.status === "paused" ? (
                      <>
                        <button className="primary-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "ادامه کمپین", resumeCampaign)}>ادامه کمپین</button>
                        <button className="danger-button" disabled={busyCampaignId === campaign.id} type="button" onClick={() => runLifecycleAction(campaign, "لغو کمپین", cancelCampaign, true)}>لغو</button>
                      </>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState />
      )}

      {creating ? (
        <Modal title="کمپین جدید" onClose={() => setCreating(false)}>
          <div className="settings-grid">
            <label>نام<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
            <label>پلتفرم<input value={form.platform} onChange={(event) => setForm({ ...form, platform: event.target.value })} /></label>
            <label>وضعیت<select value={form.status} onChange={(event) => setForm({ ...form, status: event.target.value })}>{statuses.map((status) => <option key={status} value={status}>{status}</option>)}</select></label>
            <label>source_channel_uid<input value={form.source_channel_uid} onChange={(event) => setForm({ ...form, source_channel_uid: event.target.value })} /></label>
            <label>ارسال در هر راند<input type="number" value={form.deliveries_per_round} onChange={(event) => setForm({ ...form, deliveries_per_round: event.target.value })} /></label>
            <label>سقف روزانه<input type="number" value={form.daily_limit_per_account} onChange={(event) => setForm({ ...form, daily_limit_per_account: event.target.value })} /></label>
            <label>تاخیر بین ارسال<input type="number" value={form.delay_between_deliveries_seconds} onChange={(event) => setForm({ ...form, delay_between_deliveries_seconds: event.target.value })} /></label>
            <label>cooldown راند<input type="number" value={form.round_cooldown_seconds} onChange={(event) => setForm({ ...form, round_cooldown_seconds: event.target.value })} /></label>
            <label>ترتیب عملیات<input value={form.operation_order} onChange={(event) => setForm({ ...form, operation_order: event.target.value })} /></label>
            <label>روش ارسال<input value={form.send_method} onChange={(event) => setForm({ ...form, send_method: event.target.value })} /></label>
          </div>
          <div className="modal-actions">
            <button className="primary-button" type="button" onClick={create} disabled={!form.name.trim()}>ایجاد</button>
            <button className="secondary-button" type="button" onClick={() => setCreating(false)}>انصراف</button>
          </div>
        </Modal>
      ) : null}

      {detail ? (
        <Modal title={`جزئیات کمپین ${shortId(detail.id, 24)}`} onClose={() => setDetail(null)} wide>
          <div className="wizard-steps" aria-label="گردش کار کمپین">
            {campaignWizardSteps.map((step, index) => (
              <span key={step} className="wizard-step">{index + 1}. {step}</span>
            ))}
          </div>
          <div className="toast">قبل از ذخیره مخاطب‌های بله یا ساخت Job، فهرست گیرنده‌ها باید از manifest ورودی تأییدشده آمده باشد. بررسی بدون ارسال فقط خواندنی است و Chrome یا adapter را اجرا نمی‌کند.</div>
          <KeyValueGrid data={detail} />
          {detailValidation ? (
            <>
              <h4 className="subheading">وضعیت شروع</h4>
              <KeyValueGrid data={{
                deliverable_job_count: detailValidation.deliverable_job_count,
                valid_recipient_count: detailValidation.valid_recipient_count,
                "تعداد مجاز برای اجرای واقعی": detailValidation.live_authorized_job_count,
                "تعداد بدون مجوز": detailValidation.unauthorized_job_count,
                "تعداد داده آزمایشی": detailValidation.synthetic_test_job_count,
                "Jobهای مسدودشده": (detailValidation.blocking_jobs || []).length,
                eligible_account_count: detailValidation.eligible_account_count,
                source_channel_resolved: detailValidation.source_channel_resolved,
                blocking_reasons: (detailValidation.blocking_reasons || []).map((reason) => blockingMessages[reason] || reason).join(" | "),
              }} />
            </>
          ) : null}
          <h4 className="subheading">کنترل اجرای واقعی</h4>
          <div className="toast">«اجرای واقعی تا فعال‌شدن قابلیت و تأیید نهایی غیرفعال است.»</div>
          <div className="row-actions">
            <button className="secondary-button" type="button" disabled={liveBusy} onClick={checkLiveReadiness}>بررسی آمادگی اجرای واقعی</button>
            <button className="secondary-button" type="button" disabled={liveBusy} onClick={runFinalReview}>بررسی نهایی</button>
            <button className="secondary-button" type="button" disabled={liveBusy || !finalReview?.final_review_hash} onClick={requestLiveApproval}>درخواست تأیید ارسال</button>
            <button className="secondary-button" type="button" disabled={liveBusy} onClick={runLivePreflight}>پیش‌بررسی اجرای تک‌گیرنده</button>
          </div>
          {finalReview ? (
            <KeyValueGrid data={{
              "بررسی نهایی": finalReview.validation?.ok ? "آماده ثبت تأیید" : "دارای خطا",
              final_review_hash: finalReview.final_review_hash,
              "تعداد گیرنده‌ها": finalReview.confirmed_recipients_summary?.recipient_count,
              "منبع پیام": finalReview.source?.source_uid,
              "اجرای واقعی": "غیرفعال",
            }} />
          ) : null}
          {livePreflight ? (
            <KeyValueGrid data={{
              recipient_count: livePreflight.recipient_summary?.recipient_count,
              scenario_count: livePreflight.recipient_summary?.scenario_count,
              platform_run_count: livePreflight.recipient_summary?.platform_run_count,
              "Source": JSON.stringify(livePreflight.source_summary || {}),
              "Accounts": JSON.stringify(livePreflight.account_summary || {}),
              "Manifest": livePreflight.manifest_summary?.manifest_confirmed ? "confirmed" : "missing",
              "Snapshot": livePreflight.snapshot_summary?.snapshot_id ? "present" : "missing",
              "Approval": livePreflight.approval_summary?.approval_status || "missing",
              "Duplicate safety": livePreflight.duplicate_send_history?.duplicate_scope_found ? "blocked" : "clear",
              "Execution feature": livePreflight.execution_feature_status,
              "Ready for live execution": livePreflight.execute_allowed ? "yes" : "no / pending explicit confirmation",
            }} />
          ) : null}
          <h4 className="subheading">Unified recipient scenarios</h4>
          <KeyValueGrid data={{
            scenario_count: recipientScenarios.length,
            platform_run_count: recipientScenarios.reduce((sum, item) => sum + (item.selected_platforms || []).length, 0),
            execution_disabled: true,
          }} />
          {liveReadiness ? (
            <KeyValueGrid data={{
              ready: liveReadiness.ready,
              "تعداد Job مجاز": liveReadiness.live_authorized_job_count,
              "تعداد Job مسدود": (liveReadiness.blocking_jobs || []).length,
              "تعداد بدون مجوز": liveReadiness.unauthorized_job_count,
              "تعداد داده آزمایشی": liveReadiness.synthetic_job_count,
              "دلایل مسدودشدن": (liveReadiness.blocking_reasons || []).join(" | "),
              "اکانت‌های واجد شرایط": (liveReadiness.eligible_account_ids || []).join(", "),
              "ظرفیت روزانه": JSON.stringify(liveReadiness.account_daily_capacity || {}),
              "وضعیت Feature Flag": JSON.stringify(liveReadiness.feature_flags || {}),
              estimated_jobs_this_run: liveReadiness.estimated_jobs_this_run,
            }} />
          ) : null}
          <h4 className="subheading">فهرست Approvalها</h4>
          <div className="compact-list">
            {liveApprovals.length ? liveApprovals.map((approval) => (
              <div key={approval.approval_id} className="compact-row">
                <strong>{shortId(approval.approval_id, 18)}</strong>
                <StatusBadge value={approval.approval_status} />
                <span>درخواست‌کننده: {fmt(approval.requested_by)}</span>
                <span>تأییدکننده: {fmt(approval.approved_by)}</span>
                <span>max_jobs: {fmt(approval.requested_max_jobs)}</span>
                <span>انقضا: {fmt(approval.expires_at)}</span>
              </div>
            )) : <EmptyState />}
          </div>
          {effectivePolicy ? (
            <>
              <h4 className="subheading">سیاست موثر</h4>
              <pre className="diagnostics-pre">{JSON.stringify({
                campaign_overrides: effectivePolicy.campaign_overrides,
                effective_policy: effectivePolicy.effective_policy,
                policy_resolution_source: effectivePolicy.policy_resolution_source,
              }, null, 2)}</pre>
            </>
          ) : null}
          <h4 className="subheading">تنظیمات نسخه‌بندی‌شده کمپین</h4>
          <div className="toast">منبع پیام و سیاست اجرا برای هر کمپین مستقل است. قفل‌کردن نسخه فقط snapshot تنظیمات را می‌سازد و دکمه ارسال واقعی نیست.</div>
          <div className="configuration-sections">
            {["منبع پیام", "اکانت‌ها", "ظرفیت و هم‌زمانی", "زمان‌بندی", "محدودیت ارسال", "سیاست خطا و Retry", "گیرندگان و مجوزها", "تنظیمات پلتفرم", "تنظیمات هوش مصنوعی", "Feature Flags"].map((item) => (
              <span key={item} className="config-section-chip">{item}</span>
            ))}
          </div>
          <div className="row-actions">
            <button className="secondary-button" type="button" disabled={configurationBusy} onClick={() => configurationAction(() => getCampaignConfiguration(detail.id))}>پیش‌نمایش تنظیمات مؤثر</button>
            <button className="secondary-button" type="button" disabled={configurationBusy} onClick={() => configurationAction(() => validateCampaignConfiguration(detail.id))}>اعتبارسنجی تنظیمات</button>
            <button className="secondary-button" type="button" disabled={configurationBusy} onClick={() => configurationAction(() => createCampaignConfigurationRevision(detail.id, { configuration: parsedConfigurationDraft(), change_summary: "UI draft revision", created_by: "user" }))}>ایجاد نسخه جدید</button>
            <button className="secondary-button" type="button" disabled={configurationBusy} onClick={() => configurationAction(() => checkCampaignConfigurationDrift(detail.id))}>بررسی تغییر تنظیمات پس از تأیید</button>
            {configurationRevisions.length ? (
              <button className="secondary-button" type="button" disabled={configurationBusy} onClick={() => configurationAction(() => approveCampaignConfigurationRevision(detail.id, configurationRevisions[configurationRevisions.length - 1].revision_id, { approved_by: "user" }))}>قفل‌کردن نسخه برای تأیید</button>
            ) : null}
          </div>
          <textarea className="configuration-editor" value={configurationDraftText} onChange={(event) => setConfigurationDraftText(event.target.value)} />
          {configuration?.effective ? (
            <div className="table-scroll">
              <table className="table rtl-table commercial-table">
                <thead>
                  <tr>
                    <th>فیلد</th>
                    <th>مقدار مؤثر</th>
                    <th>منشأ</th>
                    <th>وضعیت اعتبارسنجی</th>
                    <th>پیش‌فرض</th>
                    <th>حیاتی</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(configuration.effective.origin_trace?.fields || {}).slice(0, 80).map(([field, trace]) => (
                    <tr key={field}>
                      <td>{field}</td>
                      <td>{JSON.stringify(trace.final_value)}</td>
                      <td>{trace.source_layer}</td>
                      <td>{trace.validation_status}</td>
                      <td>{String(Boolean(trace.default_used))}</td>
                      <td>{String(Boolean(trace.critical))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <h4 className="subheading">تاریخچه نسخه‌ها</h4>
          <div className="compact-list">
            {configurationRevisions.length ? configurationRevisions.map((revision) => (
              <div key={revision.revision_id} className="compact-row">
                <strong>{revision.revision_number}</strong>
                <StatusBadge value={revision.status} />
                <span>{shortId(revision.revision_id, 18)}</span>
                <span>hash: {shortId(revision.resolved_configuration_hash, 16)}</span>
                <span>approved: {fmt(revision.approved_at)}</span>
              </div>
            )) : <EmptyState />}
          </div>
          {configurationResult ? <pre className="diagnostics-pre">{JSON.stringify(configurationResult, null, 2)}</pre> : null}
          <h4 className="subheading">recipientها</h4>
          <div className="compact-list">
            {recipients.length ? recipients.map((recipient) => (
              <div key={recipient.id} className="compact-row">
                <strong>{fmt(recipient.phone_normalized)}</strong>
                <span>{fmt(recipient.display_name)}</span>
                <StatusBadge value={recipient.validation_status} />
                <span>منبع گیرنده: {fmt(recipient.recipient_origin)}</span>
                <span>داده آزمایشی: {fmt(Boolean(recipient.synthetic_test_data))}</span>
                <span>مجوز اجرای واقعی: {fmt(Boolean(recipient.live_execution_authorized))}</span>
                <span>وضعیت مجوز: {fmt(recipient.authorization_status)}</span>
                <span>زمان تأیید: {fmt(recipient.live_authorized_at)}</span>
                <span>تأییدکننده: {fmt(recipient.live_authorized_by)}</span>
                <span>یادداشت مجوز: {fmt(recipient.authorization_note)}</span>
              </div>
            )) : <EmptyState />}
          </div>
          <h4 className="subheading">رویدادهای اخیر</h4>
          <div className="timeline">
            {events.length ? events.map((event) => (
              <div key={event.id} className="timeline-item">
                <StatusBadge value={event.status} />
                <strong>{event.event_type}</strong>
                <span>{fmt(event.step_name)}</span>
                <p>{fmt(event.message || event.error_message)}</p>
              </div>
            )) : <EmptyState />}
          </div>
        </Modal>
      ) : null}

      {importCampaign ? (
        <ImportModal campaign={importCampaign} onClose={() => setImportCampaign(null)} onImported={reloadAfterImport} />
      ) : null}
    </section>
  );
}
