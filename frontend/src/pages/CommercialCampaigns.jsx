import { ChevronDown, FileSpreadsheet, FileText, Instagram, MessageCircle, PauseCircle, Plus, Radio, RefreshCw, Save, Send, SendHorizontal, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { listPlatformAccounts } from "../api/accountRegistry";
import { createCampaign, listCampaigns, pauseCampaign, queueCampaign, updateCampaign } from "../api/campaigns";
import {
  ContentCard,
  EmptyState,
  FormField,
  InlineError,
  LoadingState,
  NumberInput,
  PrimaryButton,
  SecondaryButton,
  StatusBadge,
  SuccessButton,
  TextInput,
} from "../components/ui/DesignSystem.jsx";

const platformOptions = [
  { id: "bale", label: "Bale", name: "بله", supported: true, icon: MessageCircle, accent: "#1d9bf0" },
  { id: "telegram", label: "Telegram", name: "تلگرام", supported: false, icon: SendHorizontal, accent: "#229ed9" },
  { id: "whatsapp", label: "WhatsApp", name: "واتساپ", supported: false, icon: MessageCircle, accent: "#22a06b" },
  { id: "eitaa", label: "Eitaa", name: "ایتا", supported: false, icon: Radio, accent: "#d9902f" },
  { id: "rubika", label: "Rubika", name: "روبیکا", supported: false, icon: Instagram, accent: "#7c3aed" },
];

const defaultDraft = {
  id: "",
  name: "",
  description: "",
  status: "draft",
  platforms: ["bale"],
  sourceUrls: { bale: "" },
  accountAllocations: { bale: 200 },
  numberFileName: "",
  numberCount: 0,
  deliveriesPerRound: 50,
  dailyLimitPerAccount: 500,
  maxConcurrentAccounts: 2,
  delaySeconds: 4,
};

const statusLabels = {
  draft: "پیش‌نویس",
  ready: "آماده اجرا",
  queued: "آماده اجرا",
  running: "در حال اجرا",
  paused: "متوقف",
  completed: "تمام شده",
  failed: "تمام شده",
  cancelled: "تمام شده",
};

const internalCampaignNamePatterns = [
  /bale controlled batch/i,
  /future advertising campaign template/i,
  /controlled live forward verification/i,
  /phase\s*5f\.1\s*bale contact maintenance/i,
  /phase\s*5e\s*no-send readiness verification/i,
  /verification/i,
  /readiness/i,
  /controlled live/i,
  /contact maintenance/i,
  /test campaign/i,
  /dev/i,
];

function isInternalCampaign(campaign) {
  const title = `${campaignTitle(campaign)} ${campaign.description || campaign.summary || ""}`;
  return internalCampaignNamePatterns.some((pattern) => pattern.test(title));
}

function statusLabel(status) {
  return statusLabels[status] || "پیش‌نویس";
}

function statusTone(status) {
  if (["running", "queued", "ready"].includes(status)) return "success";
  if (status === "paused") return "warning";
  if (["failed", "cancelled"].includes(status)) return "danger";
  if (status === "completed") return "info";
  return "neutral";
}

function campaignId(campaign) {
  return campaign.id || campaign.campaign_id || "";
}

function campaignTitle(campaign) {
  return campaign.name || campaign.title || campaign.campaign_name || "کمپین بدون نام";
}

function getPolicy(campaign) {
  return campaign.policy_overrides || campaign.policy || {};
}

function normalizeCampaign(campaign) {
  const policy = getPolicy(campaign);
  const selectedPlatforms = campaign.platforms || policy.selected_platforms || [campaign.platform || "bale"];
  const sourceUrls = policy.platform_source_urls || campaign.platform_source_urls || {};
  if (!Object.keys(sourceUrls).length && (campaign.source_url || campaign.source_channel_url)) {
    sourceUrls[selectedPlatforms[0] || "bale"] = campaign.source_url || campaign.source_channel_url;
  }

  return {
    ...defaultDraft,
    id: campaignId(campaign),
    name: campaignTitle(campaign),
    description: campaign.description || campaign.summary || "",
    status: campaign.status || "draft",
    platforms: selectedPlatforms,
    sourceUrls,
    accountAllocations: policy.account_allocations || campaign.account_allocations || { bale: 200 },
    numberFileName: campaign.number_file_name || policy.number_file_name || "",
    numberCount: campaign.number_count || campaign.contact_count || campaign.recipient_count || policy.number_count || 0,
    deliveriesPerRound: policy.deliveries_per_round ?? 50,
    dailyLimitPerAccount: policy.daily_limit_per_account ?? 500,
    maxConcurrentAccounts: policy.max_concurrent_accounts ?? 2,
    delaySeconds: policy.delay_between_deliveries_seconds ?? 4,
  };
}

function payloadFromDraft(draft) {
  const selectedSourceUrls = Object.fromEntries(draft.platforms.map((platformId) => [platformId, draft.sourceUrls[platformId] || ""]));
  const selectedAllocations = Object.fromEntries(draft.platforms.map((platformId) => [platformId, draft.accountAllocations[platformId] || 0]));
  return {
    name: draft.name.trim(),
    description: draft.description.trim() || null,
    platform: draft.platforms[0] || "bale",
    status: draft.status,
    source_url: selectedSourceUrls[draft.platforms[0] || "bale"] || null,
    policy_overrides: {
      selected_platforms: draft.platforms,
      platform_source_urls: selectedSourceUrls,
      account_allocations: selectedAllocations,
      number_file_name: draft.numberFileName || null,
      number_count: Number(draft.numberCount || 0),
      deliveries_per_round: Number(draft.deliveriesPerRound || 0),
      daily_limit_per_account: Number(draft.dailyLimitPerAccount || 0),
      max_concurrent_accounts: Number(draft.maxConcurrentAccounts || 0),
      delay_between_deliveries_seconds: Number(draft.delaySeconds || 0),
    },
  };
}

function fileCountHint(file) {
  if (!file) return 0;
  return Math.max(0, Math.round(file.size / 28));
}

function isActiveAccount(account) {
  const status = String(account.status || account.auth_status || account.authentication_state || "").toLowerCase();
  if (!status) return true;
  return ["active", "authenticated", "ready", "enabled"].includes(status);
}

function isAvailableAccount(account) {
  const inUse = account.in_use || account.profile_in_use || account.lease_active || account.locked;
  return isActiveAccount(account) && !inUse;
}

function poolStats(accounts = []) {
  return {
    total: accounts.length,
    active: accounts.filter(isActiveAccount).length,
    available: accounts.filter(isAvailableAccount).length,
  };
}

function CampaignDetails({ campaign }) {
  const normalized = normalizeCampaign(campaign);
  const operations = campaign.operations_summary || campaign.summary || {};

  return (
    <div className="campaign-details-grid">
      <p><span>پلتفرم‌ها</span><b>{normalized.platforms.map((id) => platformOptions.find((item) => item.id === id)?.name || id).join("، ")}</b></p>
      <p><span>تخصیص اکانت</span><b>{Object.entries(normalized.accountAllocations).map(([key, value]) => `${key}: ${value}`).join("، ") || "ثبت نشده"}</b></p>
      <p><span>منبع ارسال</span><b>{Object.values(normalized.sourceUrls).filter(Boolean).length || 0} لینک</b></p>
      <p><span>بانک شماره</span><b>{normalized.numberFileName || "فایلی انتخاب نشده"} · {normalized.numberCount} شماره</b></p>
      <p><span>محدودیت‌ها</span><b>{normalized.deliveriesPerRound} هر دور · {normalized.dailyLimitPerAccount} روزانه · {normalized.maxConcurrentAccounts} همزمان</b></p>
      <p><span>وضعیت</span><b>{statusLabel(normalized.status)}</b></p>
      <p><span>خلاصه عملیات</span><b>{operations.completed || 0} انجام‌شده · {operations.failed || 0} خطا · {operations.pending || 0} در انتظار</b></p>
    </div>
  );
}

function platformMeta(platformId) {
  return platformOptions.find((item) => item.id === platformId) || { id: platformId, label: platformId, name: platformId, icon: MessageCircle, accent: "#2947b6" };
}

function platformSummary(platforms = []) {
  return platforms.map((id) => platformMeta(id).name).join("، ");
}

function sourcePlaceholder(platformId) {
  const placeholders = {
    bale: "https://web.bale.ai/chat?uid=...",
    telegram: "https://t.me/channel-or-post",
    whatsapp: "https://chat.whatsapp.com/...",
    eitaa: "https://eitaa.com/channel",
    rubika: "https://rubika.ir/channel",
  };
  return placeholders[platformId] || "https://...";
}

export default function CommercialCampaigns() {
  const [campaigns, setCampaigns] = useState([]);
  const [accountsByPlatform, setAccountsByPlatform] = useState({});
  const [draft, setDraft] = useState(defaultDraft);
  const [expandedCampaignId, setExpandedCampaignId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const connectedPlatforms = useMemo(() => {
    const map = {};
    for (const platform of platformOptions) {
      map[platform.id] = (accountsByPlatform[platform.id] || []).length > 0;
    }
    return map;
  }, [accountsByPlatform]);

  const visibleCampaigns = useMemo(() => campaigns.filter((campaign) => !isInternalCampaign(campaign)), [campaigns]);
  const campaignStats = useMemo(() => {
    const running = visibleCampaigns.filter((campaign) => campaign.status === "running").length;
    const draft = visibleCampaigns.filter((campaign) => !campaign.status || campaign.status === "draft").length;
    const platforms = new Set(visibleCampaigns.flatMap((campaign) => normalizeCampaign(campaign).platforms));
    return { total: visibleCampaigns.length, running, draft, platforms: platforms.size };
  }, [visibleCampaigns]);

  function statsForPlatform(platformId) {
    return poolStats(accountsByPlatform[platformId] || []);
  }

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [campaignData, baleAccounts] = await Promise.all([
        listCampaigns({ limit: 100, offset: 0 }),
        Promise.all(platformOptions.map((platform) => listPlatformAccounts(platform.id).catch(() => []))),
      ]);
      const rows = Array.isArray(campaignData) ? campaignData : campaignData?.items || [];
      const accountsMap = Object.fromEntries(
        platformOptions.map((platform, index) => {
          const payload = baleAccounts[index];
          return [platform.id, Array.isArray(payload) ? payload : payload?.items || []];
        }),
      );
      setCampaigns(rows);
      setAccountsByPlatform(accountsMap);
    } catch (err) {
      setError(err.message || "دریافت کمپین‌ها انجام نشد.");
    } finally {
      setLoading(false);
    }
  }

  function updateDraft(patch) {
    setDraft((current) => ({ ...current, ...patch }));
  }

  function startNewCampaign() {
    setDraft({ ...defaultDraft, sourceUrls: { bale: "" }, accountAllocations: { bale: 200 } });
    setNotice("");
    setError("");
  }

  function editCampaign(campaign) {
    setDraft(normalizeCampaign(campaign));
    setExpandedCampaignId(campaignId(campaign));
    setNotice("");
    setError("");
  }

  function togglePlatform(platformId) {
    const selected = draft.platforms.includes(platformId);
    const nextPlatforms = selected ? draft.platforms.filter((id) => id !== platformId) : [...draft.platforms, platformId];
    const safePlatforms = nextPlatforms.length ? nextPlatforms : ["bale"];
    const nextSourceUrls = Object.fromEntries(safePlatforms.map((id) => [id, draft.sourceUrls[id] || ""]));
    const nextAllocations = Object.fromEntries(safePlatforms.map((id) => [id, draft.accountAllocations[id] || 0]));
    updateDraft({
      platforms: safePlatforms,
      sourceUrls: nextSourceUrls,
      accountAllocations: nextAllocations,
    });
  }

  function setPlatformSource(platformId, value) {
    updateDraft({ sourceUrls: { ...draft.sourceUrls, [platformId]: value } });
  }

  function setPlatformAllocation(platformId, value) {
    const requested = Math.max(0, Number(value || 0));
    const available = statsForPlatform(platformId).available;
    updateDraft({ accountAllocations: { ...draft.accountAllocations, [platformId]: Math.min(requested, available) } });
  }

  function handleNumberFile(file) {
    if (!file) return;
    updateDraft({ numberFileName: file.name, numberCount: fileCountHint(file) });
  }

  async function saveDraft() {
    if (!draft.name.trim()) {
      setError("نام کمپین را وارد کنید.");
      return;
    }
    setBusy("save");
    setError("");
    setNotice("");
    try {
      const saved = draft.id ? await updateCampaign(draft.id, payloadFromDraft(draft)) : await createCampaign(payloadFromDraft(draft));
      setNotice("کمپین ذخیره شد.");
      const savedId = saved?.id || saved?.campaign_id || draft.id;
      await load();
      if (savedId) setDraft((current) => ({ ...current, id: savedId }));
    } catch (err) {
      setError(err.message || "ذخیره کمپین انجام نشد.");
    } finally {
      setBusy("");
    }
  }

  async function runCampaign(campaign) {
    const id = campaignId(campaign);
    if (!id) return;
    setBusy(`run:${id}`);
    setError("");
    setNotice("");
    try {
      await queueCampaign(id);
      setNotice("درخواست اجرای کمپین با قرارداد فعلی ثبت شد.");
      await load();
    } catch (err) {
      setError(err.message || "اجرای کمپین انجام نشد.");
    } finally {
      setBusy("");
    }
  }

  async function stopCampaign(campaign) {
    const id = campaignId(campaign);
    if (!id) return;
    setBusy(`stop:${id}`);
    setError("");
    setNotice("");
    try {
      await pauseCampaign(id);
      setNotice("درخواست توقف کمپین ثبت شد.");
      await load();
    } catch (err) {
      setError(err.message || "توقف کمپین انجام نشد.");
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (!Object.keys(accountsByPlatform).length) return;
    const nextAllocations = { ...draft.accountAllocations };
    let changed = false;
    for (const platformId of draft.platforms) {
      const available = statsForPlatform(platformId).available;
      const current = Number(nextAllocations[platformId] || 0);
      if (current > available) {
        nextAllocations[platformId] = available;
        changed = true;
      }
    }
    if (changed) updateDraft({ accountAllocations: nextAllocations });
  }, [accountsByPlatform, draft.platforms, draft.accountAllocations]);

  return (
    <section className="campaign-builder-page" dir="rtl">
      <div className="premium-campaign-hero">
        <div>
          <span className="campaign-eyebrow">مرکز کمپین</span>
          <h1>کمپین‌ها</h1>
          <p>کمپین را از یک نقطه بسازید، پیام‌رسان‌ها را انتخاب کنید، ظرفیت Pool اکانت‌ها را تخصیص دهید و اجرای کمپین را از قرارداد فعلی مدیریت کنید.</p>
        </div>
        <div className="premium-hero-actions">
          <SecondaryButton onClick={load} disabled={loading}><RefreshCw size={17} />تازه‌سازی</SecondaryButton>
          <PrimaryButton onClick={startNewCampaign}><Plus size={17} />ساخت کمپین جدید</PrimaryButton>
        </div>
        {campaignStats.total ? (
          <div className="premium-campaign-stats" aria-label="خلاصه کمپین‌ها">
            <article><span>کل کمپین‌ها</span><strong>{campaignStats.total}</strong></article>
            <article><span>در حال اجرا</span><strong>{campaignStats.running}</strong></article>
            <article><span>پیش‌نویس</span><strong>{campaignStats.draft}</strong></article>
            <article><span>پیام‌رسان‌ها</span><strong>{campaignStats.platforms}</strong></article>
          </div>
        ) : null}
      </div>

      {error ? <InlineError>{error}</InlineError> : null}
      {notice ? <div className="toast">{notice}</div> : null}
      {loading ? <LoadingState label="در حال دریافت کمپین‌ها" /> : null}

      {!loading && !visibleCampaigns.length ? (
        <EmptyState
          title="هنوز کمپینی ایجاد نکرده‌اید"
          action={<PrimaryButton onClick={startNewCampaign}><Plus size={17} />ساخت کمپین جدید</PrimaryButton>}
        />
      ) : null}

      <div className={`premium-campaign-layout ${visibleCampaigns.length ? "" : "workspace-only"}`}>
        <div className="campaign-overview-column">
      {!loading && visibleCampaigns.length ? (
        <ContentCard title="نمای کلی کمپین‌ها" description="کمپین‌ها بدون شناسه‌های داخلی نمایش داده می‌شوند؛ برای جزئیات هر ردیف را باز کنید.">
          <div className="simple-campaign-list">
            {visibleCampaigns.map((campaign) => {
              const id = campaignId(campaign);
              const expanded = expandedCampaignId === id;
              const normalized = normalizeCampaign(campaign);
              return (
                <article className="simple-campaign-item" key={id || campaign.name}>
                  <button className="simple-campaign-main" type="button" onClick={() => setExpandedCampaignId(expanded ? "" : id)}>
                    <span>
                      <strong>{campaignTitle(campaign)}</strong>
                      <small>{campaign.description || "بدون توضیحات"}</small>
                    </span>
                    <span className="campaign-row-meta">
                      <StatusBadge tone={statusTone(campaign.status)}>{statusLabel(campaign.status)}</StatusBadge>
                      <span className="campaign-platform-icons">
                        {normalized.platforms.slice(0, 4).map((platformId) => {
                          const meta = platformMeta(platformId);
                          const Icon = meta.icon;
                          return <span key={platformId} style={{ "--platform-accent": meta.accent }} title={meta.name}><Icon size={15} /></span>;
                        })}
                      </span>
                      <small>{normalized.numberCount || 0} شماره · {Object.values(normalized.accountAllocations).reduce((sum, value) => sum + Number(value || 0), 0)} اکانت</small>
                    </span>
                    <ChevronDown className={expanded ? "open" : ""} size={18} />
                  </button>
                  <div className="campaign-inline-actions">
                    <SuccessButton disabled={Boolean(busy)} onClick={() => runCampaign(campaign)}><Send size={16} />اجرای کمپین</SuccessButton>
                    <SecondaryButton disabled={Boolean(busy) || campaign.status !== "running"} onClick={() => stopCampaign(campaign)}><PauseCircle size={16} />توقف کمپین</SecondaryButton>
                    <SecondaryButton disabled title="قرارداد حذف کمپین در API فعلی موجود نیست"><Trash2 size={16} />حذف کمپین</SecondaryButton>
                  </div>
                  {expanded ? (
                    <div className="simple-campaign-details">
                      <CampaignDetails campaign={campaign} />
                      <SecondaryButton onClick={() => editCampaign(campaign)}>ویرایش این کمپین</SecondaryButton>
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        </ContentCard>
      ) : null}
        </div>

      <div className="campaign-workspace-column">
      <div className="business-builder-grid single">
        <div className="business-builder-main">
          <div className="builder-section-label">
            <span>مسیر ساخت کمپین</span>
            <strong>ساخت و ویرایش کمپین</strong>
          </div>

          <ContentCard title="۱. اطلاعات کمپین" description="نام و توضیح کوتاه برای تشخیص سریع کمپین در لیست.">
            <div className="business-form-grid simple">
              <FormField label="نام کمپین">
                <TextInput value={draft.name} onChange={(event) => updateDraft({ name: event.target.value })} placeholder="نام کمپین" />
              </FormField>
              <FormField label="توضیحات">
                <TextInput value={draft.description} onChange={(event) => updateDraft({ description: event.target.value })} placeholder="توضیح کوتاه" />
              </FormField>
            </div>
          </ContentCard>

          <ContentCard title="۲. انتخاب پیام‌رسان‌ها" description="هر پیام‌رسان انتخاب‌شده تنظیمات منبع و Pool خودش را دریافت می‌کند.">
            <div className="platform-checkbox-grid">
              {platformOptions.map((platform) => (
                (() => {
                  const Icon = platform.icon;
                  return (
                <button
                  aria-pressed={draft.platforms.includes(platform.id)}
                  className={draft.platforms.includes(platform.id) ? "selected" : ""}
                  key={platform.id}
                  type="button"
                  style={{ "--platform-accent": platform.accent }}
                  onClick={() => togglePlatform(platform.id)}
                >
                  <span className="platform-logo"><Icon size={18} /></span>
                  <input checked={draft.platforms.includes(platform.id)} readOnly type="checkbox" />
                  <span><b>{platform.label}</b><em>{platform.name}</em></span>
                  <small>{connectedPlatforms[platform.id] ? "آماده" : "نیاز به اتصال"}</small>
                </button>
                  );
                })()
              ))}
            </div>
          </ContentCard>

          <ContentCard title="۳. تخصیص ظرفیت اکانت‌ها" description="اعداد واقعی از API اکانت‌ها خوانده می‌شود؛ اکانت‌ها به صورت تکی انتخاب نمی‌شوند.">
            <div className="platform-pool-grid">
              {draft.platforms.map((platformId) => {
                const platform = platformMeta(platformId);
                const stats = statsForPlatform(platformId);
                const Icon = platform?.icon || MessageCircle;
                return (
                  <article className="platform-pool-card" key={platformId} style={{ "--platform-accent": platform?.accent || "var(--primary)" }}>
                    <div className="platform-pool-head">
                      <span className="platform-logo"><Icon size={22} /></span>
                      <div>
                        <strong>{platform?.label || platformId}</strong>
                        <small>{platform?.name || platformId}</small>
                      </div>
                      <StatusBadge tone={connectedPlatforms[platformId] ? "success" : "warning"}>
                        {connectedPlatforms[platformId] ? "آماده" : "نیاز به اتصال"}
                      </StatusBadge>
                    </div>
                    <div className="platform-pool-stats">
                      <p><span>کل اکانت‌ها</span><b>{stats.total}</b></p>
                      <p><span>فعال</span><b>{stats.active}</b></p>
                      <p><span>آماده</span><b>{stats.available}</b></p>
                    </div>
                    <FormField label="اختصاص به این کمپین">
                      <NumberInput min="0" max={stats.available} value={draft.accountAllocations[platformId] || 0} onChange={(event) => setPlatformAllocation(platformId, event.target.value)} />
                    </FormField>
                  </article>
                );
              })}
            </div>
          </ContentCard>

          <ContentCard title="۴. منابع ارسال" description="برای هر پیام‌رسان انتخاب‌شده دقیقاً یک URL منبع نمایش داده می‌شود.">
            <div className="platform-source-list">
              {draft.platforms.map((platformId) => {
                const platform = platformMeta(platformId);
                const Icon = platform.icon;
                const hasSource = Boolean((draft.sourceUrls[platformId] || "").trim());
                return (
                  <article className={`platform-source-card ${hasSource ? "filled" : ""}`} key={platformId} style={{ "--platform-accent": platform.accent }}>
                    <div className="platform-source-head">
                      <span className="platform-logo"><Icon size={20} /></span>
                      <div>
                        <strong>{platform.label}</strong>
                        <small>{hasSource ? "منبع ثبت شده" : "در انتظار URL"}</small>
                      </div>
                    </div>
                    <FormField label="Source URL">
                      <TextInput value={draft.sourceUrls[platformId] || ""} onChange={(event) => setPlatformSource(platformId, event.target.value)} placeholder={sourcePlaceholder(platformId)} />
                    </FormField>
                  </article>
                );
              })}
            </div>
          </ContentCard>

          <ContentCard title="۵. گیرندگان" description="فقط فایل CSV یا XLSX به کمپین وصل می‌شود؛ مدیریت کامل شماره‌ها در ماژول بانک شماره است.">
            <div className="number-upload-grid">
              <label className="number-upload-card">
                <FileText size={22} />
                <strong>CSV upload</strong>
                <span>انتخاب فایل CSV</span>
                <input accept=".csv,text/csv" type="file" onChange={(event) => handleNumberFile(event.target.files?.[0])} />
              </label>
              <label className="number-upload-card">
                <FileSpreadsheet size={22} />
                <strong>Excel upload</strong>
                <span>انتخاب فایل Excel</span>
                <input accept=".xlsx,.xls" type="file" onChange={(event) => handleNumberFile(event.target.files?.[0])} />
              </label>
              <article className="number-file-summary">
                <span>فایل</span>
                <strong>{draft.numberFileName || "انتخاب نشده"}</strong>
                <small>{draft.numberCount} شماره</small>
              </article>
            </div>
          </ContentCard>

          <ContentCard title="۶. تنظیمات اجرا" description="اگر مقدار ۲ باشد، هر پیام‌رسان می‌تواند هم‌زمان از ۲ اکانت استفاده کند. این سقف بین پلتفرم‌ها مشترک نمی‌شود.">
            <div className="business-settings-grid">
              <FormField label="ارسال در هر دور">
                <NumberInput min="1" value={draft.deliveriesPerRound} onChange={(event) => updateDraft({ deliveriesPerRound: event.target.value })} />
              </FormField>
              <FormField label="محدودیت روزانه هر اکانت">
                <NumberInput min="1" value={draft.dailyLimitPerAccount} onChange={(event) => updateDraft({ dailyLimitPerAccount: event.target.value })} />
              </FormField>
              <FormField label="حداکثر اکانت همزمان">
                <NumberInput min="1" value={draft.maxConcurrentAccounts} onChange={(event) => updateDraft({ maxConcurrentAccounts: event.target.value })} />
              </FormField>
              <FormField label="مکث بین عملیات">
                <NumberInput min="0" value={draft.delaySeconds} onChange={(event) => updateDraft({ delaySeconds: event.target.value })} />
              </FormField>
            </div>
          </ContentCard>

          <ContentCard title="۷. ذخیره کمپین" description={`پلتفرم‌های انتخاب‌شده: ${platformSummary(draft.platforms) || "هیچ"}`}>
          <div className="builder-save-actions">
            <SecondaryButton onClick={saveDraft} disabled={Boolean(busy)}>
              <Save size={17} />
              {busy === "save" ? "در حال ذخیره" : "ذخیره پیش‌نویس"}
            </SecondaryButton>
          </div>
          </ContentCard>
        </div>
      </div>
      </div>
      </div>
    </section>
  );
}
