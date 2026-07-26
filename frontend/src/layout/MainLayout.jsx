import AppShell from "../components/ui/AppShell.jsx";

export default function MainLayout({ activePage, onNavigate, children }) {
  return <AppShell activePage={activePage} onNavigate={onNavigate}>{children}</AppShell>;
}
