import { Layers3, MessageSquarePlus } from "lucide-react";
import { useMemo, useState } from "react";
import {
  ContentCard,
  PageHeader,
  PlatformCard,
  PlatformSelector as PlatformSelectorGrid,
  PrimaryButton,
  SecondaryButton,
  StatusBadge,
} from "../components/ui/DesignSystem.jsx";
import { platforms } from "../data/platforms";

export default function PlatformSelector({ onNavigate }) {
  const [multiMode, setMultiMode] = useState(false);
  const [selected, setSelected] = useState(["bale"]);
  const selectedPlatforms = useMemo(
    () => platforms.filter((platform) => selected.includes(platform.id)),
    [selected]
  );

  function togglePlatform(platform) {
    if (!multiMode) {
      if (platform.ready) onNavigate(`platform:${platform.id}`);
      return;
    }

    setSelected((current) => (
      current.includes(platform.id)
        ? current.filter((id) => id !== platform.id)
        : [...current, platform.id]
    ));
  }

  return (
    <section className="rtl-page platform-selection-page" dir="rtl">
      <PageHeader
        title="ارسال پیام"
        description="پلتفرم مقصد را انتخاب کنید. بله آماده اجرا است و سایر پیام‌رسان‌ها برای مسیر چندپیام‌رسانی آینده نمایش داده می‌شوند."
        actions={(
          <div className="segmented-control" aria-label="حالت انتخاب پلتفرم">
            <button className={!multiMode ? "active" : ""} onClick={() => setMultiMode(false)} type="button">تکی</button>
            <button className={multiMode ? "active" : ""} onClick={() => setMultiMode(true)} type="button">چند پلتفرم</button>
          </div>
        )}
      />

      <ContentCard
        title="انتخاب پیام‌رسان"
        description="هر گیرنده در هر پیام‌رسان انتخاب‌شده وضعیت مستقل خواهد داشت."
        actions={<StatusBadge tone="info">{selectedPlatforms.length} انتخاب</StatusBadge>}
      >
        <PlatformSelectorGrid>
          {platforms.map((platform) => (
            <PlatformCard
              disabled={!multiMode && !platform.ready}
              key={platform.id}
              onClick={() => togglePlatform(platform)}
              platform={platform}
              selected={selected.includes(platform.id)}
            />
          ))}
        </PlatformSelectorGrid>

        <div className="platform-selection-actions">
          {multiMode ? (
            <PrimaryButton disabled={selected.length < 2}>
              <Layers3 size={18} />
              ساخت کمپین چندپیام‌رسانی
            </PrimaryButton>
          ) : (
            <SecondaryButton onClick={() => onNavigate("platform:bale")}>
              <MessageSquarePlus size={18} />
              ورود به workspace بله
            </SecondaryButton>
          )}
          <p>پلتفرم‌های غیرآماده در این فاز اجرا نمی‌شوند و فقط برای طراحی مسیر آینده نمایش داده شده‌اند.</p>
        </div>
      </ContentCard>
    </section>
  );
}
