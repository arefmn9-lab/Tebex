import { Activity, ClipboardList, LayoutDashboard, ScrollText, SendHorizonal, Users } from "lucide-react";
import { platforms } from "../data/platforms";

const navItems = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "tasks", label: "Tasks", icon: ClipboardList },
  { id: "accounts", label: "Accounts", icon: Users },
  { id: "logs", label: "Logs", icon: ScrollText }
];

export default function Sidebar({ activePage, onNavigate }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          <Activity size={20} />
        </div>
        <div>
          <p className="brand-title">ClinicOS</p>
          <p className="brand-subtitle">Automation Platform</p>
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

        <div className="nav-section" dir="rtl">
          <button
            className={`nav-button ${activePage === "messaging" ? "active" : ""}`}
            onClick={() => onNavigate("messaging")}
            type="button"
          >
            <SendHorizonal size={18} />
            <span>ارسال پیام</span>
          </button>
          <div className="platform-nav">
            {platforms.map((platform) => {
              const Icon = platform.icon;
              return (
                <button
                  className={`nav-button platform-nav-button ${activePage === `platform:${platform.id}` ? "active" : ""}`}
                  key={platform.id}
                  onClick={() => onNavigate(`platform:${platform.id}`)}
                  type="button"
                >
                  <Icon size={16} />
                  <span>{platform.name}</span>
                </button>
              );
            })}
          </div>
        </div>
      </nav>
    </aside>
  );
}
