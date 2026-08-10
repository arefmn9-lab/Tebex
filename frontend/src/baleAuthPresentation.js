export const BALE_AUTH_LABELS = Object.freeze({
  ready: "آماده",
  login_required: "نیاز به ورود",
  checking_session: "در حال بررسی نشست",
  session_problem: "مشکل نشست",
  busy: "مشغول",
  error: "خطا",
  unknown: "نیاز به بررسی",
  // Compatibility keys intentionally resolve to the same compact operator states.
  verification_expired: "نیاز به بررسی",
  otp_required: "نیاز به ورود",
  login_in_progress: "در حال بررسی نشست",
  probe_failure: "خطا",
  initial: "نیاز به بررسی",
  profile_opening: "در حال بررسی نشست",
  opening_profile: "در حال بررسی نشست",
  authenticated_shell_detected: "در حال بررسی نشست",
  identity_probe_running: "در حال بررسی نشست",
  persisting_identity: "در حال بررسی نشست",
  identity_probe_pending: "در حال بررسی نشست",
  identity_verified: "در حال بررسی نشست",
  identity_mismatch: "مشکل نشست",
  auth_probe_inconclusive: "مشکل نشست",
  disabled: "نیاز به بررسی",
  healthy_recent: "آماده",
  probe_due: "نیاز به بررسی",
  temporarily_inconclusive: "مشکل نشست",
  profile_corrupt: "مشکل نشست",
});

export function effectiveAuthBlockers(account = {}) {
  const explicit = [
    ...(Array.isArray(account.eligibility_reasons) ? account.eligibility_reasons : []),
    ...(Array.isArray(account.authentication_blockers) ? account.authentication_blockers : []),
  ];
  if (account.verification_expired && !account.durable_identity_verified) explicit.push("verification_expired");
  return [...new Set(explicit.filter(Boolean))];
}

export function formatBaleAccountActionError(error, fallback = "عملیات اکانت کامل نشد. دوباره تلاش کنید.") {
  const detail = typeof error === "string" ? error : [
    error?.error_code,
    error?.error_message,
    error?.message,
    error?.data?.detail?.error_code,
    error?.data?.detail?.error_message,
    typeof error?.data?.detail === "string" ? error.data.detail : "",
  ].filter(Boolean).join(" ");
  const normalized = detail.toLowerCase();
  if (/(authentication_required|login|otp|verification_code|fake_session_recheck_failure)/.test(normalized)) {
    return "ورود این اکانت تأیید نشد. در صورت نیاز «ورود» را انجام دهید و سپس «بازبینی نشست» را دوباره بزنید.";
  }
  if (/(account_delete_blocked|active_worker_lock|active_profile_operation_lock|active_browser_runtime|assigned_job|running_job)/.test(normalized)) {
    return "اکانت در حال استفاده است و فعلاً حذف نمی‌شود. پس از پایان کار دوباره تلاش کنید.";
  }
  if (/(account_not_found|maintenance_session_not_found)/.test(normalized)) {
    return "اکانت یا بررسی قبلی آن پیدا نشد. فهرست اکانت‌ها را تازه‌سازی کنید و دوباره تلاش کنید.";
  }
  if (/account_operation_interrupted_by_restart/.test(normalized)) {
    return "عملیات قبلی اکانت با راه‌اندازی مجدد سرویس متوقف شد. وضعیت اکانت را تازه‌سازی کنید و در صورت نیاز دوباره تلاش کنید.";
  }
  if (/(profile_path_not_canonical|profile_only_orphan|profile_owned_by_another_account)/.test(normalized)) {
    return "وضعیت این اکانت نیاز به بررسی دارد. از گزینه‌های بیشتر برای عیب‌یابی استفاده کنید.";
  }
  return fallback;
}

export function mapBaleAuthState(account = {}, session = null, busyKey = "") {
  const auth = session?.status?.auth || session?.auth || {};
  const authState = String(auth.auth_state || account.effective_auth_state || account.session_state || account.session_last_known_state || account.authentication_status || "unknown_auth_state");
  const fresh = account.durable_identity_verified === true && account.session_health_acceptable === true;

  // Busy is exclusively an active account operation/resource ownership fact.
  // Historical terminal operations and expired/stale lock metadata must never
  // pin a durable authenticated account in the busy presentation state.
  if (account.operation_busy || account.active_job || account.account_lock || account.profile_lock) return BALE_AUTH_LABELS.busy;
  if (auth.error_code === "auth_probe_failed" || account.profile_probe_result === "auth_probe_failed") return BALE_AUTH_LABELS.error;
  if (["identity_mismatch", "auth_probe_inconclusive", "temporarily_inconclusive", "profile_corrupt"].includes(authState) || ["identity_mismatch", "auth_probe_inconclusive", "auth_probe_failed"].includes(account.profile_probe_result)) return BALE_AUTH_LABELS.session_problem;
  if (["otp_required", "verification_code_required"].includes(authState)) return BALE_AUTH_LABELS.login_required;
  if (["login_required", "unauthenticated", "qr_login_required"].includes(authState)) return BALE_AUTH_LABELS.login_required;
  if (["login_in_progress", "loading", "reconnecting", "profile_opening", "opening_profile", "identity_probe_pending", "identity_probe_running", "persisting_identity", "persisting_session", "activating_account", "authenticated_shell_detected", "identity_verified"].includes(authState) || busyKey.includes(account.account_id)) return BALE_AUTH_LABELS.checking_session;
  if (authState === "authenticated" && fresh) return BALE_AUTH_LABELS.ready;
  if (account.enabled === false || authState === "disabled") return BALE_AUTH_LABELS.unknown;
  return BALE_AUTH_LABELS.unknown;
}
