import { AlertTriangle, CheckCircle2, FileCheck2, Play, RefreshCw, Search, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { checkCampaignWithoutSending, finalReviewCampaign, listCampaigns, queueCampaign, validateCampaignStart } from "../api/campaigns";
import {
  Checkbox,
  ContentCard,
  DangerButton,
  EmptyState,
  FormField,
  InlineError,
  LoadingState,
  Modal,
  PageHeader,
  PrimaryButton,
  SecondaryButton,
  StatusBadge,
  TextInput,
} from "../components/ui/DesignSystem.jsx";

const QUEUE_ERROR_CODES = new Set([
  "validation_not_ok",
  "manifest_missing",
  "manifest_stale",
  "authorization_incomplete",
  "dry_run_evidence_missing",
  "dry_run_evidence_stale",
  "final_review_missing",
  "final_review_stale",
  "limit_exceeded",
  "account_context_missing",
  "operator_confirmation_required",
  "idempotency_key_required",
  "duplicate_queue_request",
  "campaign_status_mismatch",
]);

function statusTone(status) {
  const value = String(status || "").toLowerCase();
  if (["ready", "active", "running", "queued", "succeeded", "completed"].includes(value)) return "success";
  if (["failed", "cancelled", "error"].includes(value)) return "danger";
  if (["paused", "draft", "pending"].includes(value)) return "warning";
  return "neutral";
}

function statusLabel(status) {
  const labels = {
    draft: "Draft",
    pending: "Pending",
    ready: "Ready",
    queued: "Queued",
    running: "Running",
    paused: "Paused",
    completed: "Completed",
    succeeded: "Succeeded",
    failed: "Failed",
    cancelled: "Cancelled",
  };
  return labels[status] || status || "Unknown";
}

function campaignTitle(campaign) {
  return campaign?.name || campaign?.title || campaign?.campaign_name || campaign?.id || campaign?.campaign_id || "Untitled campaign";
}

function campaignId(campaign) {
  return campaign?.id || campaign?.campaign_id || "";
}

function compact(value) {
  if (value === undefined || value === null || value === "") return "Not returned";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  return String(value);
}

function asNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function getValidationHash(validation) {
  return validation?.validation_hash || validation?.validation_id || "";
}

function getValidationBlockers(validation) {
  const direct = validation?.blocking_reasons || validation?.blockers || [];
  if (Array.isArray(direct) && direct.length) return direct;
  const errors = validation?.errors || validation?.validation_errors || [];
  if (Array.isArray(errors)) return errors.map((item) => item?.error_code || item?.message || JSON.stringify(item));
  return [];
}

function getValidationWarnings(validation) {
  const warnings = validation?.warnings || [];
  if (Array.isArray(warnings)) return warnings.map((item) => item?.warning_code || item?.message || JSON.stringify(item));
  return [];
}

function getAuthorizationSummary(validation, checkEvidence) {
  const fromCheck = checkEvidence?.diagnostics?.recipient_authorization || {};
  return {
    liveAuthorized: asNumber(validation?.live_authorized_job_count ?? fromCheck.live_authorized_job_count),
    liveEligible: asNumber(validation?.live_eligible_job_count ?? fromCheck.live_eligible_job_count),
    unauthorized: asNumber(validation?.unauthorized_job_count ?? fromCheck.unauthorized_job_count),
    synthetic: asNumber(validation?.synthetic_test_job_count ?? fromCheck.synthetic_test_job_count),
    revoked: asNumber(validation?.revoked_authorization_job_count ?? fromCheck.revoked_authorization_job_count),
  };
}

function getCheckId(checkEvidence) {
  return checkEvidence?.audit?.dry_run_id || checkEvidence?.dry_run_id || checkEvidence?.audit?.check_id || checkEvidence?.check_id || "";
}

function getCheckValidationHash(checkEvidence) {
  return getValidationHash(checkEvidence?.diagnostics?.validation || checkEvidence?.validation || {});
}

function checkIsNonMutating(checkEvidence) {
  if (!checkEvidence) return false;
  const forbidden = checkEvidence?.diagnostics?.forbidden_actions || {};
  const forbiddenValues = Object.values(forbidden);
  return (
    checkEvidence?.dry_run === true &&
    checkEvidence?.diagnostics?.read_only === true &&
    checkEvidence?.audit?.status === "completed" &&
    checkEvidence?.audit?.forbidden_mutation_detected !== true &&
    forbiddenValues.length > 0 &&
    forbiddenValues.every((value) => value === false)
  );
}

function getManifest(review) {
  return review?.confirmed_recipients_summary || {};
}

function getEffectiveLimit(review) {
  return asNumber(review?.limits?.max_jobs_per_execution ?? review?.limits?.deliveries_per_round);
}

function hasAccountSourceContext(validation, review) {
  const sourceOk = validation?.source_channel_resolved === true || Boolean(review?.source?.source_uid);
  const accounts = review?.accounts?.allowed_account_ids || [];
  return sourceOk && Array.isArray(accounts) && accounts.length > 0;
}

function createIdempotencyKey(campaignId) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `campaign-queue:${campaignId}:${random}`;
}

