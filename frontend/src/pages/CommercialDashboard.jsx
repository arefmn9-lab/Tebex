import { useEffect, useState } from "react";
import { Pause, Play, RotateCw, Square, StepForward } from "lucide-react";
import { getDashboardSummary } from "../api/dashboard";
import { listEvents } from "../api/events";
import { listJobs } from "../api/jobs";
import { getSchedulerStatus, pauseScheduler, resumeScheduler, runSchedulerOnce, startScheduler, stopScheduler } from "../api/scheduler";
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, fmt, shortId } from "../components/commercial/CommercialUi.jsx";

const summaryLabels = {
  total_accounts: "کل اکانت‌ها",
  active_workers: "ورکرهای فعال",
  available_worker_slots: "ظرفیت آزاد",
  queued_jobs: "در صف",
  running_jobs: "در حال اجرا",
  succeeded_today: "موفق امروز",
  failed_today: "ناموفق امروز",
  paused_jobs: "متوقف",
  campaigns_running: "کمپین فعال",
  scheduler_status: "وضعیت زمان‌بند",
};

export default function CommercialDashboard() {
  const [summary, setSummary] = useState(null);
  const [scheduler, setScheduler] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busyAction, setBusyAction] = useState("");

  async function load() {
    setError(null);
    setLoading(true);
    try {
      const [summaryData, schedulerData, jobsData, eventsData] = await Promise.all([
        getDashboardSummary(),
        getSchedulerStatus(),
        listJobs({ limit: 8 }),
        listEvents({ limit: 8 }),
      ]);
      setSummary(summaryData);
      setScheduler(schedulerData);
      setJobs(jobsData.items || []);
      setEvents(eventsData.items || []);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }

  async function schedulerAction(name, action) {
    setBusyAction(name);
    setError(null);
    try {
      await action();
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusyAction("");
    }
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <section className="rtl-page commercial-page">
      <PageHeader title="داشبورد تجاری" description="نمای عملیاتی صف، ورکرها، کمپین‌ها و وضعیت زمان‌بند">
        <button className="secondary-button" type="button" onClick={load}>
          <RotateCw size={16} />
          تازه‌سازی
        </button>
      </PageHeader>
      {error ? <ErrorState error={error} /> : null}
      {loading ? (
        <LoadingState />
      ) : (
        <>
          <div className="commercial-metrics">
            {Object.entries(summaryLabels).map(([key, label]) => (
              <article className="metric-card" key={key}>
                <span>{label}</span>
                <strong>{fmt(summary?.[key])}</strong>
              </article>
            ))}
          </div>

          <div className="grid two commercial-split">
            <section className="panel">
              <div className="panel-header">
                <h3 className="panel-title">کنترل زمان‌بند</h3>
                <StatusBadge value={scheduler?.scheduler_status} />
              </div>
              <div className="scheduler-actions">
                <button className="primary-button" disabled={!!busyAction} type="button" onClick={() => schedulerAction("start", startScheduler)}>
                  <Play size={16} />
                  شروع
                </button>
                <button className="secondary-button" disabled={!!busyAction} type="button" onClick={() => schedulerAction("pause", pauseScheduler)}>
                  <Pause size={16} />
                  توقف موقت
                </button>
                <button className="secondary-button" disabled={!!busyAction} type="button" onClick={() => schedulerAction("resume", resumeScheduler)}>
                  <Play size={16} />
                  ادامه
                </button>
                <button className="danger-button" disabled={!!busyAction} type="button" onClick={() => schedulerAction("stop", stopScheduler)}>
                  <Square size={16} />
                  توقف
                </button>
                <button className="secondary-button" disabled={!!busyAction} type="button" onClick={() => schedulerAction("run-once", () => runSchedulerOnce({ dry_run: true }))}>
                  <StepForward size={16} />
                  اجرای خشک یک تیک
                </button>
              </div>
              <div className="ops-list">
                <p>اکانت‌های فعال: {fmt((scheduler?.active_accounts || []).join(", "))}</p>
                <p>در cooldown: {fmt((scheduler?.cooling_down_accounts || []).join(", "))}</p>
                <p>محدودیت روزانه: {fmt((scheduler?.daily_limited_accounts || []).join(", "))}</p>
                <p>استراتژی: {fmt(scheduler?.account_assignment_strategy)}</p>
              </div>
            </section>

            <section className="panel">
              <div className="panel-header">
                <h3 className="panel-title">آخرین رویدادها</h3>
              </div>
              {events.length ? (
                <div className="compact-list">
                  {events.map((event) => (
                    <div key={event.id} className="compact-row">
                      <strong>{event.event_type}</strong>
                      <span>{shortId(event.job_id)}</span>
                      <StatusBadge value={event.status} />
                    </div>
                  ))}
                </div>
              ) : (
                <EmptyState />
              )}
            </section>
          </div>

          <section className="panel">
            <div className="panel-header">
              <h3 className="panel-title">آخرین جاب‌ها</h3>
            </div>
            {jobs.length ? (
              <div className="table-scroll">
                <table className="table rtl-table wide-table">
                  <thead>
                    <tr>
                      <th>جاب</th>
                      <th>کمپین</th>
                      <th>اکانت</th>
                      <th>شماره</th>
                      <th>وضعیت</th>
                      <th>شروع</th>
                      <th>خطا</th>
                    </tr>
                  </thead>
                  <tbody>
                    {jobs.map((job) => (
                      <tr key={job.id}>
                        <td>{shortId(job.id)}</td>
                        <td>{shortId(job.campaign_id)}</td>
                        <td>{fmt(job.account_id)}</td>
                        <td>{fmt(job.phone_normalized)}</td>
                        <td><StatusBadge value={job.status} /></td>
                        <td>{fmt(job.started_at)}</td>
                        <td>{fmt(job.last_error_code)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState />
            )}
          </section>
        </>
      )}
    </section>
  );
}
