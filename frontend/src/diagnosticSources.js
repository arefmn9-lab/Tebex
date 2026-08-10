const SOURCE_DEFINITIONS = [
  ["jobs", "/automation/jobs", ["items"]],
  ["events", "/automation/events", ["items"]],
  ["logs", "/automation/logs", ["items", "logs", "$array"]],
  ["diagnostic_runs", "/automation/diagnostics/runs", ["runs", "items", "$array"]],
  ["browser_providers", "/automation/browser/providers", ["items", "providers", "$array"]],
  ["health", "/automation/accounts/health", ["items", "$array"]],
  ["scheduler", "/automation/scheduler/status", ["scheduler", "status", "$object"]],
  ["client_events", "/automation/diagnostics/client-events", ["items", "$array"]],
];

function selectValue(value, paths) {
  for (const path of paths) {
    if (path === "$array" && Array.isArray(value)) return { value, path };
    if (path === "$object" && value && typeof value === "object" && !Array.isArray(value)) return { value, path };
    if (value && Object.prototype.hasOwnProperty.call(value, path)) return { value: value[path], path };
  }
  throw new Error(`response_shape_unrecognized; expected one of ${paths.join(",")}`);
}

export function parseDiagnosticSources(results) {
  const values = {};
  const metadata = {};
  SOURCE_DEFINITIONS.forEach(([name, endpoint, paths], index) => {
    const result = results[index];
    const base = { endpoint, request_status: result?.status || "missing", http_status: result?.value?.__http_status ?? null, parse_error: null, value_path_used: null };
    if (!result || result.status !== "fulfilled") {
      metadata[name] = { ...base, parse_error: result?.reason?.message || "request_failed" };
      values[name] = undefined;
      return;
    }
    try {
      const selected = selectValue(result.value, paths);
      values[name] = selected.value;
      metadata[name] = { ...base, value_path_used: selected.path };
    } catch (error) {
      values[name] = undefined;
      metadata[name] = { ...base, parse_error: error.message };
    }
  });
  return { values, metadata };
}
