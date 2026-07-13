import { RefreshCw, Server } from "lucide-react";
import { API_BASE_URL } from "../api/client";

export default function TopBar() {
  return (
    <header className="topbar" dir="rtl">
      <div>
        <h1 className="topbar-title">کنترل تجاری ارسال Bale</h1>
      </div>
      <div className="topbar-meta">
        <span className="pill">
          <Server size={14} />
          {API_BASE_URL}
        </span>
        <span className="pill">
          <RefreshCw size={14} />
          تازه‌سازی دستی
        </span>
      </div>
    </header>
  );
}
