import {
  Activity,
  BookUser,
  BriefcaseBusiness,
  LayoutDashboard,
  LogOut,
  Users,
} from "lucide-react";

const navItems = [
  { id: "dashboard", label: "داشبورد", icon: LayoutDashboard },
  { id: "campaigns", label: "کمپین‌ها", icon: BriefcaseBusiness },
  { id: "accounts", label: "اکانت‌ها", icon: Users },
  { id: "numberBank", label: "بانک شماره", icon: BookUser, disabled: true },
  { id: "operations", label: "مرکز عملیات", icon: Activity },
];

export { navItems };

export default function Sidebar({ activePage, onNavigate }) {
  const normalizedActive = ["jobs", "logs", "reports", "diagnostics"].includes(activePage) ? "operations" : activePage;

  return (
    <aside className="sidebar" dir="rtl">
      <div className="brand">
        <div className="brand-mark">
          <Activity size={20} />
        </div>
        <div>
          <p className="brand-title">ClinicOS</p>
          <p className="brand-subtitle">مدیریت کمپین</p>
        </div>
      </div>

      <nav className="nav-list" aria-label="ناوبری اصلی">
        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <button
              aria-current={normalizedActive === item.id ? "page" : undefined}
              className={`nav-button ${normalizedActive === item.id ? "active" : ""}`}
              disabled={item.disabled}
              key={item.id}
              onClick={() => onNavigate(item.id)}
              title={item.disabled ? "در فاز بعدی تکمیل می‌شود" : item.label}
              type="button"
            >
              <Icon size={18} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>

      <div className="sidebar-account">
        <div className="account-avatar">ا</div>
        <div>
          <strong>خوش آمدید</strong>
          <span>مدیر سیستم</span>
        </div>
        <LogOut size={16} aria-hidden="true" />
      </div>
    </aside>
  );
}
