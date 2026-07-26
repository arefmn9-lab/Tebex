import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { listEvents } from "../api/events";
import { listJobs } from "../api/jobs";
import { getSchedulerStatus } from "../api/scheduler";
import {
  ContentCard,
  EmptyState,
  FormField,
  InlineError,
  LoadingState,
  PageHeader,
  SecondaryButton,
  StatusBadge,
  TextInput,
} from "../components/ui/DesignSystem.jsx";

function fmt(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function short(value, length = 16) {
  const text = fmt(value);
  return text.length > length ? `${text.slice(0, length)}...` : text;
}

function tone(status) {
  const value = String(status || "").toLowerCase();
  if (["succeeded", "success", "completed", "running", "queued"].includes(value)) return "success";
  if (["failed", "error", "cancelled"].includes(value)) return "danger";
  if (["paused", "skipped", "assigned"].includes(value)) return "warning";
  return "neutral";
}

function label(status) {
  const labels = {
    queued: "در صف",
    assigned: "اختصاص داده شده",
    running: "در حال اجرا",
    succeeded: "موفق",
    completed: "کامل شده",
    failed: "خطا",
    skipped: "رد شده",
    paused: "متوقف",
    cancelled: "لغو شده",
  };
  return labels[status] || status || "نامشخص";
}

export default function OperationsAndLogs() {
  const [filters, setFilters] = useState({ campaign_id: "", account_id: "", recipient_id: "" });
  const [jobs, setJobs] = useState([]);
  const [events, setEvents] = useState([]);
  const [scheduler, setScheduler] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [jobData, eventData, schedulerData] = await Promise.all([
        listJobs({ campaign_id: filters.campaign_id, account_id: filters.account_id, limit: 30, offset: 0 }),
        listEvents({ campaign_id: filters.campaign_id, account_id: filters.account_id, limit: 40, offset: 0 }),
        getSchedulerStatus().catch(() => null),
      ]);
      setJobs(jobData?.items || []);
      setEvents(eventData?.items || []);
      setScheduler(schedulerData);
    } catch (err) {
      setError(err.message || "دریافت عملیات و لاگ‌ها انجام نشد");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <section className="operations-logs-page" dir="rtl">
      <PageHeader
        title="عملیات و لاگ‌ها"
        description="نمای یکپارچه فعالیت کمپین، اکانت، گیرنده، خطاها و زمان‌ها بدون تغییر در Backend."
        actions={<SecondaryButton onClick={load} disabled={loading}><RefreshCw size={17} />تازه‌سازی</SecondaryButton>}
      />

      {error ? <InlineError>{error}</InlineError> : null}

      <ContentCard title="فیلترها" description="فقط برای نمایش UI استفاده می‌شود و لاگ Backend را تغییر نمی‌دهد.">
        <div className="operations-filter-grid">
          <FormField label="کمپین"><TextInput value={filters.campaign_id} onChange={(event) => setFilters({ ...filters, campaign_id: event.target.value })} /></FormField>
          <FormField label="اکانت"><TextInput value={filters.account_id} onChange={(event) => setFilters({ ...filters, account_id: event.target.value })} /></FormField>
          <FormField label="گیرنده"><TextInput value={filters.recipient_id} onChange={(event) => setFilters({ ...filters, recipient_id: event.target.value })} /></FormField>
          <SecondaryButton onClick={load}>اعمال فیلتر</SecondaryButton>
        </div>
      </ContentCard>

      <div className="operations-summary-grid">
        <ContentCard title="وضعیت عملیات">
          <div className="status-stack">
            <p><span>زمان‌بند</span><b>{fmt(scheduler?.scheduler_status)}</b></p>
            <p><span>اسلات آزاد</span><b>{fmt(scheduler?.available_slots)}</b></p>
            <p><span>اکانت فعال</span><b>{fmt(scheduler?.active_account_count)}</b></p>
          </div>
        </ContentCard>
        <ContentCard title="خلاصه">
          <div className="status-stack">
            <p><span>فعالیت کمپین</span><b>{jobs.length}</b></p>
            <p><span>رویدادها</span><b>{events.length}</b></p>
            <p><span>خطاها</span><b>{events.filter((event) => event.error_code || event.error_message || event.status === "failed").length}</b></p>
          </div>
        </ContentCard>
      </div>

      {loading ? <LoadingState label="در حال دریافت عملیات" /> : null}

      {!loading ? (
        <ContentCard title="فعالیت کمپین و گیرندگان">
          {jobs.length ? (
            <div className="workspace-table-wrap">
              <table className="table bale-table">
                <thead><tr><th>کمپین</th><th>گیرنده</th><th>اکانت</th><th>وضعیت</th><th>خطا</th><th>زمان</th></tr></thead>
                <tbody>
                  {jobs.map((job) => (
                    <tr key={job.id}>
                      <td>{short(job.campaign_id)}</td>
                      <td>{fmt(job.phone_normalized || job.recipient_id)}</td>
                      <td>{fmt(job.account_id)}</td>
                      <td><StatusBadge tone={tone(job.status)}>{label(job.status)}</StatusBadge></td>
                      <td>{fmt(job.last_error_code || job.last_error_message)}</td>
                      <td>{fmt(job.started_at || job.scheduled_at || job.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <EmptyState title="فعالیت کمپینی ثبت نشده است" />}
        </ContentCard>
      ) : null}

      {!loading ? (
        <ContentCard title="رویدادها و خطاها">
          {events.length ? (
            <div className="operations-event-list">
              {events.map((event) => (
                <article key={event.id}>
                  <div>
                    <strong>{fmt(event.event_type)}</strong>
                    <span>{fmt(event.message || event.error_message)}</span>
                  </div>
                  <StatusBadge tone={tone(event.status)}>{label(event.status)}</StatusBadge>
                  <small>{fmt(event.account_id)} · {fmt(event.recipient_id)} · {fmt(event.created_at)}</small>
                </article>
              ))}
            </div>
          ) : <EmptyState title="لاگی برای نمایش وجود ندارد" />}
        </ContentCard>
      ) : null}
    </section>
  );
}
