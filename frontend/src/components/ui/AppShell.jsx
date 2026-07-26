import { Activity, Bell, Menu, RefreshCw } from "lucide-react";
import { useState } from "react";
import Sidebar from "../Sidebar.jsx";
import TopHeader from "../TopBar.jsx";
import { IconButton } from "./DesignSystem.jsx";

export default function AppShell({ activePage, onNavigate, children }) {
  const [drawerOpen, setDrawerOpen] = useState(false);

  function navigate(pageId) {
    onNavigate(pageId);
    setDrawerOpen(false);
  }

  return (
    <div className={`app-shell ${drawerOpen ? "mobile-nav-open" : ""}`} dir="rtl">
      <Sidebar activePage={activePage} onNavigate={navigate} collapsed={false} />
      <div className="mobile-nav-backdrop" onClick={() => setDrawerOpen(false)} role="presentation" />
      <main className="main-column">
        <TopHeader
          actions={(
            <>
              <IconButton label="بازکردن منو" onClick={() => setDrawerOpen(true)}><Menu size={18} /></IconButton>
              <IconButton label="اعلان‌ها"><Bell size={18} /><span className="notification-dot">3</span></IconButton>
              <IconButton label="تازه‌سازی وضعیت"><RefreshCw size={18} /></IconButton>
            </>
          )}
          statusIcon={<Activity size={17} />}
        />
        <div className="content">{children}</div>
      </main>
    </div>
  );
}
