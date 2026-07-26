export default function TopHeader({ actions, statusIcon }) {
  return (
    <header className="topbar" dir="rtl">
      <div>
        <h1 className="topbar-title">مدیریت پیام‌رسانی ClinicOS</h1>
        <p className="topbar-copy">وضعیت سامانه و عملیات ارسال</p>
      </div>
      <div className="topbar-meta">
        <span className="connection-pill">
          {statusIcon}
          متصل
        </span>
        {actions}
      </div>
    </header>
  );
}
