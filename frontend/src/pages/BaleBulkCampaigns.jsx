import { useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ClipboardPaste, Pause, Play, Plus, RefreshCw, Save, Square, Trash2 } from "lucide-react";
import {
  getBaleBulkResults,
  getBaleBulkResumeState,
  prepareBaleBulkLiveRun,
  runBaleBulkDryPreflight,
  saveBaleBulkCampaign,
  validateBaleBulkCampaign,
} from "../api/campaigns";
import { ErrorState, KeyValueGrid, PageHeader, StatusBadge, fmt } from "../components/commercial/CommercialUi.jsx";

const maxRecipients = 10;
const defaultCampaign = {
  campaign_id: "",
  platform: "bale",
  account_id: "",
  source: { uid: "", url: "" },
  recipients: [],
  mode: "dry_run",
  concurrency: 1,
  max_recipients: maxRecipients,
  max_final_clicks: 0,
  continue_on_not_found: true,
  stop_on_ambiguous: true,
  stop_on_selection_mismatch: true,
  stop_on_structural_failure: true,
  metadata: {},
  explicit_live_authorized: false,
};

const protectedInvariants = [
  "exact recipient equality",
  "one exact recipient row",
  "full result-row click",
  "selected_names exact equality",
  "badge == 1",
  "exactly one final-send control",
  "at most one final click per recipient",
  "no retry after click invocation",
  "submitted is terminal",
  "checkpoint before continuing",
  "canonical profile lease and identity verification",
  "authenticated gate",
];

function normalized(value) {
  return String(value || "").trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

function uidFromUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.searchParams.get("uid") || "";
  } catch {
    return "";
  }
}

function sourceUrlFromUid(uid) {
  return uid ? `https://web.bale.ai/chat?uid=${encodeURIComponent(uid)}` : "";
}

function newRecipient(index) {
  return { recipient_id: `recipient-${String(index + 1).padStart(3, "0")}`, display_name: "", enabled: true };
}

function rowValidation(recipients) {
  const seen = new Map();
  const errors = {};
  recipients.forEach((recipient, index) => {
    const row = [];
    if (!recipient.recipient_id.trim()) row.push("recipient_id required");
    if (!recipient.display_name.trim()) row.push("display name required");
    const key = normalized(recipient.display_name);
    if (key) {
      if (seen.has(key)) {
        row.push("duplicate normalized name");
        errors[seen.get(key)] = [...(errors[seen.get(key)] || []), "duplicate normalized name"];
      }
      seen.set(key, index);
    }
    if (row.length) errors[index] = row;
  });
  return errors;
}

function countByState(items = []) {
  return items.reduce((acc, item) => {
    const state = item.state || item.validation_state || "pending";
    acc[state] = (acc[state] || 0) + 1;
    return acc;
  }, {});
}

