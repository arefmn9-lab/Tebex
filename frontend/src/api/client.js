import { recordDiagnosticEvent } from "../diagnostics";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8011";
const inFlightGets = new Map();

function timeoutFor(path, method, explicit) {
  if (explicit != null) return Number(explicit);
  if (method !== "GET") return 15000;
  if (/readiness|capacity|validate-start/.test(path)) return 10000;
  return 5000;
}

export class ApiError extends Error {
  constructor(message, status, data, errorCode = "HTTP_APPLICATION_ERROR") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
    this.errorCode = errorCode;
  }
}

async function performRequest(path, options = {}) {
  const started = performance.now();
  const method = options.method || "GET";
  const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
  const headers = {
    ...(isFormData ? {} : { "Content-Type": "application/json" }),
    ...(options.headers || {})
  };
  let response;
  const timeoutMs = timeoutFor(path, method, options.timeoutMs);
  const safeGet = method === "GET";
  const attempts = safeGet ? 2 : 1;
  let lastError;
  const requestOptions = { ...options };
  delete requestOptions.timeoutMs;
  delete requestOptions.deduplicate;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      response = await fetch(`${API_BASE_URL}${path}`, { headers, ...requestOptions, signal: controller.signal });
      window.clearTimeout(timer);
      break;
    } catch (error) {
      window.clearTimeout(timer);
      lastError = error;
      if (attempt < attempts) continue;
    }
  }
  if (!response) {
    const timedOut = lastError?.name === "AbortError";
    const errorCode = timedOut ? "REQUEST_TIMEOUT" : "BACKEND_OFFLINE";
    const elapsedMs = Math.round(performance.now() - started);
    const error = new ApiError(
      timedOut ? `پاسخ بکاند کند است: ${path} (${elapsedMs} ms)` : "Backend is offline",
      0,
      { endpoint: path, elapsed_ms: elapsedMs },
      errorCode,
    );
    const diagnostic = recordDiagnosticEvent({
      action: method, module: "api", endpoint: path, success: false,
      status: timedOut ? "request_timeout" : "backend_offline", error_code: errorCode,
      error_message: error.message, duration_ms: performance.now() - started, stack_trace: lastError?.stack,
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
    const detail = data?.detail;
    const backendErrorCode = detail?.error_code || data?.error_code || `HTTP_${response.status}`;
    const backendErrorMessage = detail?.error_message
      || data?.error_message
      || (typeof detail === "string" ? detail : `Request failed: ${response.status}`);
    const error = new ApiError(
      backendErrorCode ? `${backendErrorCode}: ${backendErrorMessage}` : backendErrorMessage,
      response.status,
      data,
      backendErrorCode,
    );
    const diagnostic = recordDiagnosticEvent({
      action: method, module: "api", endpoint: path, success: false,
      status: String(response.status), error_code: backendErrorCode,
      error_message: error.message, duration_ms: performance.now() - started, stack_trace: error.stack,
    });
    window.dispatchEvent(new CustomEvent("clinicos:action-error", { detail: diagnostic }));
    throw error;
  }

  recordDiagnosticEvent({
    action: method, module: "api", endpoint: path, success: true,
    status: String(response.status), duration_ms: performance.now() - started,
  });
  if (data && typeof data === "object") {
    Object.defineProperty(data, "__http_status", { value: response.status, enumerable: false, configurable: true });
  }
  return data;
}

export function request(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  if (method !== "GET" || options.deduplicate === false) return performRequest(path, options);
  const key = `${method}:${path}`;
  const existing = inFlightGets.get(key);
  if (existing) return existing;
  const promise = performRequest(path, options).finally(() => {
    if (inFlightGets.get(key) === promise) inFlightGets.delete(key);
  });
  inFlightGets.set(key, promise);
  return promise;
}

export function pendingGetCount() {
  return inFlightGets.size;
}

export { API_BASE_URL };
