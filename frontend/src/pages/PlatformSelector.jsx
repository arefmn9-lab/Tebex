import { ChevronLeft } from "lucide-react";
import { platforms } from "../data/platforms";

export default function PlatformSelector({ onNavigate }) {
  return (
    <section className="rtl-page" dir="rtl">
      <div className="page-header">
        <div>
          <h2 className="page-title">ارسال پیام</h2>
          <p className="page-copy">یک پلتفرم را برای مدیریت اکانت‌ها، وظایف و گزارش‌های ارسال انتخاب کنید.</p>
        </div>
      </div>

      <div className="platform-grid">
        {platforms.map((platform) => {
          const Icon = platform.icon;
          return (
            <button
              className="platform-card"
              key={platform.id}
              onClick={() => onNavigate(`platform:${platform.id}`)}
              type="button"
            >
              <span className="platform-icon">
                <Icon size={22} />
              </span>
              <span>
                <strong>{platform.name}</strong>
                <small>ورود به پنل کنترل ارسال</small>
              </span>
              <ChevronLeft size={18} />
            </button>
          );
        })}
      </div>
    </section>
  );
}