function extractApiError(err) {
  const detail = err?.data?.detail;
  const code = detail?.error_code || detail?.summary?.error_code || err?.data?.error_code || "";
  const message = detail?.message || err?.message || "Request failed";
  return { code, message };
}

function EvidenceRow({ label, value, tone }) {
  return (
    <p className="detail-row">
      <span>{label}</span>
      <b>{tone ? <StatusBadge tone={tone}>{compact(value)}</StatusBadge> : compact(value)}</b>
    </p>
  );
}

function EvidenceList({ items }) {
  const visible = items.filter(Boolean);
  if (!visible.length) return <p className="page-copy">No blockers returned.</p>;
  return (
    <ul className="page-copy">
      {visible.map((item, index) => <li key={`${item}-${index}`}>{compact(item)}</li>)}
    </ul>
  );
}

export default function CampaignExecutionEntry({ onNavigate }) {
  const [campaigns, setCampaigns] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState(null);
  const [message, setMessage] = useState("");
  const [validation, setValidation] = useState(null);
  const [checkEvidence, setCheckEvidence] = useState(null);
  const [review, setReview] = useState(null);
  const [queuedResult, setQueuedResult] = useState(null);
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [operatorConfirmed, setOperatorConfirmed] = useState(false);

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("fa-IR");
    if (!needle) return campaigns;
    return campaigns.filter((campaign) => `${campaignTitle(campaign)} ${campaignId(campaign)} ${campaign.status || ""}`.toLocaleLowerCase("fa-IR").includes(needle));
  }, [campaigns, query]);

  const selected = campaigns.find((campaign) => campaignId(campaign) === selectedId) || null;
  const selectedDraft = selected?.status === "draft";
  const blockers = getValidationBlockers(validation);
  const warnings = getValidationWarnings(validation);
  const authorization = getAuthorizationSummary(validation, checkEvidence);
  const manifest = getManifest(review);
  const deliverableCount = asNumber(validation?.deliverable_job_count);
  const effectiveLimit = getEffectiveLimit(review);
  const validationEvidence = getValidationHash(validation);
  const checkEvidenceId = getCheckId(checkEvidence);
  const finalReviewHash = review?.final_review_hash || "";
  const manifestHash = manifest?.manifest_hash || "";
  const accountSourceReady = hasAccountSourceContext(validation, review);
  const authorizationComplete = [authorization.unauthorized, authorization.synthetic, authorization.revoked].every((value) => value === 0);
  const limitReady = effectiveLimit !== null && deliverableCount !== null && deliverableCount <= effectiveLimit;
  const evidenceNonMutating = checkIsNonMutating(checkEvidence);
  const queueDisabledReasons = [
    !selected && "Select a campaign explicitly.",
    selected && !selectedDraft && "Campaign status must be draft.",
    !validation && "Run validation.",
    validation && validation.ok !== true && "Validation must pass.",
    validation && !validationEvidence && "Validation hash or ID was not returned.",
    review && !manifest?.manifest_confirmed && "Confirmed manifest was not returned.",
    review && !manifestHash && "Manifest hash was not returned.",
    validation && !authorizationComplete && "Recipient authorization is incomplete.",
    !checkEvidence && "Run check without sending.",
    checkEvidence && !evidenceNonMutating && "Check evidence is missing non-mutating proof.",
    checkEvidence && !checkEvidenceId && "Dry-run/check ID was not returned.",
    !review && "Run final review.",
    review && !finalReviewHash && "Final-review hash was not returned.",
    validation && review && !accountSourceReady && "Account/source context is missing.",
    review && !limitReady && "Effective limit is missing or exceeded.",
    blockers.length > 0 && "Blocking validation reasons remain.",
    Boolean(busy) && "A request is in progress.",
    queuedResult && "Campaign is already queued from this page.",
  ].filter(Boolean);
  const canQueue = queueDisabledReasons.length === 0;

  function clearEvidence() {
    setValidation(null);
    setCheckEvidence(null);
    setReview(null);
    setQueuedResult(null);
    setConfirmationOpen(false);
    setOperatorConfirmed(false);
  }

  async function load({ preserveSelection = true } = {}) {
    setLoading(true);
    setError(null);
    try {
      const payload = await listCampaigns({ limit: 100, offset: 0 });
      const rows = Array.isArray(payload) ? payload : payload?.items || [];
      setCampaigns(rows);
      if (!preserveSelection) return;
      setSelectedId((current) => {
        if (!current) return "";
        const next = rows.find((campaign) => campaignId(campaign) === current);
        if (!next) {
          clearEvidence();
          return "";
        }
        const currentCampaign = campaigns.find((campaign) => campaignId(campaign) === current);
        if (
          currentCampaign &&
          (
            currentCampaign.status !== next.status ||
            currentCampaign.updated_at !== next.updated_at ||
            currentCampaign.completed_at !== next.completed_at ||
            currentCampaign.paused_at !== next.paused_at
          )
        ) {
          clearEvidence();
        }
        return current;
      });
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setLoading(false);
    }
  }

  function selectCampaign(id) {
    setSelectedId(id);
    clearEvidence();
    setError(null);
    setMessage("");
  }

  async function runValidation() {
    if (!selectedId || busy) return;
    setBusy("validate");
    setError(null);
    setMessage("");
    setValidation(null);
    setCheckEvidence(null);
    setReview(null);
    setQueuedResult(null);
    try {
      const result = await validateCampaignStart(selectedId);
      setValidation(result);
      setMessage("Validation completed.");
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy("");
    }
  }

  async function runCheck() {
    if (!selectedId || busy || !validation) return;
    setBusy("check");
    setError(null);
    setMessage("");
    setCheckEvidence(null);
    setReview(null);
    try {
      const result = await checkCampaignWithoutSending(selectedId);
      setCheckEvidence(result);
      setMessage("Check without sending completed.");
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy("");
    }
  }

  async function runFinalReview() {
    if (!selectedId || busy || !validation || !checkEvidence) return;
    setBusy("review");
    setError(null);
    setMessage("");
    setReview(null);
    try {
      const result = await finalReviewCampaign(selectedId);
      setReview(result);
      setMessage("Final review evidence captured.");
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy("");
    }
  }

  function openConfirmation() {
    if (!canQueue) return;
    setOperatorConfirmed(false);
    setConfirmationOpen(true);
  }

  async function confirmQueue() {
    if (!canQueue || !operatorConfirmed || busy || !selectedId) return;
    setBusy("queue");
    setError(null);
    setMessage("");
    try {
      const payload = {
        validation_hash: validation?.validation_hash,
        validation_id: validation?.validation_id,
        dry_run_id: checkEvidence?.audit?.dry_run_id || checkEvidence?.dry_run_id,
        check_id: checkEvidence?.audit?.check_id || checkEvidence?.check_id,
        final_review_hash: finalReviewHash,
        manifest_hash: manifestHash,
        approval_id: review?.approval_id || review?.approval?.approval_id,
        idempotency_key: createIdempotencyKey(selectedId),
        explicit_operator_confirmation: true,
        expected_campaign_status: "draft",
      };
      const result = await queueCampaign(selectedId, payload);
      setQueuedResult(result);
      setConfirmationOpen(false);
      setOperatorConfirmed(false);
      setMessage(`Queued. execution_started=${compact(result?.execution_started)}`);
      await load();
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    load({ preserveSelection: false });
  }, []);

  return (
    <section className="campaign-entry-page" dir="rtl">
      <PageHeader
        title="Campaign Execution Entry"
        description="Queueing is locked behind explicit selection, validation, check-without-sending evidence, final review, and a final operator confirmation."
        actions={<SecondaryButton onClick={() => load()} disabled={loading || Boolean(busy)}><RefreshCw size={17} />Refresh</SecondaryButton>}
      />

      {error ? <InlineError>{QUEUE_ERROR_CODES.has(error.code) ? `${error.code}: ${error.message}` : error.message}</InlineError> : null}
      {message ? <div className="toast">{message}</div> : null}

      <ContentCard title="Campaign Selection" description="No campaign is selected automatically. Choose one campaign before running any action.">
        <div className="campaign-entry-toolbar">
          <FormField label="Search campaigns">
            <TextInput value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name, ID, or status" />
          </FormField>
          <SecondaryButton onClick={() => onNavigate?.("campaigns")}>Campaign management</SecondaryButton>
        </div>

        {loading ? <LoadingState label="Loading campaigns" /> : null}
        {!loading && !filtered.length ? <EmptyState title="No campaigns found" description="Create or adjust filters before selecting a campaign." /> : null}

        {!loading && filtered.length ? (
          <div className="campaign-entry-list">
            {filtered.map((campaign) => {
              const id = campaignId(campaign);
              return (
                <button className={selectedId === id ? "selected" : ""} key={id} onClick={() => selectCampaign(id)} type="button">
                  <span>
                    <strong>{campaignTitle(campaign)}</strong>
                    <small>{id}</small>
                  </span>
                  <StatusBadge tone={statusTone(campaign.status)}>{statusLabel(campaign.status)}</StatusBadge>
                </button>
              );
            })}
          </div>
        ) : null}
      </ContentCard>

      {!selected ? (
        <ContentCard title="Execution Gate">
          <EmptyState title="Select a campaign" description="Queue controls are disabled until a campaign is explicitly selected." />
        </ContentCard>
      ) : (
        <ContentCard title="Execution Gate" actions={<StatusBadge tone={statusTone(selected.status)}>{statusLabel(selected.status)}</StatusBadge>}>
          <div className="settings-modal-grid">
            <EvidenceRow label="Campaign ID" value={campaignId(selected)} />
            <EvidenceRow label="Name" value={campaignTitle(selected)} />
            <EvidenceRow label="Status" value={selected.status} tone={selectedDraft ? "warning" : "danger"} />
            <EvidenceRow label="Platform" value={selected.platform} />
            <EvidenceRow label="Created" value={selected.created_at} />
            <EvidenceRow label="Updated" value={selected.updated_at} />
          </div>
          {!selectedDraft ? <InlineError>Campaign status must be draft before queueing.</InlineError> : null}

          <div className="campaign-execution-actions">
            <PrimaryButton disabled={!selectedId || !selectedDraft || Boolean(busy) || Boolean(queuedResult)} onClick={runValidation}>
              <CheckCircle2 size={17} />
              Run validation
            </PrimaryButton>
            <SecondaryButton disabled={!selectedId || !selectedDraft || !validation || Boolean(busy) || Boolean(queuedResult)} onClick={runCheck}>
              <Search size={17} />
              Check without sending
            </SecondaryButton>
            <SecondaryButton disabled={!selectedId || !selectedDraft || !validation || !checkEvidence || Boolean(busy) || Boolean(queuedResult)} onClick={runFinalReview}>
              <FileCheck2 size={17} />
              Final review
            </SecondaryButton>
            <DangerButton disabled={!canQueue} onClick={openConfirmation}>
              <Play size={17} />
              Queue campaign
            </DangerButton>
          </div>

          <div className="settings-modal-grid">
            <ContentCard title="Validation">
              <EvidenceRow label="OK" value={validation?.ok} tone={validation?.ok ? "success" : "danger"} />
              <EvidenceRow label="Validation hash/ID" value={validationEvidence} />
              <EvidenceRow label="Deliverable recipients" value={deliverableCount} />
              <EvidenceRow label="Eligible accounts" value={validation?.eligible_account_count} />
              <EvidenceRow label="Source resolved" value={validation?.source_channel_resolved} />
              <EvidenceRow label="Source UID" value={review?.source?.source_uid || validation?.source_channel_resolution?.source_channel_uid} />
              <EvidenceRow label="Authorized jobs" value={authorization.liveAuthorized} />
              <EvidenceRow label="Unauthorized jobs" value={authorization.unauthorized} />
              <EvidenceRow label="Synthetic jobs" value={authorization.synthetic} />
              <EvidenceRow label="Revoked jobs" value={authorization.revoked} />
              <EvidenceList items={blockers} />
              <EvidenceList items={warnings} />
            </ContentCard>

            <ContentCard title="Check Evidence">
              <EvidenceRow label="Evidence ID" value={checkEvidenceId} />
              <EvidenceRow label="Validation evidence" value={getCheckValidationHash(checkEvidence)} />
              <EvidenceRow label="Read only" value={checkEvidence?.diagnostics?.read_only} tone={checkEvidence?.diagnostics?.read_only ? "success" : "danger"} />
              <EvidenceRow label="Audit status" value={checkEvidence?.audit?.status} />
              <EvidenceRow label="Non-mutating" value={evidenceNonMutating} tone={evidenceNonMutating ? "success" : "danger"} />
              <EvidenceRow label="Browser launched" value={checkEvidence?.diagnostics?.forbidden_actions?.browser_launched} />
              <EvidenceRow label="Adapter called" value={checkEvidence?.diagnostics?.forbidden_actions?.adapter_called} />
              <EvidenceRow label="Message sent" value={checkEvidence?.diagnostics?.forbidden_actions?.message_sent} />
            </ContentCard>

            <ContentCard title="Final Review">
              <EvidenceRow label="Final-review hash" value={finalReviewHash} />
              <EvidenceRow label="Manifest confirmed" value={manifest?.manifest_confirmed} tone={manifest?.manifest_confirmed ? "success" : "danger"} />
              <EvidenceRow label="Manifest hash" value={manifestHash} />
              <EvidenceRow label="Recipient count" value={manifest?.recipient_count} />
              <EvidenceRow label="Effective limit" value={effectiveLimit} />
              <EvidenceRow label="Allowed accounts" value={review?.accounts?.allowed_account_ids} />
              <EvidenceRow label="Source UID" value={review?.source?.source_uid} />
              <EvidenceRow label="Review OK" value={review?.validation?.ok} tone={review?.validation?.ok ? "success" : "danger"} />
            </ContentCard>
          </div>

          <ContentCard title="Queue Readiness">
            {queueDisabledReasons.length ? <EvidenceList items={queueDisabledReasons} /> : <p className="page-copy"><ShieldCheck size={16} /> All queue gates are satisfied.</p>}
            {queuedResult ? (
              <div className="settings-modal-grid">
                <EvidenceRow label="Queued" value={queuedResult.queued} tone={queuedResult.queued ? "success" : "danger"} />
                <EvidenceRow label="Execution started" value={queuedResult.execution_started} tone={queuedResult.execution_started === false ? "success" : "danger"} />
              </div>
            ) : null}
          </ContentCard>
        </ContentCard>
      )}

      {confirmationOpen ? (
        <Modal title="Confirm Queue Request" onClose={() => setConfirmationOpen(false)}>
          <div className="confirm-dialog-body">
            <InlineError>
              Queueing does not send messages immediately. It makes this campaign eligible for later scheduler or worker execution after backend safety gates pass.
            </InlineError>
            <div className="settings-modal-grid">
              <EvidenceRow label="Campaign" value={`${campaignTitle(selected)} (${campaignId(selected)})`} />
              <EvidenceRow label="Authorized recipients" value={authorization.liveAuthorized} />
              <EvidenceRow label="Blocked recipients" value={(authorization.unauthorized || 0) + (authorization.synthetic || 0) + (authorization.revoked || 0)} />
              <EvidenceRow label="Skipped recipients" value={deliverableCount !== null && authorization.liveEligible !== null ? deliverableCount - authorization.liveEligible : null} />
              <EvidenceRow label="Account/source context" value={accountSourceReady} />
              <EvidenceRow label="Effective limit" value={effectiveLimit} />
              <EvidenceRow label="Manifest hash" value={manifestHash} />
              <EvidenceRow label="Validation evidence" value={validationEvidence} />
              <EvidenceRow label="Dry-run/check evidence" value={checkEvidenceId} />
              <EvidenceRow label="Final-review evidence" value={finalReviewHash} />
            </div>
            <Checkbox
              checked={operatorConfirmed}
              label="I understand this arms the campaign for later execution after backend safety gates pass."
              onChange={(event) => setOperatorConfirmed(event.target.checked)}
            />
          </div>
          <div className="modal-actions">
            <DangerButton disabled={!operatorConfirmed || Boolean(busy)} onClick={confirmQueue}>
              <AlertTriangle size={17} />
              Confirm and queue
            </DangerButton>
            <SecondaryButton disabled={Boolean(busy)} onClick={() => setConfirmationOpen(false)}>Cancel</SecondaryButton>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
