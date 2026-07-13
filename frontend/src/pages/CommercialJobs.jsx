import { useEffect, useState } from "react";
import { RotateCw } from "lucide-react";
import { getJob, listJobEvents, listJobs } from "../api/jobs";
import { EmptyState, ErrorState, KeyValueGrid, LoadingState, Modal, PageHeader, Pager, StatusBadge, fmt, shortId } from "../components/commercial/CommercialUi.jsx";

const statuses = ["", "queued", "assigned", "running", "succeeded", "failed", "skipped", "paused", "cancelled"];

function parseDiagnostics(value) {
  if (!value) return null;
  if (typeof value === "object") return value;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

export default function CommercialJobs() {
  const [filters, setFilters] = useState({ status: "", account_id: "", campaign_id: "" });
  const [limit] = useState(25);
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [detail, setDetail] = useState(null);
  const [events, setEvents] = useState([]);

  async function load(nextOffset = offset) {
    setLoading(true);
    setError(null);
    try {
      const data = await listJobs({ ...filters, limit, offset: nextOffset });
      setRows(data.items || []);
      setOffset(nextOffset);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }

  async function openDetail(jobId) {
    setError(null);
    try {
      const [jobData, eventData] = await Promise.all([getJob(jobId), listJobEvents(jobId, { limit: 50 })]);
      setDetail(jobData);
      setEvents(eventData.items || []);
    } catch (err) {
      setError(err);
    }
  }

  useEffect(() => {
    load(0);
  }, []);

  return (
    <section className="rtl-page commercial-page">
      <PageHeader title="صف عملیات" description="جاب‌های persistent، وضعیت اجرا، نتیجه forward و خطاها">
        <button className="secondary-button" type="button" onClick={() => load(offset)}>
          <RotateCw size={16} />
          تازه‌سازی
        </button>
      </PageHeader>
      <div className="filter-bar">
        <select value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
          {statuses.map((status) => <option key={status || "all"} value={status}>{status || "همه وضعیت‌ها"}</option>)}
        </select>
        <input placeholder="account_id" value={filters.account_id} onChange={(event) => setFilters({ ...filters, account_id: event.target.value })} />
        <input placeholder="campaign_id" value={filters.campaign_id} onChange={(event) => setFilters({ ...filters, campaign_id: event.target.value })} />
        <button className="primary-button" type="button" onClick={() => load(0)}>اعمال فیلتر</button>
      </div>
      {error ? <ErrorState error={error} /> : null}
      {loading ? (
        <LoadingState />
      ) : rows.length ? (
        <>
          <div className="table-scroll">
            <table className="table rtl-table commercial-table">
              <thead>
                <tr>
                  <th>job id</th>
                  <th>campaign</th>
                  <th>recipient / phone</th>
                  <th>account</th>
                  <th>status</th>
                  <th>priority</th>
                  <th>attempt</th>
                  <th>scheduled_at</th>
                  <th>started_at</th>
                  <th>completed_at</th>
                  <th>last error</th>
                  <th>forward verified</th>
                  <th>forwarded count</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((job) => (
                  <tr key={job.id} className="click-row" onClick={() => openDetail(job.id)}>
                    <td>{shortId(job.id)}</td>
                    <td>{shortId(job.campaign_id)}</td>
                    <td>{fmt(job.phone_normalized || job.recipient_id)}</td>
                    <td>{fmt(job.account_id)}</td>
                    <td><StatusBadge value={job.status} /></td>
                    <td>{fmt(job.priority)}</td>
                    <td>{fmt(job.attempt_count)}</td>
                    <td>{fmt(job.scheduled_at)}</td>
                    <td>{fmt(job.started_at)}</td>
                    <td>{fmt(job.completed_at)}</td>
                    <td>{fmt(job.last_error_code || job.last_error_message)}</td>
                    <td>{fmt(job.forward_verified)}</td>
                    <td>{fmt(job.verified_forwarded_recipient_count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager limit={limit} offset={offset} onPage={load} />
        </>
      ) : (
        <EmptyState />
      )}

      {detail ? (
        <Modal title={`جزئیات جاب ${shortId(detail.id, 24)}`} onClose={() => setDetail(null)} wide>
          <KeyValueGrid data={detail} />
          <h4 className="subheading">مجوز اجرای واقعی</h4>
          <KeyValueGrid data={{
            "منبع گیرنده": detail.recipient_origin,
            "داده آزمایشی": Boolean(detail.synthetic_test_data),
            "مجوز اجرای واقعی": Boolean(detail.live_execution_authorized),
            "وضعیت مجوز": detail.authorization_status,
            "زمان تأیید": detail.live_authorized_at,
            "تأییدکننده": detail.live_authorized_by,
            "یادداشت مجوز": detail.authorization_note,
            "دلیل مسدودشدن اجرای واقعی": detail.last_error_code === "live_recipient_authorization_required" ? detail.last_error_message : "",
          }} />
          {events.find((event) => parseDiagnostics(event.diagnostics_json)?.session_id || parseDiagnostics(event.diagnostics_json)?.session_reused !== undefined) ? (
            <>
              <h4 className="subheading">نشست و زمان‌بندی</h4>
              <KeyValueGrid data={parseDiagnostics(events.find((event) => parseDiagnostics(event.diagnostics_json)?.session_id || parseDiagnostics(event.diagnostics_json)?.session_reused !== undefined)?.diagnostics_json) || {}} />
            </>
          ) : null}
          <h4 className="subheading">رویدادها</h4>
          <div className="timeline">
            {events.map((event) => (
              <div key={event.id} className="timeline-item">
                <StatusBadge value={event.status} />
                <strong>{event.event_type}</strong>
                <span>{fmt(event.step_name)}</span>
                <p>{fmt(event.message || event.error_message)}</p>
              </div>
            ))}
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
