const events = [];
const listeners = new Set();
const SECRET_KEYS = /otp|cookie|token|secret|authorization|password|storage/i;
// Diagnostics cannot import the API client because the API client already
// records diagnostics. Keep this base expression in sync with api/client.js so
// browser-originated events reach FastAPI rather than Vite's own origin.
const DIAGNOSTICS_API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8011";
export const FRONTEND_SOURCE_REVISION = __CLINICOS_SOURCE_REVISION__;
export const FRONTEND_SOURCE_ROOT = __CLINICOS_FRONTEND_ROOT__;

function mask(value) {
  const text = String(value || "");
  if (!text) return null;
  if (text.length <= 4) return "****";
  return `${text.slice(0, 2)}***${text.slice(-2)}`;
}

function scrub(value, seen = new WeakSet()) {
  if (value === null || value === undefined) return value;
  if (typeof value !== "object") return value;
  if (seen.has(value)) return "[circular]";
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => scrub(item, seen));
  return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !SECRET_KEYS.test(key))
    .map(([key, item]) => [key, scrub(item, seen)]));
}

function idFromPath(path, name) {
  const match = String(path).match(new RegExp(`/${name}s?/([^/?]+)`, "i"));
  return match ? decodeURIComponent(match[1]) : null;
}

export function createDiagnosticEvent(input) {
  const endpoint = input.endpoint || "";
  return scrub({
    timestamp: input.timestamp || new Date().toISOString(),
    event_id: input.event_id || crypto.randomUUID(),
    action: input.action || "unknown",
    module: input.module || "frontend",
    account_id: input.account_id || idFromPath(endpoint, "account"),
    campaign_id: input.campaign_id || idFromPath(endpoint, "campaign"),
    success: Boolean(input.success),
    status: input.status || (input.success ? "succeeded" : "failed"),
    error_code: input.error_code || null,
    error_message: input.error_message || null,
    endpoint: endpoint || null,
    duration_ms: Number.isFinite(input.duration_ms) ? Math.round(input.duration_ms) : null,
    related_files: input.related_files || [],
    stack_trace: input.stack_trace || null,
    source_revision: FRONTEND_SOURCE_REVISION,
    clicked_action: input.clicked_action || null,
    handler_reached: input.handler_reached ?? null,
    button_disabled: input.button_disabled ?? null,
    disabled_reason: input.disabled_reason || null,
    pending_state: input.pending_state || null,
    operation_id: input.operation_id || null,
    http_method: input.http_method || null,
    stage: input.stage || null,
    result: input.result || null,
  });
}

export function recordDiagnosticEvent(input) {
  const event = createDiagnosticEvent(input);
  events.unshift(event);
  events.splice(250);
  listeners.forEach((listener) => listener(event));
  if (typeof fetch === "function") {
    fetch(`${DIAGNOSTICS_API_BASE_URL}/automation/diagnostics/client-events`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(event), keepalive: true,
    }).catch(() => {});
  }
  return event;
}

export const getClientDiagnosticEvents = () => [...events];
export function subscribeDiagnostics(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function readableReport(allEvents, context = {}) {
  const lines = [
    "ClinicOS Diagnostic Report",
    `Generated: ${new Date().toISOString()}`,
    `Location: ${window.location.hash || "#/dashboard"}`,
    `Context: ${JSON.stringify(scrub(context))}`,
    `Events: ${allEvents.length}`,
    "",
  ];
  allEvents.forEach((event, index) => lines.push(
    `#${index + 1} ${event.timestamp} | ${event.status} | ${event.module}.${event.action}`,
    `event_id: ${event.event_id}`,
    `account_id: ${event.account_id || "-"} | campaign_id: ${event.campaign_id || "-"}`,
    `endpoint: ${event.endpoint || "-"} | duration_ms: ${event.duration_ms ?? "-"}`,
    `error: ${event.error_code || "-"} ${event.error_message || ""}`.trim(),
    `related_files: ${(event.related_files || []).join(", ") || "-"}`,
    event.stack_trace ? `stack_trace:\n${event.stack_trace}` : "stack_trace: -",
    ""
  ));
  return lines.join("\n");
}

export function downloadDiagnostics(filename, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
