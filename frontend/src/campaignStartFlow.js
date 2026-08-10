export function campaignValidationFailureMessage(validation = {}) {
  const blockers = Array.isArray(validation?.blocking_reasons) ? validation.blocking_reasons : [];
  if (blockers.includes("requested_accounts_exceed_runtime_capacity")) {
    const requested = Number(validation?.required_account_count || validation?.requested_account_count || 0);
    const runtimeCapacity = Number(validation?.effective_runtime_capacity ?? validation?.configured_runtime_concurrency);
    const available = Number(validation?.available_runtime_slots ?? runtimeCapacity);
    if (requested > 0 && Number.isFinite(runtimeCapacity)) {
      return `برای اجرای همزمان ${requested.toLocaleString("fa-IR")} حساب، ظرفیت اجرای سیستم روی ${runtimeCapacity.toLocaleString("fa-IR")} تنظیم شده است. ظرفیت در دسترس اکنون ${available.toLocaleString("fa-IR")} است؛ ظرفیت را در تنظیمات افزایش دهید یا تعداد حساب‌های کمپین را کاهش دهید.`;
    }
    return "ظرفیت اجرای همزمان برای تعداد حساب‌های درخواستی کافی نیست.";
  }
  if (blockers.includes("runtime_slots_unavailable")) {
    return "اسلات‌های اجرای سیستم در حال حاضر توسط کمپین دیگری استفاده می‌شوند. بعداً دوباره تلاش کنید.";
  }
  return blockers.join(", ") || "campaign_validation_failed";
}

export async function executeCampaignStart(campaignId, {
  validateCampaignStart,
  finalReviewCampaign,
  queueCampaign,
  startCampaign,
  idempotencyKey,
}) {
  console.info("[CAMPAIGN_START_PHASE] validate", { campaign_id: campaignId });
  const validation = await validateCampaignStart(campaignId);
  const validationSucceeded = validation?.success === true
    && validation?.valid === true
    && validation?.can_start === true
    && validation?.ok === true
    && Array.isArray(validation?.blocking_reasons)
    && validation.blocking_reasons.length === 0;
  if (!validationSucceeded) {
    const recipientCount = Number(validation?.recipient_count || 0);
    const deliverableCount = Number(validation?.deliverable_job_count || 0);
    const removedCount = Number(validation?.duplicate_recipient_count || 0)
      + Number(validation?.invalid_recipient_count || 0)
      + Number(validation?.excluded_recipient_count || 0);
    const message = validation?.blocking_reasons?.includes("campaign_has_no_deliverable_jobs")
      ? `این کمپین ${recipientCount} مخاطب دارد، اما ${deliverableCount} مخاطب قابل ارسال است: ${removedCount} شماره به‌عنوان تکراری، نامعتبر یا مستثنا حذف شده‌اند.`
      : campaignValidationFailureMessage(validation);
    const error = new Error(message);
    error.data = { detail: { error_code: "campaign_validation_failed", error_message: message, validation } };
    throw error;
  }
  const review = await finalReviewCampaign(campaignId, {
    explicit_operator_confirmation: true,
    approved_by: "campaign_ui_operator",
  });
  const blockingErrors = Array.isArray(review?.blocking_errors)
    ? review.blocking_errors
    : Array.isArray(review?.validation?.errors) ? review.validation.errors : [];
  const proofIsValid = review?.ok === true
    && review?.approved === true
    && review?.has_blocking_errors !== true
    && Boolean(review?.review_token)
    && Boolean(review?.final_review_hash);
  if (!proofIsValid) {
    const reasons = blockingErrors.map((item) => {
      const label = item?.field_label_fa || item?.field || "کمپین";
      return `${label}: ${item?.error_code || "خطای بررسی نهایی"}`;
    });
    const message = reasons.length
      ? `بررسی نهایی کمپین تأیید نشد:\n${reasons.join("\n")}`
      : "بررسی نهایی کمپین تأیید نشد و مدرک معتبر صف‌بندی صادر نشد.";
    const error = new Error(message);
    error.data = { detail: { error_code: "final_review_not_approved", final_review: review } };
    throw error;
  }
  console.info("[CAMPAIGN_START_PHASE] queue", { campaign_id: campaignId });
  const queued = await queueCampaign(campaignId, {
    validation_hash: review.validation_hash,
    final_review_hash: review?.final_review_hash,
    review_token: review?.review_token,
    manifest_hash: review?.confirmed_recipients_summary?.manifest_hash,
    idempotency_key: idempotencyKey,
    explicit_operator_confirmation: true,
    expected_campaign_status: "draft",
  });
  if (queued?.success !== true && queued?.ok !== true && queued?.campaign?.status !== "queued") {
    const error = new Error(queued?.error_message || "campaign_queue_failed");
    error.data = { detail: { error_code: queued?.error_code || "campaign_queue_failed", queued } };
    throw error;
  }
  console.info("[CAMPAIGN_START_PHASE] start", { campaign_id: campaignId });
  const started = await startCampaign(campaignId);
  console.info("[CAMPAIGN_START_COMPLETE]", {
    campaign_id: campaignId,
    status: started?.campaign?.status || "running",
  });
  return { validation, review, queued, started };
}
