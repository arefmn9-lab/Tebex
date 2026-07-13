import { Fragment, useEffect, useState } from "react";
import { RotateCw } from "lucide-react";
import { getSchedulerStatus } from "../api/scheduler";
import { listEvents } from "../api/events";
import { EmptyState, ErrorState, LoadingState, PageHeader, Pager, StatusBadge, fmt, shortId } from "../components/commercial/CommercialUi.jsx";

export default function CommercialLogs() {
  const [filters, setFilters] = useState({ account_id: "", campaign_id: "", job_id: "" });
  const [limit] = useState(30);
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState([]);
  const [scheduler, setScheduler] = useState(null);
  const [expanded, setExpanded] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  async function load(nextOffset = offset) {
    setLoading(true);
    setError(null);
    try {
      const [eventData, schedulerData] = await Promise.all([
        listEvents({ ...filters, limit, offset: nextOffset }),
        getSchedulerStatus(),
      ]);
      setRows(eventData.items || []);
      setScheduler(schedulerData);
      setOffset(nextOffset);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(0);
  }, []);

  return (
    <section className="rtl-page commercial-page">
      <PageHeader title="لاگ عملیات" description="رویدادهای سطح جاب، account و زمان‌بند">
        <button className="secondary-button" type="button" onClick={() => load(offset)}>
          <RotateCw size={16} />
          تازه‌سازی
        </button>
      </PageHeader>
      <div className="grid two commercial-split">
        <aside className="panel">
          <div className="panel-header"><h3 className="panel-title">لاگ سیستم</h3></div>
          <div className="ops-list">
            <p>وضعیت زمان‌بند: {fmt(scheduler?.scheduler_status)}</p>
            <p>اسلات آزاد: {fmt(scheduler?.available_slots)}</p>
            <p>اکانت فعال: {fmt(scheduler?.active_account_count)}</p>
            <p>آخرین tick: {fmt(scheduler?.last_tick_at)}</p>
          </div>
        </aside>
        <section className="panel">
          <div className="filter-bar">
            <input placeholder="job_id" value={filters.job_id} onChange={(event) => setFilters({ ...filters, job_id: event.target.value })} />
            <input placeholder="campaign_id" value={filters.campaign_id} onChange={(event) => setFilters({ ...filters, campaign_id: event.target.value })} />
            <input placeholder="account_id" value={filters.account_id} onChange={(event) => setFilters({ ...filters, account_id: event.target.value })} />
            <button className="primary-button" type="button" onClick={() => load(0)}>اعمال</button>
          </div>
        </section>
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
                  <th>event id</th>
                  <th>recipient / lead</th>
                  <th>event type</th>
                  <th>step</th>
                  <th>status</th>
                  <th>message</th>
                  <th>error</th>
                  <th>account</th>
                  <th>created</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((event) => (
                  <Fragment key={event.id}>
                    <tr key={event.id} className="click-row" onClick={() => setExpanded(expanded === event.id ? "" : event.id)}>
                      <td>{shortId(event.id)}</td>
                      <td>{shortId(event.recipient_id)}</td>
                      <td>{event.event_type}</td>
                      <td>{fmt(event.step_name)}</td>
                      <td><StatusBadge value={event.status} /></td>
                      <td className="truncate">{fmt(event.message)}</td>
                      <td>{fmt(event.error_code || event.error_message)}</td>
                      <td>{fmt(event.account_id)}</td>
                      <td>{fmt(event.created_at)}</td>
                    </tr>
                    {expanded === event.id ? (
                      <tr key={`${event.id}-diagnostics`}>
                        <td colSpan="9">
                          <pre className="diagnostics-pre">{fmt(event.diagnostics_json)}</pre>
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
          <Pager limit={limit} offset={offset} onPage={load} />
        </>
      ) : (
        <EmptyState />
      )}
    </section>
  );
}
