import { Activity, BriefcaseBusiness, ClipboardList, LayoutDashboard, ScrollText, Settings, Users } from "lucide-react";

const navItems = [
  { id: "dashboard", label: "داشبورد", icon: LayoutDashboard },
  { id: "accounts", label: "اکانت‌ها", icon: Users },
  { id: "jobs", label: "صف عملیات", icon: ClipboardList },
  { id: "campaigns", label: "کمپین‌ها", icon: BriefcaseBusiness },
  { id: "logs", label: "لاگ عملیات", icon: ScrollText },
  { id: "settings", label: "تنظیمات", icon: Settings },
];

export default function Sidebar({ activePage, onNavigate }) {
  return (
    <aside className="sidebar" dir="rtl">
      <div className="brand">
        <div className="brand-mark">
          <Activity size={20} />
        </div>
        <div>
          <p className="brand-title">ClinicOS</p>
          <p className="brand-subtitle">Commercial Queue</p>
        </div>
      </div>

      <nav className="nav-list" aria-label="Primary navigation">
        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <button
              className={`nav-button ${activePage === item.id ? "active" : ""}`}
              key={item.id}
              onClick={() => onNavigate(item.id)}
              type="button"
            >
              <Icon size={18} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>
    </aside>
  );
}