export default function BaleBulkCampaigns() {
  const [campaign, setCampaign] = useState(defaultCampaign);
  const [pasteText, setPasteText] = useState("");
  const [validation, setValidation] = useState(null);
  const [results, setResults] = useState(null);
  const [resume, setResume] = useState(null);
  const [liveConfirmOpen, setLiveConfirmOpen] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState(null);

  const localRowErrors = useMemo(() => rowValidation(campaign.recipients), [campaign.recipients]);
  const backendRowErrors = validation?.row_errors || {};
  const mergedRowErrors = { ...localRowErrors, ...backendRowErrors };
  const enabledCount = campaign.recipients.filter((recipient) => recipient.enabled).length;
  const stateCounts = countByState(resume?.items || results?.recipient_results || []);
  const terminalCount = ["submitted", "skipped_terminal", "click_invoked", "post_click_ambiguous"].reduce((sum, key) => sum + (stateCounts[key] || 0), 0);
  const skippedCount = ["skipped_terminal", "skipped_disabled"].reduce((sum, key) => sum + (stateCounts[key] || 0), 0);
  const dryRunPassed = validation?.ok && (results?.status === "completed" || results?.status === "ready");
  const liveDisabled = !dryRunPassed || campaign.mode !== "live" || enabledCount === 0 || campaign.max_final_clicks <= 0;

  function updateCampaign(patch) {
    setCampaign((current) => ({ ...current, ...patch }));
  }

  function updateSource(patch) {
    setCampaign((current) => ({ ...current, source: { ...current.source, ...patch } }));
  }

  function updateRecipient(index, patch) {
    setCampaign((current) => ({
      ...current,
      recipients: current.recipients.map((recipient, rowIndex) => (rowIndex === index ? { ...recipient, ...patch } : recipient)),
    }));
  }

  function addRecipient() {
    setCampaign((current) => {
      if (current.recipients.length >= maxRecipients) return current;
      return { ...current, recipients: [...current.recipients, newRecipient(current.recipients.length)] };
    });
  }

  function removeRecipient(index) {
    setCampaign((current) => ({ ...current, recipients: current.recipients.filter((_, rowIndex) => rowIndex !== index) }));
  }

  function pasteRecipients() {
    const names = pasteText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    setCampaign((current) => {
      const remaining = maxRecipients - current.recipients.length;
      const additions = names.slice(0, remaining).map((name, offset) => ({
        recipient_id: name,
        display_name: name,
        enabled: true,
      }));
      return { ...current, recipients: [...current.recipients, ...additions] };
    });
    setPasteText("");
  }

  async function runAction(name, action) {
    setBusy(name);
    setError(null);
    try {
      const data = await action();
      return data;
    } catch (err) {
      setError(err);
      return null;
    } finally {
      setBusy("");
    }
  }

  async function saveDraft() {
    const data = await runAction("save", () => saveBaleBulkCampaign(campaign));
    if (data?.validation) setValidation(data.validation);
  }

  async function validate() {
    const data = await runAction("validate", () => validateBaleBulkCampaign(campaign.campaign_id, campaign));
    if (data) setValidation(data);
  }

  async function dryPreflight() {
    await saveDraft();
    const data = await runAction("dry", () => runBaleBulkDryPreflight(campaign.campaign_id));
    if (data) {
      setResults(data);
      const nextResume = await getBaleBulkResumeState(campaign.campaign_id);
      setResume(nextResume);
    }
  }

  async function reviewResults() {
    const data = await runAction("results", () => getBaleBulkResults(campaign.campaign_id));
    if (data) setResults(data.stored_campaign || data);
  }

  async function loadResume() {
    const data = await runAction("resume", () => getBaleBulkResumeState(campaign.campaign_id));
    if (data) setResume(data);
  }

  async function confirmLive() {
    const payload = { ...campaign, mode: "live", explicit_live_authorized: true };
    const data = await runAction("live", () => prepareBaleBulkLiveRun(campaign.campaign_id, payload));
    if (data) {
      setResults(data);
      setLiveConfirmOpen(false);
    }
  }

  return (
    <section className="commercial-page bale-bulk-page">
      <PageHeader title="Bale Bulk Campaign" description="Editable campaign configuration">
        <button className="secondary-button" type="button" onClick={loadResume} disabled={!campaign.campaign_id || busy}>
          <RefreshCw size={16} />
          Resume Pending
        </button>
      </PageHeader>

      {error ? <ErrorState error={error} /> : null}

      <div className="commercial-metrics">
        <article className="metric-card"><span>recipients</span><strong>{campaign.recipients.length}/{campaign.max_recipients}</strong></article>
        <article className="metric-card"><span>enabled nonterminal</span><strong>{Math.max(0, enabledCount - terminalCount)}</strong></article>
        <article className="metric-card"><span>terminal</span><strong>{terminalCount}</strong></article>
        <article className="metric-card"><span>skipped</span><strong>{skippedCount}</strong></article>
        <article className="metric-card"><span>final-click budget</span><strong>{campaign.max_final_clicks}</strong></article>
      </div>

      <div className="bale-bulk-layout">
        <section className="panel bulk-editor-panel">
          <div className="section-header">
            <h3>Campaign Configuration</h3>
            <StatusBadge value={validation?.ok ? "valid" : "draft"} />
          </div>
          <div className="settings-grid">
            <label>campaign name / ID<input value={campaign.campaign_id} onChange={(event) => updateCampaign({ campaign_id: event.target.value })} /></label>
            <label>sender Bale account<input value={campaign.account_id} onChange={(event) => updateCampaign({ account_id: event.target.value })} /></label>
            <label>source UID<input value={campaign.source.uid} onChange={(event) => updateSource({ uid: event.target.value, url: sourceUrlFromUid(event.target.value) || campaign.source.url })} /></label>
            <label>source URL<input value={campaign.source.url} onChange={(event) => updateSource({ url: event.target.value, uid: uidFromUrl(event.target.value) || campaign.source.uid })} /></label>
            <label>mode<select value={campaign.mode} onChange={(event) => updateCampaign({ mode: event.target.value, max_final_clicks: event.target.value === "dry_run" ? 0 : campaign.max_final_clicks })}><option value="dry_run">dry_run</option><option value="live">live</option></select></label>
            <label>maximum recipients<input type="number" min="1" max={maxRecipients} value={campaign.max_recipients} onChange={(event) => updateCampaign({ max_recipients: Number(event.target.value) })} /></label>
            <label>maximum final-click budget<input type="number" min="0" max={enabledCount} value={campaign.max_final_clicks} onChange={(event) => updateCampaign({ max_final_clicks: Number(event.target.value) })} /></label>
          </div>
          <div className="bulk-policy-grid">
            {[
              ["continue_on_not_found", "continue_on_not_found"],
              ["stop_on_ambiguous", "stop_on_ambiguous"],
              ["stop_on_selection_mismatch", "stop_on_selection_mismatch"],
              ["stop_on_structural_failure", "stop_on_structural_failure"],
            ].map(([key, label]) => (
              <label className="checkbox-row" key={key}>
                <input type="checkbox" checked={campaign[key]} onChange={(event) => updateCampaign({ [key]: event.target.checked })} />
                {label}
              </label>
            ))}
          </div>
          {validation?.errors?.length ? <div className="inline-error">{validation.errors.map((item) => `${item.field}: ${item.error_code}`).join(" | ")}</div> : null}
          <div className="modal-actions">
            <button className="primary-button" type="button" onClick={saveDraft} disabled={busy === "save"}><Save size={16} />Save Draft</button>
            <button className="secondary-button" type="button" onClick={validate} disabled={!campaign.campaign_id || busy === "validate"}><CheckCircle2 size={16} />Validate Campaign</button>
            <button className="secondary-button" type="button" onClick={dryPreflight} disabled={!validation?.ok || busy === "dry"}><Play size={16} />Run Dry Preflight</button>
            <button className="secondary-button" type="button" onClick={reviewResults} disabled={!campaign.campaign_id || busy === "results"}><RefreshCw size={16} />Review Results</button>
            <button className="primary-button" type="button" onClick={() => setLiveConfirmOpen(true)} disabled={liveDisabled}><AlertTriangle size={16} />Start Controlled Live Run</button>
            <button className="secondary-button" type="button" disabled><Pause size={16} />Pause</button>
            <button className="secondary-button" type="button" disabled><Square size={16} />Stop</button>
          </div>
        </section>

        <section className="panel protected-panel">
          <h3>Protected Scenario-Version Invariants</h3>
          <ul className="invariant-list">
            {protectedInvariants.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </section>
      </div>

      <section className="panel recipient-editor">
        <div className="section-header">
          <h3>Recipients</h3>
          <span className="status-note">{campaign.recipients.length} current / {campaign.max_recipients} maximum</span>
        </div>
        <div className="bulk-paste-row">
          <textarea value={pasteText} onChange={(event) => setPasteText(event.target.value)} rows="3" placeholder="Bale-000006&#10;Bale-000007" />
          <button className="secondary-button" type="button" onClick={pasteRecipients} disabled={!pasteText.trim() || campaign.recipients.length >= maxRecipients}><ClipboardPaste size={16} />Paste List</button>
          <button className="secondary-button" type="button" onClick={addRecipient} disabled={campaign.recipients.length >= maxRecipients}><Plus size={16} />Add Recipient</button>
        </div>
        <div className="table-scroll">
          <table className="table commercial-table bulk-recipient-table">
            <thead>
              <tr>
                <th>order</th>
                <th>enabled</th>
                <th>recipient ID</th>
                <th>display name</th>
                <th>validation</th>
                <th>preflight</th>
                <th>execution</th>
                <th>exact match</th>
                <th>final clicks</th>
                <th>delivery</th>
                <th>retry</th>
                <th>error / stop reason</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {campaign.recipients.map((recipient, index) => {
                const state = resume?.items?.find((item) => item.recipient_id === recipient.recipient_id);
                const rowErrors = mergedRowErrors[index] || [];
                const resultRow = results?.recipient_results?.find((item) => item.recipient_id === recipient.recipient_id) || state?.prior_result?.result || {};
                return (
                  <tr key={`${recipient.recipient_id}-${index}`}>
                    <td>{index + 1}</td>
                    <td><input type="checkbox" checked={recipient.enabled} onChange={(event) => updateRecipient(index, { enabled: event.target.checked })} /></td>
                    <td><input value={recipient.recipient_id} onChange={(event) => updateRecipient(index, { recipient_id: event.target.value })} /></td>
                    <td><input value={recipient.display_name} onChange={(event) => updateRecipient(index, { display_name: event.target.value })} /></td>
                    <td>{rowErrors.length ? <span className="inline-error">{rowErrors.join(", ")}</span> : <StatusBadge value="valid" />}</td>
                    <td><StatusBadge value={resultRow.state || state?.state || "pending"} /></td>
                    <td>{fmt(resultRow.delivery_status || resultRow.state)}</td>
                    <td>{fmt(resultRow.exact_match_count)}</td>
                    <td>{fmt(resultRow.final_send_click_count)}</td>
                    <td>{fmt(resultRow.delivery_status)}</td>
                    <td>{resultRow.retry_allowed === false ? "false" : fmt(resultRow.retry_allowed)}</td>
                    <td>{fmt(resultRow.error_code || results?.stop_reason)}</td>
                    <td><button className="icon-button danger-icon" type="button" onClick={() => removeRecipient(index)} aria-label="Remove recipient"><Trash2 size={15} /></button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {results ? (
        <section className="panel result-panel">
          <h3>Results</h3>
          <KeyValueGrid data={{
            status: results.status || results.live_run_ready,
            stop_reason: results.stop_reason,
            next_pending_recipient: results.next_pending_recipient || resume?.next_pending_recipient_id,
            total_final_clicks: results.click_budget?.used_final_clicks || 0,
            execution_started: results.execution_started,
          }} />
        </section>
      ) : null}

      {liveConfirmOpen ? (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <section className="modal-panel commercial-modal">
            <div className="modal-header">
              <h3>Controlled Live Confirmation</h3>
              <button className="icon-button" type="button" onClick={() => setLiveConfirmOpen(false)}>x</button>
            </div>
            <p className="status-note">
              Recipient count: {enabledCount}. Maximum possible final clicks: {campaign.max_final_clicks}.
            </p>
            <div className="modal-actions">
              <button className="danger-button" type="button" onClick={confirmLive} disabled={busy === "live"}><AlertTriangle size={16} />Confirm Live Authorization</button>
              <button className="secondary-button" type="button" onClick={() => setLiveConfirmOpen(false)}>Cancel</button>
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
