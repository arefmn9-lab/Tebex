import { useEffect, useMemo, useState } from "react";
import MainLayout from "./layout/MainLayout.jsx";
import CommercialDashboard from "./pages/CommercialDashboard.jsx";
import CommercialAccounts from "./pages/CommercialAccounts.jsx";
import CommercialJobs from "./pages/CommercialJobs.jsx";
import CommercialCampaigns from "./pages/CommercialCampaigns.jsx";
import CommercialLogs from "./pages/CommercialLogs.jsx";
import CommercialSettings from "./pages/CommercialSettings.jsx";
import BaleBulkCampaigns from "./pages/BaleBulkCampaigns.jsx";
import Diagnostics from "./pages/Diagnostics.jsx";
import BaleWorkspace from "./pages/BaleWorkspace.jsx";
import OperationsAndLogs from "./pages/OperationsAndLogs.jsx";
import SimpleAccounts from "./pages/SimpleAccounts.jsx";
import PlatformSelector from "./pages/PlatformSelector.jsx";
import PlatformWorkspace from "./pages/PlatformWorkspace.jsx";

const pages = {
  dashboard: CommercialDashboard,
  accounts: SimpleAccounts,
  jobs: CommercialJobs,
  operations: OperationsAndLogs,
  campaigns: CommercialCampaigns,
  baleBulk: BaleBulkCampaigns,
  logs: CommercialLogs,
  reports: CommercialLogs,
  settings: CommercialSettings,
  diagnostics: Diagnostics,
  messaging: PlatformSelector,
  platforms: PlatformSelector
};

function pageToHash(pageId) {
  if (pageId.startsWith("platform:")) {
    return `#/platform/${pageId.split(":")[1]}`;
  }
  return `#/${pageId}`;
}

function hashToPage() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  if (hash.startsWith("platform/")) {
    return `platform:${hash.split("/")[1]}`;
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
    const syncFromHash = () => setActivePage(hashToPage());
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
      {activePage === "platform:bale" ? (
        <BaleWorkspace />
      ) : activePage.startsWith("platform:") ? (
        <PlatformWorkspace platformId={activePage.split(":")[1]} />
      ) : (
        <Page onNavigate={handleNavigate} />
      )}
    </MainLayout>
  );
}
