import { Copy, Download, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { listEvents } from "../api/events";
import { listJobs } from "../api/jobs";
import { listLogs } from "../api/logs";
import { getSchedulerStatus } from "../api/scheduler";
import { listDiagnosticRuns } from "../api/automation";
import { listBrowserProviders } from "../api/platforms";
import { request } from "../api/client";
import {
  createDiagnosticEvent, downloadDiagnostics, getClientDiagnosticEvents,
  readableReport, subscribeDiagnostics,
} from "../diagnostics";
import { ContentCard, EmptyState, InlineError, LoadingState, PageHeader, SecondaryButton, StatusBadge } from "../components/ui/DesignSystem.jsx";

const fmt = (value) => value === null || value === undefined || value === "" ? "-" : String(value);
const tone = (value) => /fail|error|cancel/i.test(value || "") ? "danger" : /success|complete|running|queue/i.test(value || "") ? "success" : "neutral";

function backendEvent(event) {
  let diagnostics = {};
  try { diagnostics = typeof event.diagnostics_json === "string" ? JSON.parse(event.diagnostics_json) : event.diagnostics_json || {}; } catch { diagnostics = {}; }
  return createDiagnosticEvent({
    timestamp: event.created_at, event_id: event.id, action: event.event_type,
    module: diagnostics.module || "backend", account_id: event.account_id,
    campaign_id: event.campaign_id, success: !/fail|error/i.test(event.status || "") && !event.error_code,
    status: event.status, error_code: event.error_code, error_message: event.error_message || event.message,
    endpoint: diagnostics.endpoint, duration_ms: diagnostics.duration_ms,
    related_files: diagnostics.related_files || [], stack_trace: diagnostics.stack_trace,
  });
}

export default function OperationsAndLogs() {
  const [data, setData] = useState({ jobs: [], events: [], logs: [], runs: [], providers: [], health: [], scheduler: null });
  const [clientEvents, setClientEvents] = useState(getClientDiagnosticEvents);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState("");
  const [copied, setCopied] = useState(false);

  async function load() {
    setLoading(true); setError("");
    const sources = await Promise.allSettled([
      listJobs({ limit: 40, offset: 0 }), listEvents({ limit: 80, offset: 0 }), listLogs(),
      listDiagnosticRuns(), listBrowserProviders(), request("/automation/accounts/health"), getSchedulerStatus(),
    ]);
    const failures = sources.filter((item) => item.status === "rejected");
    setData({
      jobs: sources[0].value?.items || [], events: sources[1].value?.items || [],
      logs: Array.isArray(sources[2].value) ? sources[2].value : [],
      runs: sources[3].value?.runs || [],
      providers: Array.isArray(sources[4].value?.items) ? sources[4].value.items : Array.isArray(sources[4].value) ? sources[4].value : [],
      health: Array.isArray(sources[5].value?.items) ? sources[5].value.items : Array.isArray(sources[5].value) ? sources[5].value : [],
      scheduler: sources[6].value || null,
    });
    if (failures.length) setError(`${failures.length} diagnostic source(s) could not be loaded. Available sources are shown below.`);
    setLoading(false);
  }

  useEffect(() => { load(); return subscribeDiagnostics(() => setClientEvents(getClientDiagnosticEvents())); }, []);
  const structured = useMemo(() => [...clientEvents, ...data.events.map(backendEvent)]
    .sort((a, b) => String(b.timestamp).localeCompare(String(a.timestamp))), [clientEvents, data.events]);
  const failures = structured.filter((event) => !event.success);
  const context = { scheduler: data.scheduler, diagnostic_runs: data.runs.map((run) => run.name), browser_providers: data.providers };
  const report = () => readableReport(structured, context);
  async function copyReport() { await navigator.clipboard.writeText(report()); setCopied(true); window.setTimeout(() => setCopied(false), 1500); }
  function downloadJson() { downloadDiagnostics("clinicos-diagnostics.json", JSON.stringify({ generated_at: new Date().toISOString(), context, events: structured }, null, 2), "application/json"); }
  function downloadTxt() { downloadDiagnostics("clinicos-diagnostics.txt", report(), "text/plain"); }

  return (
    <section className="operations-logs-page" dir="rtl">
      <PageHeader title="مرکز عملیات / Operations Center" description="نمای یکپارچه و فقط‌خواندنی سلامت سیستم، عملیات، خطاها، لاگ‌ها و رخدادهای تشخیصی موجود."
        actions={<div className="operations-export-actions">
          <SecondaryButton onClick={copyReport}><Copy size={16} />{copied ? "Copied" : "Copy Debug Report"}</SecondaryButton>
          <SecondaryButton onClick={downloadJson}><Download size={16} />Download JSON</SecondaryButton>
          <SecondaryButton onClick={downloadTxt}><Download size={16} />Download TXT</SecondaryButton>
          <SecondaryButton onClick={load} disabled={loading}><RefreshCw size={16} />تازه‌سازی</SecondaryButton>
        </div>} />
      {error ? <InlineError>{error}</InlineError> : null}
      {loading ? <LoadingState label="در حال جمع‌آوری اطلاعات عملیاتی" /> : null}

      <div className="operations-summary-grid">
        <ContentCard title="System health"><div className="status-stack">
          <p><span>Scheduler</span><b>{fmt(data.scheduler?.scheduler_status)}</b></p>
          <p><span>Available workers</span><b>{fmt(data.scheduler?.available_slots)}</b></p>
          <p><span>Account health records</span><b>{data.health.length}</b></p>
          <p><span>Browser providers</span><b>{data.providers.length}</b></p>
        </div></ContentCard>
        <ContentCard title="Operational summary"><div className="status-stack">
          <p><span>Recent actions</span><b>{structured.length}</b></p>
          <p><span>Errors</span><b>{failures.length}</b></p>
          <p><span>Campaign operations</span><b>{data.jobs.length}</b></p>
          <p><span>Backend logs</span><b>{data.logs.length}</b></p>
        </div></ContentCard>
      </div>

      <ContentCard title="Recent actions, errors, account/campaign and worker events">
        {structured.length ? <div className="operations-event-list">{structured.slice(0, 100).map((event) => (
          <article key={event.event_id}>
            <div><strong>{event.module} · {event.action}</strong><span>{event.error_message || event.endpoint || "Completed"}</span></div>
            <StatusBadge tone={tone(event.status)}>{event.status}</StatusBadge>
            <small>{event.account_id || "-"} · {event.campaign_id || "-"} · {event.timestamp}</small>
            <button className="secondary-button" onClick={() => setExpanded(expanded === event.event_id ? "" : event.event_id)}>View Details</button>
            {expanded === event.event_id ? <pre className="diagnostics-file-preview" dir="ltr">{JSON.stringify(event, null, 2)}</pre> : null}
          </article>
        ))}</div> : <EmptyState title="No operational events are available." />}
      </ContentCard>

      <div className="grid two">
        <ContentCard title="Backend logs">{data.logs.length ? <pre className="diagnostics-file-preview" dir="ltr">{data.logs.slice(-80).map((log) => `${fmt(log.timestamp || log.created_at)} ${fmt(log.status)} ${fmt(log.message)}`).join("\n")}</pre> : <EmptyState title="No backend logs." />}</ContentCard>
        <ContentCard title="Browser/profile events and diagnostic files">
          <div className="status-stack">
            {data.providers.map((provider, index) => <p key={provider.id || index}><span>{fmt(provider.name || provider.id || provider.provider)}</span><b>{fmt(provider.status || provider.enabled)}</b></p>)}
            {data.runs.slice(0, 20).map((run) => <p key={run.name}><span>{run.name}</span><b>{run.file_count} files</b></p>)}
          </div>
        </ContentCard>
      </div>
    </section>
  );
}
