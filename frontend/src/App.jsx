import { useEffect, useMemo, useState } from "react";
import MainLayout from "./layout/MainLayout.jsx";
import CommercialDashboard from "./pages/CommercialDashboard.jsx";
import CommercialAccounts from "./pages/CommercialAccounts.jsx";
import CommercialJobs from "./pages/CommercialJobs.jsx";
import CommercialCampaigns from "./pages/CommercialCampaigns.jsx";
import CommercialSettings from "./pages/CommercialSettings.jsx";
import CampaignExecutionEntry from "./pages/CampaignExecutionEntry.jsx";
import OperationsAndLogs from "./pages/OperationsAndLogs.jsx";
import BaleAccounts from "./pages/BaleAccounts.jsx";
import ActionErrorNotice from "./components/ActionErrorNotice.jsx";

const pages = {
  dashboard: CommercialDashboard,
  accounts: BaleAccounts,
  jobs: CommercialJobs,
  operations: OperationsAndLogs,
  campaigns: CommercialCampaigns,
  logs: OperationsAndLogs,
  reports: OperationsAndLogs,
  diagnostics: OperationsAndLogs,
  settings: CommercialSettings,
  messaging: CampaignExecutionEntry
};

function pageToHash(pageId) {
  return `#/${pageId}`;
}

function hashToPage() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  if (hash === "platforms" || hash === "baleBulk" || hash.startsWith("platform/")) {
    return "campaigns";
  }
  return hash || "dashboard";
}

function isUiEnabled() {
  const processValue = process.env.UI_ENABLED;
  const viteValue = import.meta.env.VITE_UI_ENABLED;
  const value = processValue ?? viteValue ?? "true";
  return value === true || String(value).toLowerCase() === "true";
}

export default function App() {
  const [activePage, setActivePage] = useState(hashToPage);
  const Page = useMemo(() => pages[activePage] ?? CommercialDashboard, [activePage]);

  function handleNavigate(pageId) {
    setActivePage(pageId);
    window.location.hash = pageToHash(pageId);
  }

  useEffect(() => {
    const syncFromHash = () => {
      const page = hashToPage();
      setActivePage(page);
      if (page === "campaigns" && /^(platforms|baleBulk|platform\/)/.test(window.location.hash.replace(/^#\/?/, ""))) {
        window.history.replaceState(null, "", "#/campaigns");
      }
    };
    syncFromHash();
    window.addEventListener("hashchange", syncFromHash);
    return () => window.removeEventListener("hashchange", syncFromHash);
  }, []);

  if (!isUiEnabled()) {
    return (
      <main className="disabled-shell">
        <section className="disabled-panel">
          <span className="status-dot neutral" />
          <h1>UI disabled - backend only mode</h1>
          <p>Set <code>VITE_UI_ENABLED=true</code> to enable the dashboard.</p>
        </section>
      </main>
    );
  }

  return (
    <MainLayout activePage={activePage} onNavigate={handleNavigate}>
      <Page onNavigate={handleNavigate} />
      <ActionErrorNotice />
    </MainLayout>
  );
}
