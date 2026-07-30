import { recordDiagnosticEvent } from "../diagnostics";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8011";

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

export async function request(path, options = {}) {
  const started = performance.now();
  const method = options.method || "GET";
  const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
  const headers = {
    ...(isFormData ? {} : { "Content-Type": "application/json" }),
    ...(options.headers || {})
  };
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { headers, ...options });
  } catch (error) {
    const diagnostic = recordDiagnosticEvent({
      action: method, module: "api", endpoint: path, success: false,
      status: "network_error", error_code: "NETWORK_ERROR",
      error_message: error.message, duration_ms: performance.now() - started, stack_trace: error.stack,
    });
    window.dispatchEvent(new CustomEvent("clinicos:action-error", { detail: diagnostic }));
    throw error;
  }

  let data = null;
  const text = await response.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!response.ok) {
    const error = new ApiError(
      typeof data?.detail === "string" ? data.detail : `Request failed: ${response.status}`,
      response.status,
      data
    );
    const diagnostic = recordDiagnosticEvent({
      action: method, module: "api", endpoint: path, success: false,
      status: String(response.status), error_code: data?.error_code || `HTTP_${response.status}`,
      error_message: error.message, duration_ms: performance.now() - started, stack_trace: error.stack,
    });
    window.dispatchEvent(new CustomEvent("clinicos:action-error", { detail: diagnostic }));
    throw error;
  }

  recordDiagnosticEvent({
    action: method, module: "api", endpoint: path, success: true,
    status: String(response.status), duration_ms: performance.now() - started,
  });
  return data;
}

export { API_BASE_URL };
